"""Owner first and takeover: MIYA never talks over the owner
(docs/jarvis-plan.md §5.8, JV-17).

The userbot records what the owner does in a chat and what a client does to
a message Jarvis is answering; this module turns each signal into chat state
(``jarvis_chat_state``) and outbox changes. The userbot's handlers only
record: none of them writes to Telegram.

* ``owner_read`` (``events.MessageRead(inbox=True)``): the owner read the
  client's latest message, so ``owner_seen_at`` moves on.
* ``owner_composing`` (``UpdateDraftMessage``): the owner is typing in the
  chat, so ``owner_composing_at`` moves on.
* ``owner_wrote`` (an outgoing message the owner typed, live or read by the
  catch-up): ``owner_replied_at``; the chat's pending drafts are cancelled
  (``auto:own``), its open escalation is resolved as ``owner_replied`` by
  ``auto:own``, the AI turn count starts again and the AI stays silent there
  until JARVIS_TAKEOVER_MINUTES after the message. Observation (``shadow``)
  rows stay as they are: they are never sent, and they are the record of
  what Jarvis would have said. ``owner_wrote_at`` is the same takeover for
  a message the userbot never stores (the owner's sticker or GIF).
* ``client_edited`` (after ``record_edit``): every pending AI row that read
  the edited message expires (``trigger_edited``), an open escalation whose
  stall it was is cancelled, and a fresh message queues the chat again, so
  the edited words get a fresh draft.
* ``client_deleted`` (``events.MessageDeleted``): the message is marked
  deleted (``meta.deleted_at``) and the pending AI rows answering it are
  cancelled (``trigger_deleted``); the responder never answers it. A draft
  for a later message of the same burst expires (``burst_deleted``) and the
  chat is queued again.

MIYA's own messages are never the owner's (rule 21): a message MIYA sent,
whether its echo is tagged already or the send is still in flight when the
echo arrives (whether Telethon dispatches self-sent messages is open, plan
Q11), never takes a chat over; and a read made while MIYA sends into the
chat (the sender marks a chat read only when it sends, JV-19) is not the
owner's. The signal and the claim tell the owner's message apart with one
SQL predicate (``authorship.owner_takeover_clause``).

The responder (``_load``) and ``outbox.claim_ready`` (the gate's chat-state
and trigger lines) both read these signals.

Failures are contained (rule 25): each row is changed in a savepoint, and a
failure is logged with its class and the row's ids while the other rows go
on. The userbot's handlers contain the rest, so a signal that cannot be
recorded never stops the reader.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import (
    ESCALATION_OPEN_STATES,
    Escalation,
    Interaction,
    JarvisChatState,
    OutboxMessage,
    Person,
)
from miya.jarvis import chatkey, gate, outbox
from miya.services import authorship

log = logging.getLogger(__name__)

# history ``by`` values: the owner's own message, and a client's edit or
# deletion (the values the gate writes for the same reasons at a claim).
BY_OWN: Final = outbox.BY_OWN
BY_EDIT: Final = outbox.BY_EXPIRY
BY_DELETE: Final = outbox.BY_GATE
OWNER_REPLIED: Final = gate.OWNER_REPLIED
TRIGGER_EDITED: Final = gate.TRIGGER_EDITED
TRIGGER_DELETED: Final = gate.TRIGGER_DELETED
BURST_DELETED: Final = gate.BURST_DELETED
OPEN: Final = "open"
RESOLVED: Final = "resolved"
CANCELLED: Final = "cancelled"
TOLD_FIELD: Final = outbox.TOLD_FIELD
TG_MESSAGE_KEY: Final = authorship.TG_MESSAGE_KEY

# How long after MIYA's send its echo may still arrive.
ECHO_WINDOW: Final = authorship.ECHO_WINDOW
# A read this soon after MIYA sent into the chat is the sender's own.
SELF_READ_WINDOW: Final = timedelta(minutes=1)
# The drafts the owner's own message takes off the table.
OWNER_CANCELS: Final = (outbox.DRAFT, outbox.APPROVED)
# How far back a deleted message may still be one a reply would answer.
DELETE_LOOKBACK: Final = timedelta(days=2)
# Marked Telegram ids at or below this are channels and supergroups, which
# number their messages on their own; the rest share the account's numbers.
CHANNEL_ID_FLOOR: Final = -1_000_000_000_000
# The owner hears about a draft or an escalation they were shown, once, for
# this long after it changed.
NOTICE_LOOKBACK: Final = timedelta(days=1)
NOTICE_BATCH: Final = 20

NoticeKind = Literal["draft_cancelled", "draft_edited", "escalation_closed"]
DRAFT_CANCELLED: Final[NoticeKind] = "draft_cancelled"
DRAFT_EDITED: Final[NoticeKind] = "draft_edited"
ESCALATION_CLOSED: Final[NoticeKind] = "escalation_closed"


def takeover() -> timedelta:
    """How long the AI stays silent after the owner writes (JARVIS_TAKEOVER_MINUTES)."""
    return timedelta(minutes=settings.jarvis_takeover_minutes)


def _meta(row: Interaction) -> dict[str, Any]:
    return row.meta if isinstance(row.meta, dict) else {}


def _entry(
    field: str, old: object, new: object, *, by: str, code: str, now: datetime
) -> dict[str, object]:
    return {
        "at": now.isoformat(),
        "field": field,
        "old": old,
        "new": new,
        "by": by,
        "code": code,
    }


def _tg_message_id() -> sa.ColumnElement[int]:
    value: sa.ColumnElement[int] = Interaction.meta[TG_MESSAGE_KEY].astext.cast(
        sa.BigInteger
    )
    return value


# --- the owner wrote ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Takeover:
    """What one message the owner typed did to its chat."""

    chat_key: str
    owner_at: datetime
    paused_until: datetime
    cancelled: tuple[int, ...] = ()
    escalation_id: int | None = None


async def miya_echo(session: AsyncSession, interaction: Interaction) -> bool:
    """Is this stored outgoing message MIYA's own send? Tagged rows are told
    apart by ``authorship``; this catches the echo that arrives before the
    outbox recorded the sent message's id (plan Q11), through the same SQL
    predicate the claim reads (``authorship.untagged_echo_clause``)."""
    found = await session.scalar(
        sa.select(authorship.untagged_echo_clause()).where(
            Interaction.id == interaction.id
        )
    )
    return bool(found)


async def _record_reply(
    session: AsyncSession,
    chat_key: str,
    person_id: int | None,
    *,
    at: datetime,
    until: datetime,
) -> None:
    """``owner_replied_at`` and the pause move on (never back), and the AI
    turn count starts again unless MIYA sent after this message."""
    insert = pg_insert(JarvisChatState).values(
        chat_key=chat_key,
        channel=chatkey.TELEGRAM,
        person_id=person_id,
        owner_replied_at=at,
        ai_paused_until=until,
        ai_turns_since_client=0,
    )
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[JarvisChatState.chat_key],
            set_={
                "person_id": sa.func.coalesce(
                    JarvisChatState.person_id, insert.excluded.person_id
                ),
                # GREATEST skips a NULL: the first value is taken as it is.
                "owner_replied_at": sa.func.greatest(
                    JarvisChatState.owner_replied_at, insert.excluded.owner_replied_at
                ),
                "ai_paused_until": sa.func.greatest(
                    JarvisChatState.ai_paused_until, insert.excluded.ai_paused_until
                ),
                "ai_turns_since_client": sa.case(
                    (
                        sa.or_(
                            JarvisChatState.last_ai_sent_at.is_(None),
                            JarvisChatState.last_ai_sent_at
                            <= insert.excluded.owner_replied_at,
                        ),
                        0,
                    ),
                    else_=JarvisChatState.ai_turns_since_client,
                ),
                "updated_at": sa.func.now(),
            },
        )
    )


async def _cancel(
    session: AsyncSession,
    row_ids: Iterable[int],
    *,
    by: str,
    code: str,
    now: datetime,
    chat_key: str | None = None,
) -> tuple[int, ...]:
    """``outbox.cancel`` per row, each in a savepoint: a row that moved on
    meanwhile is left alone, one that fails is logged and the rest go on."""
    done: list[int] = []
    for row_id in row_ids:
        try:
            async with session.begin_nested():
                await outbox.cancel(session, row_id, by=by, now=now, code=code)
            done.append(row_id)
        except outbox.AlreadyAnswered:
            continue
        except Exception as exc:
            log.error(
                "draft not cancelled",
                extra={
                    "outbox_id": row_id,
                    "chat_key": chat_key,
                    "code": code,
                    "error": type(exc).__name__,
                },
            )
    return tuple(done)


async def _drafts_answered_by(
    session: AsyncSession, chat_key: str, at: datetime
) -> list[int]:
    """The chat's pending AI drafts that the owner's message at ``at``
    answered: their client message came first, and no owner tap on the card
    came after it (a tap after the owner wrote is the owner's choice)."""
    return list(
        await session.scalars(
            sa.select(OutboxMessage.id)
            .join(Interaction, Interaction.id == OutboxMessage.trigger_interaction_id)
            .where(
                OutboxMessage.chat_key == chat_key,
                OutboxMessage.state.in_(OWNER_CANCELS),
                OutboxMessage.authored_by == gate.AI,
                Interaction.occurred_at <= at,
                sa.or_(
                    OutboxMessage.approved_at.is_(None),
                    OutboxMessage.approved_by == gate.AUTO_APPROVER,
                    OutboxMessage.approved_at <= at,
                ),
            )
            .order_by(OutboxMessage.id)
        )
    )


async def resolve_escalation(
    session: AsyncSession, chat_key: str, *, owner_at: datetime, now: datetime
) -> int | None:
    """Close the chat's open escalation the owner answered with their own
    message (the client message it is about came first, as for the drafts:
    the escalation itself may be written after the owner's message, by a
    responder that could not see it yet): ``resolved``, resolution
    ``owner_replied``, answered by ``auto:own``, with a history entry. The
    escalation's id, or None when there was none to close. Contained: a
    failure is logged with the ids and the escalation stays as it was."""
    escalation_id: int | None = None
    try:
        async with session.begin_nested():
            escalation = await session.scalar(
                sa.select(Escalation)
                .join(Interaction, Interaction.id == Escalation.interaction_id)
                .where(
                    Escalation.chat_key == chat_key,
                    Escalation.state.in_(ESCALATION_OPEN_STATES),
                    Interaction.occurred_at <= owner_at,
                )
                .order_by(Escalation.id)
                .limit(1)
                .with_for_update(of=Escalation)
                .execution_options(populate_existing=True)
            )
            if escalation is None:
                return None
            escalation_id = escalation.id
            old = escalation.state
            history = [
                *(escalation.history or []),
                _entry("state", old, RESOLVED, by=BY_OWN, code=OWNER_REPLIED, now=now),
            ]
            await session.execute(
                sa.update(Escalation)
                .where(Escalation.id == escalation_id)
                .values(
                    state=RESOLVED,
                    resolution=OWNER_REPLIED,
                    answered_by=BY_OWN,
                    answered_at=owner_at,
                    history=history,
                    updated_at=sa.func.now(),
                )
            )
            await session.flush()
    except Exception as exc:
        log.error(
            "escalation not resolved",
            extra={
                "chat_key": chat_key,
                "escalation_id": escalation_id,
                "error": type(exc).__name__,
            },
        )
        return None
    log.info(
        "escalation resolved by the owner's own message",
        extra={"chat_key": chat_key, "escalation_id": escalation_id},
    )
    return escalation_id


async def owner_wrote(
    session: AsyncSession, interaction: Interaction, *, now: datetime
) -> Takeover | None:
    """The owner typed a message in a chat: they take it over. None when
    the message is not the owner's own typing (MIYA's send, a client's
    message, an archive import) or Jarvis is off."""
    tg_chat_id = interaction.tg_chat_id
    if (
        not settings.jarvis_enabled
        or tg_chat_id is None
        or interaction.source != InteractionSource.telegram_userbot
        or not authorship.is_owner_typed(interaction)
        or _meta(interaction).get("archive")
    ):
        return None
    if await miya_echo(session, interaction):
        log.info(
            "an echo of MIYA's own send is no takeover",
            extra={
                "chat_key": chatkey.telegram(tg_chat_id),
                "interaction_id": interaction.id,
            },
        )
        return None
    return await owner_wrote_at(
        session,
        tg_chat_id=tg_chat_id,
        person_id=interaction.person_id,
        at=interaction.occurred_at,
        now=now,
        interaction_id=interaction.id,
    )


async def owner_wrote_at(
    session: AsyncSession,
    *,
    tg_chat_id: int,
    person_id: int | None,
    at: datetime,
    now: datetime,
    interaction_id: int | None = None,
) -> Takeover | None:
    """The takeover itself, for a message the caller knows the owner typed
    at ``at`` (``owner_wrote``, or a sticker the userbot never stores):
    ``owner_replied_at`` and the pause, the drafts it answered cancelled,
    the escalation closed. None when Jarvis is off. Contained step by
    step."""
    if not settings.jarvis_enabled:
        return None
    key = chatkey.telegram(tg_chat_id)
    at = min(at, now)
    until = at + takeover()
    try:
        async with session.begin_nested():
            await _record_reply(session, key, person_id, at=at, until=until)
    except Exception as exc:
        # The claim reads the owner's stored message itself; the drafts are
        # still cancelled below.
        log.error(
            "owner reply not recorded",
            extra={
                "chat_key": key,
                "interaction_id": interaction_id,
                "error": type(exc).__name__,
            },
        )
    try:
        async with session.begin_nested():
            answered = await _drafts_answered_by(session, key, at)
    except Exception as exc:
        log.error(
            "drafts the owner answered not read",
            extra={
                "chat_key": key,
                "interaction_id": interaction_id,
                "error": type(exc).__name__,
            },
        )
        answered = []
    cancelled = await _cancel(
        session, answered, by=BY_OWN, code=OWNER_REPLIED, now=now, chat_key=key
    )
    escalation_id = await resolve_escalation(session, key, owner_at=at, now=now)
    log.info(
        "owner took the chat over",
        extra={
            "chat_key": key,
            "interaction_id": interaction_id,
            "outbox_ids": list(cancelled),
            "escalation_id": escalation_id,
        },
    )
    return Takeover(
        chat_key=key,
        owner_at=at,
        paused_until=until,
        cancelled=cancelled,
        escalation_id=escalation_id,
    )


# --- the owner read, the owner is typing ------------------------------------------


async def _sending_now(
    session: AsyncSession, state: JarvisChatState, now: datetime
) -> bool:
    """Is MIYA sending into this chat (a claimed row, or a send just made)?"""
    if (
        state.last_ai_sent_at is not None
        and state.last_ai_sent_at >= now - SELF_READ_WINDOW
    ):
        return True
    claimed = await session.scalar(
        sa.select(OutboxMessage.id)
        .where(
            OutboxMessage.chat_key == state.chat_key,
            OutboxMessage.state == outbox.SENDING,
        )
        .limit(1)
    )
    return claimed is not None


async def owner_read(
    session: AsyncSession, *, tg_chat_id: int, max_id: int, now: datetime
) -> bool:
    """The owner read the chat up to message ``max_id``: when that covers the
    client's latest stored message, ``owner_seen_at`` moves on to ``now``.
    True when recorded. Only a chat Jarvis keeps state for; a read made
    while MIYA sends there is the sender's own and is ignored."""
    if not settings.jarvis_enabled:
        return False
    key = chatkey.telegram(tg_chat_id)
    state = await session.get(JarvisChatState, key, populate_existing=True)
    if state is None:
        return False
    if await _sending_now(session, state, now):
        log.info("a read while MIYA sends is not the owner's", extra={"chat_key": key})
        return False
    newest = await session.scalar(
        sa.select(sa.func.max(_tg_message_id())).where(
            Interaction.source == InteractionSource.telegram_userbot,
            Interaction.tg_chat_id == tg_chat_id,
            Interaction.direction == Direction.in_,
            Interaction.meta.has_key(TG_MESSAGE_KEY),
            # A message the client deleted can no longer be read.
            sa.not_(Interaction.meta.has_key(outbox.DELETED_KEY)),
        )
    )
    if newest is None or max_id < newest:
        return False
    await session.execute(
        sa.update(JarvisChatState)
        .where(JarvisChatState.chat_key == key)
        .values(
            owner_seen_at=sa.func.greatest(JarvisChatState.owner_seen_at, now),
            updated_at=sa.func.now(),
        )
    )
    log.info("owner read the chat", extra={"chat_key": key, "tg_message_id": max_id})
    return True


async def owner_composing(
    session: AsyncSession, *, tg_chat_id: int, at: datetime, now: datetime
) -> bool:
    """The owner's draft in this chat changed (they are typing):
    ``owner_composing_at`` moves on. True when recorded (only a chat Jarvis
    keeps state for)."""
    if not settings.jarvis_enabled:
        return False
    key = chatkey.telegram(tg_chat_id)
    moment = min(at, now)
    found = await session.scalar(
        sa.update(JarvisChatState)
        .where(JarvisChatState.chat_key == key)
        .values(
            owner_composing_at=sa.func.greatest(
                JarvisChatState.owner_composing_at, moment
            ),
            updated_at=sa.func.now(),
        )
        .returning(JarvisChatState.chat_key)
    )
    if found is None:
        return False
    log.info("owner is typing in the chat", extra={"chat_key": key})
    return True


# --- the client edited or deleted a message ---------------------------------------


async def _cancel_escalation_for_edit(
    session: AsyncSession, escalation_id: int, *, now: datetime
) -> bool:
    """An ``open`` escalation whose stall answered words the client edited is
    cancelled, so the edited message is read again as new. One the owner has
    touched (snoozed, replying) stays theirs."""
    try:
        async with session.begin_nested():
            escalation = await session.scalar(
                sa.select(Escalation)
                .where(Escalation.id == escalation_id, Escalation.state == OPEN)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if escalation is None:
                return False
            history = [
                *(escalation.history or []),
                _entry(
                    "state", OPEN, CANCELLED, by=BY_EDIT, code=TRIGGER_EDITED, now=now
                ),
            ]
            await session.execute(
                sa.update(Escalation)
                .where(Escalation.id == escalation_id)
                .values(
                    state=CANCELLED,
                    resolution=CANCELLED,
                    history=history,
                    updated_at=sa.func.now(),
                )
            )
            await session.flush()
            return True
    except Exception as exc:
        log.error(
            "escalation not cancelled after an edit",
            extra={"escalation_id": escalation_id, "error": type(exc).__name__},
        )
        return False


async def _requeue(session: AsyncSession, chat_key: str, *, now: datetime) -> None:
    """Queue the chat for the responder again, after the debounce."""
    due = now + timedelta(seconds=settings.jarvis_debounce_seconds)
    try:
        async with session.begin_nested():
            await session.execute(
                sa.update(JarvisChatState)
                .where(JarvisChatState.chat_key == chat_key)
                .values(respond_after=due, updated_at=sa.func.now())
            )
    except Exception as exc:
        log.error(
            "chat not queued again after an edit or a deletion",
            extra={"chat_key": chat_key, "error": type(exc).__name__},
        )


async def client_edited(
    session: AsyncSession,
    interaction: Interaction,
    *,
    now: datetime,
    requeue: bool = True,
) -> tuple[int, ...]:
    """A client edited a message: the pending AI rows that read it expire
    (they answer it, or a later message it could have come with: the
    responder reads the unanswered messages of the last
    JARVIS_MAX_MESSAGE_AGE_SECONDS together), an open escalation whose stall
    was among them is cancelled, and, when the message is still young enough
    for a reply, the chat is queued again for a fresh draft. Without
    ``requeue`` (the edit swapped the message's media) nothing is drafted
    again. The expired rows' ids; nothing changes when no draft read the
    message."""
    tg_chat_id = interaction.tg_chat_id
    if (
        not settings.jarvis_enabled
        or tg_chat_id is None
        or interaction.direction != Direction.in_
        or interaction.source != InteractionSource.telegram_userbot
    ):
        return ()
    key = chatkey.telegram(tg_chat_id)
    max_age = timedelta(seconds=settings.jarvis_max_message_age_seconds)
    pending = (
        await session.execute(
            sa.select(OutboxMessage.id, OutboxMessage.escalation_id)
            .join(Interaction, Interaction.id == OutboxMessage.trigger_interaction_id)
            .where(
                OutboxMessage.chat_key == key,
                OutboxMessage.state.in_(outbox.PENDING),
                OutboxMessage.authored_by == gate.AI,
                Interaction.occurred_at >= interaction.occurred_at,
                Interaction.occurred_at <= interaction.occurred_at + max_age,
            )
            .order_by(OutboxMessage.id)
        )
    ).all()
    expired: list[int] = []
    escalations: set[int] = set()
    for row_id, escalation_id in pending:
        try:
            async with session.begin_nested():
                if not await outbox.expire_edited(session, row_id, now=now):
                    continue
            expired.append(row_id)
            if escalation_id is not None:
                escalations.add(escalation_id)
        except Exception as exc:
            log.error(
                "draft not expired after an edit",
                extra={
                    "outbox_id": row_id,
                    "chat_key": key,
                    "interaction_id": interaction.id,
                    "error": type(exc).__name__,
                },
            )
    if not expired:
        return ()
    for escalation_id in sorted(escalations):
        await _cancel_escalation_for_edit(session, escalation_id, now=now)
    if (
        requeue
        and interaction.occurred_at >= now - max_age
        and not _meta(interaction).get("archive")
    ):
        await _requeue(session, key, now=now)
    log.info(
        "drafts expired after a client edit",
        extra={
            "chat_key": key,
            "interaction_id": interaction.id,
            "outbox_ids": expired,
        },
    )
    return tuple(expired)


async def client_deleted(
    session: AsyncSession,
    *,
    tg_chat_id: int | None,
    message_ids: Iterable[int],
    now: datetime,
) -> tuple[int, ...]:
    """Messages were deleted. Telegram names the chat only for a channel or
    a supergroup (``tg_chat_id``); elsewhere a message id is unique across
    the account's private chats and small groups, so those are searched.
    Each recent client message found is marked deleted (``meta.deleted_at``)
    and the pending AI rows answering it are cancelled
    (``trigger_deleted``). A draft for a later message of the same burst
    (its trigger within JARVIS_MAX_MESSAGE_AGE_SECONDS after the deleted
    one) read the deleted words too: it expires (``burst_deleted``) and a
    still-fresh chat is queued again, so what is left is drafted without
    them. A stall stays: it is the escalation's, and quotes nothing. The
    cancelled and expired rows' ids."""
    ids = sorted(
        {i for i in message_ids if isinstance(i, int) and not isinstance(i, bool)}
    )
    if not settings.jarvis_enabled or not ids:
        return ()
    query = sa.select(Interaction).where(
        Interaction.source == InteractionSource.telegram_userbot,
        Interaction.direction == Direction.in_,
        Interaction.meta.has_key(TG_MESSAGE_KEY),
        _tg_message_id().in_(ids),
        Interaction.occurred_at >= now - DELETE_LOOKBACK,
    )
    if tg_chat_id is not None:
        query = query.where(Interaction.tg_chat_id == tg_chat_id)
    else:
        query = query.where(Interaction.tg_chat_id > CHANNEL_ID_FLOOR)
    marked: list[tuple[int, int | None, datetime]] = []
    for row in list(await session.scalars(query.order_by(Interaction.id))):
        row_id = row.id
        try:
            async with session.begin_nested():
                meta = dict(_meta(row))
                if outbox.DELETED_KEY not in meta:
                    meta[outbox.DELETED_KEY] = now.isoformat()
                    row.meta = meta
                    await session.flush()
            marked.append((row_id, row.tg_chat_id, row.occurred_at))
        except Exception as exc:
            log.error(
                "deleted message not marked",
                extra={"interaction_id": row_id, "error": type(exc).__name__},
            )
    if not marked:
        return ()
    marked_ids = [row_id for row_id, _chat, _at in marked]
    pending = list(
        await session.scalars(
            sa.select(OutboxMessage.id)
            .where(
                OutboxMessage.trigger_interaction_id.in_(marked_ids),
                OutboxMessage.state.in_(outbox.PENDING),
                OutboxMessage.authored_by == gate.AI,
            )
            .order_by(OutboxMessage.id)
        )
    )
    cancelled = await _cancel(
        session, pending, by=BY_DELETE, code=TRIGGER_DELETED, now=now
    )
    expired: list[int] = []
    for row_id, chat_id, occurred_at in marked:
        if chat_id is not None:
            expired.extend(
                await _expire_burst(
                    session,
                    chat_id,
                    deleted_id=row_id,
                    deleted_at=occurred_at,
                    skip=marked_ids,
                    now=now,
                )
            )
    log.info(
        "client messages deleted",
        extra={
            "interaction_ids": marked_ids[:20],
            "outbox_ids": list(cancelled),
            "expired_ids": expired,
        },
    )
    return (*cancelled, *expired)


async def _expire_burst(
    session: AsyncSession,
    tg_chat_id: int,
    *,
    deleted_id: int,
    deleted_at: datetime,
    skip: Iterable[int],
    now: datetime,
) -> list[int]:
    """The pending AI drafts of a chat that answered a burst the deleted
    message was part of expire (``burst_deleted``), each in a savepoint; a
    still-fresh chat is queued again. The expired rows' ids. Contained: a
    failure is logged with the ids and the rest go on."""
    key = chatkey.telegram(tg_chat_id)
    max_age = timedelta(seconds=settings.jarvis_max_message_age_seconds)
    try:
        async with session.begin_nested():
            later = list(
                await session.scalars(
                    sa.select(OutboxMessage.id)
                    .join(
                        Interaction,
                        Interaction.id == OutboxMessage.trigger_interaction_id,
                    )
                    .where(
                        OutboxMessage.chat_key == key,
                        OutboxMessage.state.in_(outbox.PENDING),
                        OutboxMessage.authored_by == gate.AI,
                        OutboxMessage.escalation_id.is_(None),
                        OutboxMessage.trigger_interaction_id.notin_(list(skip)),
                        Interaction.occurred_at >= deleted_at,
                        Interaction.occurred_at <= deleted_at + max_age,
                    )
                    .order_by(OutboxMessage.id)
                )
            )
    except Exception as exc:
        log.error(
            "drafts of a burst not read after a deletion",
            extra={
                "chat_key": key,
                "interaction_id": deleted_id,
                "error": type(exc).__name__,
            },
        )
        return []
    expired: list[int] = []
    for row_id in later:
        try:
            async with session.begin_nested():
                if not await outbox.expire_edited(
                    session, row_id, now=now, code=BURST_DELETED
                ):
                    continue
            expired.append(row_id)
        except Exception as exc:
            log.error(
                "draft not expired after a deletion",
                extra={
                    "outbox_id": row_id,
                    "chat_key": key,
                    "interaction_id": deleted_id,
                    "error": type(exc).__name__,
                },
            )
    if expired and deleted_at >= now - max_age:
        await _requeue(session, key, now=now)
    return expired


# --- what the owner is told ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notice:
    """A draft or an escalation the owner was shown that a signal took off
    the table: their own reply cancelled the draft, the client's edit
    replaced it, their own reply closed the escalation."""

    kind: NoticeKind
    row_id: int
    name: str | None = None
    # The card or alert the owner saw, so the notice answers it.
    reply_to: int | None = None


def _untold(history: InstrumentedAttribute[list[Any]]) -> sa.ColumnElement[bool]:
    told: sa.ColumnElement[bool] = history.op("@>")(
        sa.cast(sa.literal([{"field": TOLD_FIELD}], JSONB), JSONB)
    )
    return sa.not_(told)


async def untold(
    session: AsyncSession, *, now: datetime, limit: int = NOTICE_BATCH
) -> list[Notice]:
    """The notices still owed: only for a draft card or an escalation alert
    the owner was shown (a shadow row, or one never carded, is no news). A
    draft the client's edit expired is news once its fresh draft exists:
    «qoralama yangilanadi» is never told for one that is not coming (the
    edited message was too old to answer, or nothing queued the chat)."""
    since = now - NOTICE_LOOKBACK
    fresh = aliased(OutboxMessage)
    redrafted = (
        sa.exists()
        .where(
            fresh.chat_key == OutboxMessage.chat_key,
            fresh.id > OutboxMessage.id,
            fresh.trigger_interaction_id >= OutboxMessage.trigger_interaction_id,
        )
        .correlate(OutboxMessage)
    )
    drafts = await session.execute(
        sa.select(OutboxMessage.id, OutboxMessage.state, OutboxMessage.card_tg_message_id)
        .where(
            OutboxMessage.card_tg_message_id.isnot(None),
            OutboxMessage.updated_at >= since,
            sa.or_(
                sa.and_(
                    OutboxMessage.state == outbox.CANCELLED,
                    OutboxMessage.last_error == OWNER_REPLIED,
                ),
                sa.and_(
                    OutboxMessage.state == outbox.EXPIRED,
                    OutboxMessage.last_error == TRIGGER_EDITED,
                    redrafted,
                ),
            ),
            _untold(OutboxMessage.history),
        )
        .order_by(OutboxMessage.id)
        .limit(limit)
    )
    found = [
        Notice(
            kind=DRAFT_CANCELLED if state == outbox.CANCELLED else DRAFT_EDITED,
            row_id=int(row_id),
            reply_to=card,
        )
        for row_id, state, card in drafts
    ]
    closed = await session.execute(
        sa.select(Escalation.id, Person.display_name, Escalation.alert_tg_message_id)
        .outerjoin(Person, Person.id == Escalation.person_id)
        .where(
            Escalation.alerted_at.isnot(None),
            Escalation.updated_at >= since,
            Escalation.state == RESOLVED,
            Escalation.resolution == OWNER_REPLIED,
            Escalation.answered_by == BY_OWN,
            _untold(Escalation.history),
        )
        .order_by(Escalation.id)
        .limit(limit)
    )
    found.extend(
        Notice(kind=ESCALATION_CLOSED, row_id=int(row_id), name=name, reply_to=alert)
        for row_id, name, alert in closed
    )
    return found


async def mark_told(session: AsyncSession, notice: Notice, *, now: datetime) -> None:
    """The owner got this notice: never again."""
    if notice.kind != ESCALATION_CLOSED:
        await outbox.mark_told(session, notice.row_id, now=now, by=BY_OWN)
        return
    escalation = await session.scalar(
        sa.select(Escalation)
        .where(Escalation.id == notice.row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if escalation is None:
        return
    history = [
        *(escalation.history or []),
        _entry(
            TOLD_FIELD, None, escalation.state, by=BY_OWN, code=OWNER_REPLIED, now=now
        ),
    ]
    await session.execute(
        sa.update(Escalation)
        .where(Escalation.id == notice.row_id)
        .values(history=history, updated_at=sa.func.now())
    )
    await session.flush()


__all__ = [
    "BURST_DELETED",
    "CHANNEL_ID_FLOOR",
    "DRAFT_CANCELLED",
    "DRAFT_EDITED",
    "ESCALATION_CLOSED",
    "Notice",
    "NoticeKind",
    "Takeover",
    "client_deleted",
    "client_edited",
    "mark_told",
    "miya_echo",
    "owner_composing",
    "owner_read",
    "owner_wrote",
    "owner_wrote_at",
    "resolve_escalation",
    "takeover",
    "untold",
]
