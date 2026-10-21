"""The responder: from a client's message to a proposal (docs/jarvis-plan.md §3.3,
JV-16).

1. **Enqueue.** After ``ingest_message`` returns True for a live incoming
   private message younger than JARVIS_MAX_MESSAGE_AGE_SECONDS, the userbot
   calls ``enqueue_candidate``: one upsert of ``jarvis_chat_state`` that sets
   ``respond_after`` JARVIS_DEBOUNCE_SECONDS ahead (a burst pushes it on, so
   it collapses into one reply). No model call in a Telethon handler; never
   from the catch-up or a backfill. An edit queues the chat again only when
   it expired a draft that read the edited words (JV-17, ``chat_state``).
2. **Claim.** ``jarvis_respond`` (every 15 s) claims due chat states with
   ``SKIP LOCKED`` and leases them (``respond_after`` moves ``CLAIM_LEASE``
   ahead), then reads each chat in a transaction of its own: no row lock is
   held during a model call, so the userbot's next enqueue never waits. A run
   that dies leaves the lease to run out and the chat is read again; an
   inbound message past the age limit never gets a reply.
3. **The gate.** Jarvis on, the chat eligible (a private chat, no personal
   rule, no hard exclusion), the trigger fresh, the owner-first window over
   (JARVIS_OWNER_FIRST_SECONDS from the client's first unanswered message),
   the owner has not answered, read the latest message or typed in the chat
   just now (JV-17: typing pushes the look-in on), the AI is not paused
   there after the owner wrote, no deleted message is answered, nothing in
   the burst the shield flagged, no open escalation (a second push gets
   silence: the escalation's history grows), the per-chat cap, the AI turn
   limit, the cost cap. An open
   escalation holds the chat only until the owner writes in it (resolved,
   ``auto:own``) or, still ``open`` (or ``owner_replying``: the owner said on
   its card they would write, JV-18), for JARVIS_DRAFT_EXPIRY_HOURS (expired).
4. **Deterministic signals first** (``redlines.classify_inbound``): a price,
   deadline, promise, guarantee, payment claim, identity, complaint, legal
   or other-people message opens an escalation and queues a stall; no model
   is called. An injection tripwire sends the chat to the owner with no
   stall and no model call. So a courtesy-only reply is never made for such
   a message. In ``draft`` and ``auto`` mode such a message does not wait
   for the owner-first window (JV-18 1a): it is escalated as soon as the
   debounce ends, so the owner's alert goes out at once (``run(alert=…)``),
   and only the stall is held (``not_before``) until the window ends.
5. Otherwise the **intent** (deterministic greeting, else EXTRACT_MODEL),
   **KB retrieval** (only entries whose answer passes the reply role: a
   verbatim quote of one is auto-safe), and a **draft** by REASON_MODEL with
   no tools (``draft.DraftInputs`` is all it reads; an auto-eligible intent
   also gets the owner's question bank, JV-23).
6. **Guards.** The deterministic guard (``outbox.preview``, the inputs
   ``create`` uses), then, only on a pass, the model guard (``guard.check``),
   which can only block: its block, error, refusal or parse failure becomes
   a stall plus an escalation; a suspicious thread (spam, scam, hijack) goes
   to the owner with no stall.
7. **The row.** ``outbox.create`` with the retrieved entry ids and the model
   verdict; the mode decides its state (``shadow`` until the owner raises
   it). A model failure on the intent or the draft is ``needs_review``: an
   escalation with no stall, nothing sent. Over the cost cap no model is
   called: every eligible message goes to the owner with a stall template.

Failures are contained per chat (rule 25): every write runs in a savepoint,
a failure is logged with its class and the chat's ids, and the next chat
goes on. Model usage is recorded by ``costs.ask`` in its own transaction.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import (
    ESCALATION_OPEN_STATES,
    ESCALATION_REASONS,
    ChatMonitor,
    Escalation,
    Interaction,
    JarvisChatState,
    OutboxMessage,
)
from miya.db.session import session_scope
from miya.jarvis import (
    caps,
    chat_state,
    chatkey,
    costs,
    draft,
    envelope,
    gate,
    guard,
    intent,
    kb,
    outbox,
    policy,
    redlines,
    stalls,
    style,
)
from miya.jarvis.style_prompt import ReplyStyle
from miya.services import authorship, safety

log = logging.getLogger(__name__)

# How long a claimed chat is this run's before another run may claim it.
CLAIM_LEASE: Final = timedelta(minutes=3)
# Chats one run reads.
BATCH: Final = 20
# How far back an escalation's repeats mark messages as handled.
HANDLED_LOOKBACK: Final = timedelta(days=2)
# The run's own clock: how long the chats before this one took.
_monotonic = time.monotonic

BY: Final = "auto:policy"
# The modes whose escalations are alerted to the owner (JV-18 1a): there a
# red-line message is escalated as soon as the debounce ends, and only the
# stall waits for the owner-first window. In shadow nothing is shown, so the
# responder keeps the window for everything.
ALERT_MODES: Final = ("draft", "auto")
# Who closed an escalation the responder settled by its age
# (JARVIS_DRAFT_EXPIRY_HOURS); the owner's own message closes one through
# chat_state.resolve_escalation (``auto:own``).
BY_EXPIRY: Final = "auto:expiry"
OPEN: Final = "open"
OWNER_REPLYING: Final = "owner_replying"
# The escalation states the responder lets expire after
# JARVIS_DRAFT_EXPIRY_HOURS: still waiting for the owner, or the owner said
# they would write and never did (JV-18). A snoozed one comes back as open.
EXPIRING: Final = (OPEN, OWNER_REPLYING)
EXPIRED: Final = "expired"
# The code of a shadow-mode escalation closed once the chat's mode alerts.
MODE_CHANGED: Final = "mode_changed"
SHADOW_MODE: Final = "shadow"
OTHER: Final = "other"
STALL_INTENT: Final = "stall"
REPEAT_FIELD: Final = "repeat"
# The escalation reasons a deterministic flag of the output guard gives.
_FLAG_REASONS: Final[frozenset[str]] = frozenset(ESCALATION_REASONS)

# Outcome codes (logs and the escalation history; never text).
DISABLED: Final = "disabled"
GONE: Final = "gone"
INELIGIBLE: Final = "ineligible"
STALE: Final = "stale"
NOTHING: Final = "nothing"
OWNER_REPLIED: Final = gate.OWNER_REPLIED
OWNER_SEEN: Final = gate.OWNER_SEEN
AI_PAUSED: Final = gate.AI_PAUSED
WAITING: Final = "owner_first"
UNSAFE: Final = "unsafe"
REPEAT: Final = "repeat"
CHAT_CAP: Final = "chat_cap"
AI_TURNS: Final = "ai_turns"
RED_LINE: Final = "red_line"
INJECTION: Final = "injection"
COST_CAP: Final = costs.COST_CAP_CODE
NEEDS_REVIEW: Final = "needs_review"
INTENT_ESCALATES: Final = "intent"
NEEDS_OWNER: Final = "needs_owner"
GUARD_BLOCK: Final = "guard_block"
MODEL_BLOCK: Final = "model_block"
SUSPICIOUS: Final = "suspicious"
DRAFTED: Final = "drafted"
ERROR: Final = "error"


# --- settings ---------------------------------------------------------------------------


def debounce() -> timedelta:
    return timedelta(seconds=settings.jarvis_debounce_seconds)


def owner_first() -> timedelta:
    return timedelta(seconds=settings.jarvis_owner_first_seconds)


def max_age() -> timedelta:
    return timedelta(seconds=settings.jarvis_max_message_age_seconds)


# --- 1. enqueue -------------------------------------------------------------------------


def _meta(interaction: Interaction) -> dict[str, Any]:
    return interaction.meta if isinstance(interaction.meta, dict) else {}


def fresh(occurred_at: datetime, now: datetime) -> bool:
    """Young enough for an AI reply (JARVIS_MAX_MESSAGE_AGE_SECONDS)."""
    return occurred_at >= now - max_age()


async def enqueue_candidate(
    session: AsyncSession, interaction: Interaction, *, now: datetime
) -> bool:
    """Queue a live incoming private message for the responder; True when
    queued. Only SQL: the userbot calls it right after ``ingest_message``
    returned True for a live message, never for the catch-up, a backfill or
    an edit. Anything else (Jarvis off, a group, an archive import, an old
    message, a chat the owner switched off) is not queued."""
    if not settings.jarvis_enabled or policy.ceiling() == "off":
        return False
    tg_chat_id = interaction.tg_chat_id
    if (
        interaction.direction != Direction.in_
        or interaction.source != InteractionSource.telegram_userbot
        or tg_chat_id is None
        or tg_chat_id <= 0
        or _meta(interaction).get("archive")
        or not fresh(interaction.occurred_at, now)
    ):
        return False
    monitor = await session.scalar(
        sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == tg_chat_id)
    )
    if (
        monitor is None
        or not monitor.monitor_enabled
        or monitor.chat_type is not ChatType.private
    ):
        return False
    due = max(now, interaction.occurred_at) + debounce()
    insert = pg_insert(JarvisChatState).values(
        chat_key=chatkey.telegram(tg_chat_id),
        channel=chatkey.TELEGRAM,
        person_id=interaction.person_id,
        last_inbound_interaction_id=interaction.id,
        last_inbound_at=interaction.occurred_at,
        respond_after=due,
    )
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[JarvisChatState.chat_key],
            set_={
                "person_id": sa.func.coalesce(
                    insert.excluded.person_id, JarvisChatState.person_id
                ),
                "last_inbound_interaction_id": (
                    insert.excluded.last_inbound_interaction_id
                ),
                "last_inbound_at": insert.excluded.last_inbound_at,
                "respond_after": insert.excluded.respond_after,
                "updated_at": sa.func.now(),
            },
            # A message delivered out of order never moves the state back.
            where=sa.or_(
                JarvisChatState.last_inbound_at.is_(None),
                JarvisChatState.last_inbound_at <= insert.excluded.last_inbound_at,
            ),
        )
    )
    log.info(
        "reply candidate queued",
        extra={
            "chat_key": chatkey.telegram(tg_chat_id),
            "interaction_id": interaction.id,
        },
    )
    return True


# --- 2. claim ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    chat_key: str
    channel: str
    lease: datetime
    interaction_id: int | None


async def claim_due(
    session: AsyncSession, *, now: datetime, limit: int = BATCH
) -> list[Candidate]:
    """Due chat states, locked with SKIP LOCKED and leased; the caller
    commits before reading them."""
    rows = list(
        await session.scalars(
            sa.select(JarvisChatState)
            .where(
                JarvisChatState.respond_after.isnot(None),
                JarvisChatState.respond_after <= now,
            )
            .order_by(JarvisChatState.respond_after, JarvisChatState.chat_key)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    lease = now + CLAIM_LEASE
    out: list[Candidate] = []
    for row in rows:
        row.respond_after = lease
        out.append(
            Candidate(
                chat_key=row.chat_key,
                channel=row.channel,
                lease=lease,
                interaction_id=row.last_inbound_interaction_id,
            )
        )
    await session.flush()
    return out


async def _finish(
    session: AsyncSession, candidate: Candidate, *, until: datetime | None = None
) -> None:
    """Done with this chat (``until`` None), or look again at ``until``. A
    message that arrived meanwhile moved ``respond_after`` off the lease, and
    then it stays as the enqueue set it."""
    await session.execute(
        sa.update(JarvisChatState)
        .where(
            JarvisChatState.chat_key == candidate.chat_key,
            JarvisChatState.respond_after == candidate.lease,
        )
        .values(respond_after=until, updated_at=sa.func.now())
    )


# --- 3. what one chat looks like ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    chat_key: str
    code: str
    outbox_ids: tuple[int, ...] = ()
    escalation_id: int | None = None
    # Drafting was stopped by the cost cap (the owner is told once a day).
    cost_capped: bool = False


@dataclass(slots=True)
class _Chat:
    chat_key: str
    channel: str
    tg_chat_id: int
    person_id: int | None
    trigger: Interaction
    burst: list[Interaction]
    mode: str
    text: str = ""
    burst_ids: list[int] = field(default_factory=list)
    # The owner's latest own message in this chat (before the burst).
    owner_at: datetime | None = None
    # Read before the owner-first window ended (JV-18 1a): the escalation and
    # its alert are made now, the client's stall waits until then.
    stall_after: datetime | None = None


def message_text(interaction: Interaction) -> str:
    """What the client said: the text and a voice note's transcript."""
    parts = [p.strip() for p in (interaction.raw_text, interaction.transcript) if p]
    return "\n".join(p for p in parts if p)


def _risky(interaction: Interaction) -> bool:
    media = interaction.media if isinstance(interaction.media, dict) else {}
    risk = media.get("risk")
    return isinstance(risk, dict) and risk.get("kind") not in (None, safety.OK)


def unsafe(interaction: Interaction) -> bool:
    """The shield (JV-01, JV-09) flagged it: never answered."""
    return safety.blocks_reply(_meta(interaction)) or _risky(interaction)


def _edited_away(history: Sequence[object] | None) -> bool:
    """Was this escalation cancelled because the client edited the message
    its stall answered (JV-17)? Then the edited words are read again."""
    last = history[-1] if history else None
    return isinstance(last, dict) and last.get("code") == chat_state.TRIGGER_EDITED


async def _handled_upto(session: AsyncSession, chat_key: str, now: datetime) -> int:
    """The newest client message of this chat the responder already answered
    (a proposal's trigger, an escalation or one of its repeats). A draft a
    client edit or a deletion in its burst expired, and the escalation an
    edit cancelled with it, answered nothing: those messages are read again
    (JV-17)."""
    triggered = await session.scalar(
        sa.select(sa.func.max(OutboxMessage.trigger_interaction_id)).where(
            OutboxMessage.chat_key == chat_key,
            sa.not_(
                sa.and_(
                    OutboxMessage.state == outbox.EXPIRED,
                    # COALESCE: an expired row with no code answered its message.
                    sa.func.coalesce(OutboxMessage.last_error, "").in_(
                        outbox.REDRAFT_CODES
                    ),
                )
            ),
        )
    )
    handled = int(triggered or 0)
    for interaction_id, state, history in await session.execute(
        sa.select(Escalation.interaction_id, Escalation.state, Escalation.history).where(
            Escalation.chat_key == chat_key,
            sa.or_(
                Escalation.state.in_(ESCALATION_OPEN_STATES),
                Escalation.updated_at >= now - HANDLED_LOOKBACK,
            ),
        )
    ):
        if state == chat_state.CANCELLED and _edited_away(history):
            continue
        handled = max(handled, int(interaction_id))
        for entry in history or []:
            value = entry.get("new") if isinstance(entry, dict) else None
            if (
                isinstance(entry, dict)
                and entry.get("field") == REPEAT_FIELD
                and isinstance(value, int)
            ):
                handled = max(handled, value)
    return handled


async def _last_outgoing(
    session: AsyncSession, tg_chat_id: int
) -> tuple[datetime | None, datetime | None]:
    """(the owner's latest own message, the latest outgoing message at all)
    in this chat."""
    base = sa.select(sa.func.max(Interaction.occurred_at)).where(
        Interaction.tg_chat_id == tg_chat_id,
        Interaction.source == InteractionSource.telegram_userbot,
    )
    owner: datetime | None = await session.scalar(
        base.where(authorship.owner_words_clause())
    )
    anyone: datetime | None = await session.scalar(
        base.where(Interaction.direction == Direction.out)
    )
    return owner, anyone


async def _burst(
    session: AsyncSession,
    *,
    tg_chat_id: int,
    after_id: int,
    after_at: datetime | None,
    now: datetime,
) -> list[Interaction]:
    """The client's unanswered live messages, oldest first; never one the
    client deleted (JV-17)."""
    query = sa.select(Interaction).where(
        Interaction.tg_chat_id == tg_chat_id,
        Interaction.source == InteractionSource.telegram_userbot,
        Interaction.direction == Direction.in_,
        Interaction.id > after_id,
        Interaction.occurred_at >= now - max_age(),
        sa.not_(Interaction.meta.has_key("archive")),
        sa.not_(Interaction.meta.has_key(outbox.DELETED_KEY)),
    )
    if after_at is not None:
        query = query.where(Interaction.occurred_at > after_at)
    return list(
        await session.scalars(query.order_by(Interaction.occurred_at, Interaction.id))
    )


async def thread_turns(
    session: AsyncSession, tg_chat_id: int, *, limit: int | None = None
) -> list[envelope.Turn]:
    """This chat's latest JARVIS_HISTORY_TURNS turns, oldest first: the only
    conversation any Jarvis model reads. A client message the shield flagged
    is never quoted to a model (§5.5): its turn reads ``envelope.HIDDEN``."""
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(
                Interaction.tg_chat_id == tg_chat_id,
                Interaction.source == InteractionSource.telegram_userbot,
            )
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
            .limit(limit or settings.jarvis_history_turns)
        )
    )
    turns: list[envelope.Turn] = []
    for row in reversed(rows):
        if row.direction == Direction.in_:
            speaker: envelope.Speaker = "CLIENT"
        elif authorship.is_ai_reply(row):
            speaker = "MIYA"
        else:
            speaker = "OWNER"
        text = (
            envelope.HIDDEN
            if row.direction == Direction.in_ and unsafe(row)
            else message_text(row)
        )
        turns.append(envelope.turn(speaker, text, row.id))
    return turns


# --- writes: escalations and proposals --------------------------------------------------


def _reasons(values: Iterable[str]) -> list[str]:
    found = [v for v in dict.fromkeys(values) if v in _FLAG_REASONS]
    return found or [OTHER]


def _guard_reasons(flags: Iterable[str]) -> list[str]:
    """The escalation reasons of a deterministic block's flags; digits, a
    link, the language or anything else is «other»."""
    return _reasons(f if f in _FLAG_REASONS else OTHER for f in flags)


def _entry(
    field_name: str, new: object, *, now: datetime, code: str, **extra: object
) -> dict[str, object]:
    entry: dict[str, object] = {
        "at": now.isoformat(),
        "field": field_name,
        "old": None,
        "new": new,
        "by": BY,
        "code": code,
    }
    entry.update(extra)
    return entry


async def _open_escalation(session: AsyncSession, chat_key: str) -> Escalation | None:
    found: Escalation | None = await session.scalar(
        sa.select(Escalation)
        .where(
            Escalation.chat_key == chat_key,
            Escalation.state.in_(ESCALATION_OPEN_STATES),
        )
        .with_for_update()
    )
    return found


def _repeat(
    escalation: Escalation,
    chat: _Chat,
    reasons: Sequence[str],
    *,
    code: str,
    now: datetime,
) -> None:
    """A second push into the open escalation: only a real reason is merged
    («other» is what a push with no signal reads as, and never dilutes the
    reasons the escalation already has)."""
    added = [r for r in reasons if r != OTHER] or ([] if escalation.reasons else [OTHER])
    escalation.reasons = list(dict.fromkeys([*(escalation.reasons or []), *added]))
    escalation.history = [
        *(escalation.history or []),
        _entry(REPEAT_FIELD, chat.trigger.id, now=now, code=code),
    ]


def _opened_unseen(escalation: Escalation) -> bool:
    """Opened in shadow mode (its creation entry says so) and never alerted."""
    history = escalation.history or []
    first = history[0] if history else None
    mode = first.get("mode") if isinstance(first, dict) else None
    return mode == SHADOW_MODE and escalation.alerted_at is None


def escalation_expiry() -> timedelta:
    return timedelta(hours=settings.jarvis_draft_expiry_hours)


async def _settle_escalation(
    session: AsyncSession, chat: _Chat, *, now: datetime
) -> None:
    """Close the chat's open escalation when it no longer holds the chat: the
    owner wrote in the chat after it opened (resolved, ``auto:own``: the
    userbot's signal does it at once, JV-17, and this catches a message
    stored without one), or an ``open`` one (or one the owner said on its
    card they would write, JV-18) is older than JARVIS_DRAFT_EXPIRY_HOURS
    (expired). Otherwise one model failure or
    tripwire would mute the chat for good. One opened in shadow mode, which
    the owner was never shown, closes (``mode_changed``) as soon as the chat
    is in an alerting mode: a new push then opens one the cards alert,
    instead of becoming a repeat nobody sees. Contained: a failure is logged
    with the ids and the escalation is left as it was."""
    if chat.owner_at is not None:
        resolved = await chat_state.resolve_escalation(
            session, chat.chat_key, owner_at=chat.owner_at, now=now
        )
        if resolved is not None:
            return
    escalation_id: int | None = None
    try:
        async with session.begin_nested():
            escalation = await _open_escalation(session, chat.chat_key)
            if escalation is None:
                return
            escalation_id = escalation.id
            old = escalation.state
            switched = _opened_unseen(escalation) and chat.mode in ALERT_MODES
            if not switched and (
                old not in EXPIRING or escalation.created_at > now - escalation_expiry()
            ):
                return
            code = MODE_CHANGED if switched else EXPIRED
            history = [
                *(escalation.history or []),
                _entry("state", EXPIRED, now=now, code=code, old=old, by=BY_EXPIRY),
            ]
            await session.execute(
                sa.update(Escalation)
                .where(Escalation.id == escalation.id)
                .values(
                    state=EXPIRED,
                    resolution=EXPIRED,
                    history=history,
                    updated_at=sa.func.now(),
                )
            )
            await session.flush()
            log.info(
                "escalation settled",
                extra={
                    "chat_key": chat.chat_key,
                    "escalation_id": escalation_id,
                    "code": code,
                },
            )
    except Exception as exc:
        log.error(
            "escalation not settled",
            extra={
                "chat_key": chat.chat_key,
                "escalation_id": escalation_id,
                "interaction_id": chat.trigger.id,
                "error": type(exc).__name__,
            },
        )


async def _escalate(
    session: AsyncSession,
    chat: _Chat,
    reasons: Iterable[str],
    *,
    code: str,
    stall: bool,
    now: datetime,
    detail: Mapping[str, object] | None = None,
    cost_capped: bool = False,
    stall_for: Sequence[str] = (),
) -> Outcome:
    """Open an escalation (or, while one is open, add this push to its
    history: no second stall) and queue a stall when ``stall``. The stall
    answers ``stall_for``, what the client asked (a price, a deadline); with
    nothing there it is the owner's default stall, as for a draft the guard
    refused. A chat read before its owner-first window ended
    (``chat.stall_after``) gets the stall scheduled for the window's end:
    the owner's own message before then cancels it."""
    wanted = _reasons(reasons)
    if chat.stall_after is not None:
        detail = {**dict(detail or {}), "stall_after": chat.stall_after.isoformat()}
    try:
        async with session.begin_nested():
            escalation = await _open_escalation(session, chat.chat_key)
            if escalation is not None:
                _repeat(escalation, chat, wanted, code=code, now=now)
                await session.flush()
                return Outcome(
                    chat.chat_key,
                    REPEAT,
                    escalation_id=escalation.id,
                    cost_capped=cost_capped,
                )
            escalation = Escalation(
                interaction_id=chat.trigger.id,
                person_id=chat.person_id,
                channel=chat.channel,
                chat_key=chat.chat_key,
                reasons=wanted,
                state=OPEN,
                history=[
                    _entry(
                        "state",
                        OPEN,
                        now=now,
                        code=code,
                        mode=chat.mode,
                        interaction_ids=chat.burst_ids,
                        **dict(detail or {}),
                    )
                ],
            )
            session.add(escalation)
            await session.flush()
    except IntegrityError:
        # Another run opened this chat's escalation first (one open per chat).
        async with session.begin_nested():
            escalation = await _open_escalation(session, chat.chat_key)
            if escalation is None:
                raise
            _repeat(escalation, chat, wanted, code=code, now=now)
            await session.flush()
        return Outcome(chat.chat_key, REPEAT, escalation_id=escalation.id)
    log.info(
        "escalation opened",
        extra={
            "chat_key": chat.chat_key,
            "escalation_id": escalation.id,
            "interaction_id": chat.trigger.id,
            "code": code,
            "reasons": ",".join(wanted),
        },
    )
    ids: tuple[int, ...] = ()
    if stall:
        row_id = await _queue_stall(
            session,
            chat,
            stall_for,
            escalation.id,
            now=now,
            not_before=chat.stall_after,
        )
        ids = (row_id,) if row_id is not None else ()
    return Outcome(
        chat.chat_key, code, ids, escalation_id=escalation.id, cost_capped=cost_capped
    )


async def _queue_stall(
    session: AsyncSession,
    chat: _Chat,
    reasons: Sequence[str],
    escalation_id: int,
    *,
    now: datetime,
    not_before: datetime | None = None,
) -> int | None:
    """The one stall of a new escalation: a fixed template (or the owner's
    override that passes the stall role) in the client's script and the
    owner's form of address for them. Never generated, never a model call.
    ``not_before`` holds it until the owner-first window ends."""
    honorific: str | None = None
    overrides: dict[str, str] = {}
    try:
        async with session.begin_nested():
            honorific = (await style.tone_for(session, chat.person_id)).honorific
            overrides = await style.stall_overrides(session)
    except Exception as exc:
        log.error(
            "stall tone unreadable",
            extra={"chat_key": chat.chat_key, "error": type(exc).__name__},
        )
    chosen = stalls.for_message(
        chat.text, reasons, honorific=honorific, overrides=overrides
    )
    return await _propose(
        session,
        chat,
        origin="stall",
        intent_name=STALL_INTENT,
        body=chosen.text,
        now=now,
        escalation_id=escalation_id,
        not_before=not_before,
    )


async def _propose(
    session: AsyncSession,
    chat: _Chat,
    *,
    origin: str,
    intent_name: str,
    body: str,
    now: datetime,
    escalation_id: int | None = None,
    kb_entry_ids: Sequence[int] = (),
    model_verdict: str | None = None,
    not_before: datetime | None = None,
) -> int | None:
    """``outbox.create``, contained: the row's id, or None when none was
    stored (the mode is off for this intent, or the write failed)."""
    try:
        async with session.begin_nested():
            row = await outbox.create(
                session,
                channel=chat.channel,
                chat_key=chat.chat_key,
                origin=origin,
                authored_by="ai",
                body=body,
                now=now,
                intent=intent_name,
                person_id=chat.person_id,
                trigger_interaction_id=chat.trigger.id,
                escalation_id=escalation_id,
                kb_entry_ids=list(kb_entry_ids),
                model_verdict=model_verdict,
                not_before=not_before,
            )
            return row.id
    except outbox.JarvisDisabled as exc:
        log.info(
            "no proposal: mode off",
            extra={"chat_key": chat.chat_key, "intent": intent_name, "code": str(exc)},
        )
    except Exception as exc:
        log.error(
            "proposal not stored",
            extra={
                "chat_key": chat.chat_key,
                "interaction_id": chat.trigger.id,
                "escalation_id": escalation_id,
                "error": type(exc).__name__,
            },
        )
    return None


# --- 4-7. one chat ----------------------------------------------------------------------


async def _kb_for(session: AsyncSession, text: str) -> tuple[draft.KbText, ...]:
    """The retrieved entries whose answer passes the reply role: a draft that
    quotes one verbatim is an approved ``kb`` shape and may go out on its
    own. An entry the reply role refuses (a digit, a promise-like verb) would
    only make a blocked draft, so it is not shown to the writer."""
    found: list[draft.KbText] = []
    for match in await kb.retrieve(session, text):
        if redlines.check_outbound(match.answer, "reply").passed:
            found.append(draft.KbText(id=match.id, answer=match.answer))
    return tuple(found)


async def _style_for(
    session: AsyncSession, chat: _Chat, intent_name: str
) -> ReplyStyle | None:
    try:
        async with session.begin_nested():
            return await style.for_reply(
                session, person_id=chat.person_id, text=chat.text, intent=intent_name
            )
    except Exception as exc:
        log.error(
            "style unreadable for a draft",
            extra={"chat_key": chat.chat_key, "error": type(exc).__name__},
        )
        return None


async def _load(
    session: AsyncSession, candidate: Candidate, *, now: datetime
) -> _Chat | Outcome | datetime:
    """The chat to answer, an outcome that ends the run for it, or the time
    the owner-first window ends."""
    key = candidate.chat_key
    if not settings.jarvis_enabled or policy.ceiling() == "off":
        return Outcome(key, DISABLED)
    # Read as stored now: the enqueue is a core upsert, so a state this session
    # read before would not show its new trigger.
    state = await session.get(JarvisChatState, key, populate_existing=True)
    tg_chat_id = (
        chatkey.tg_chat_id(key) if candidate.channel == chatkey.TELEGRAM else None
    )
    if state is None or tg_chat_id is None:
        return Outcome(key, GONE)
    policies = await policy.load(session)
    mode = policy.effective_mode(
        policies, candidate.channel, None, env_ceiling=policy.ceiling()
    )
    if mode == "off":
        return Outcome(key, DISABLED)
    facts = await outbox.chat_facts(
        session,
        chat_key=key,
        channel=candidate.channel,
        tg_chat_id=tg_chat_id,
        person_id=state.person_id,
    )
    rules = await policy.load_rules(
        session,
        chat_key=key,
        person_id=state.person_id,
        addresses=[key] if tg_chat_id > 0 else [],
    )
    refused = policy.ai_refusal(facts, rules, addressed=None)
    if refused is not None or facts.chat_type != "private":
        return Outcome(key, f"{INELIGIBLE}:{refused or facts.chat_type}"[:64])
    if state.last_inbound_at is None or not fresh(state.last_inbound_at, now):
        return Outcome(key, STALE)
    if state.ai_paused_until is not None and state.ai_paused_until > now:
        return Outcome(key, AI_PAUSED)
    owner_at, out_at = await _last_outgoing(session, tg_chat_id)
    if state.owner_replied_at is not None:
        owner_at = max(owner_at or state.owner_replied_at, state.owner_replied_at)
        out_at = max(out_at or state.owner_replied_at, state.owner_replied_at)
    if owner_at is not None and owner_at >= state.last_inbound_at:
        return Outcome(key, OWNER_REPLIED)
    if state.owner_seen_at is not None and state.owner_seen_at >= state.last_inbound_at:
        # The owner read the client's latest message (JV-17): theirs to answer.
        return Outcome(key, OWNER_SEEN)
    burst = await _burst(
        session,
        tg_chat_id=tg_chat_id,
        after_id=await _handled_upto(session, key, now),
        after_at=out_at,
        now=now,
    )
    if not burst:
        return Outcome(key, NOTHING)
    ready = burst[0].occurred_at + owner_first()
    composing = state.owner_composing_at
    typing = composing is not None and composing + gate.COMPOSING_HOLD > now
    if typing and composing is not None:
        # The owner is typing in the chat (JV-17): look again once they stop.
        ready = max(ready, composing + gate.COMPOSING_HOLD)
    stall_after: datetime | None = None
    if now < ready:
        if typing or mode not in ALERT_MODES or not escalates_at_once(burst):
            # Typing, the owner is on it already: no alert to interrupt them.
            return ready
        # The spec's «immediately» (JV-18 1a): a message only the owner may
        # answer is escalated, and alerted, as soon as the debounce ends; only
        # the client-facing stall waits for the owner-first window.
        stall_after = ready
    trigger = burst[-1]
    return _Chat(
        chat_key=key,
        channel=candidate.channel,
        tg_chat_id=tg_chat_id,
        person_id=trigger.person_id or state.person_id,
        trigger=trigger,
        burst=burst,
        mode=mode,
        text="\n".join(t for t in (message_text(m) for m in burst) if t),
        burst_ids=[m.id for m in burst],
        owner_at=owner_at,
        stall_after=stall_after,
    )


def escalates_at_once(burst: Sequence[Interaction]) -> bool:
    """Does this burst go to the owner on a deterministic signal alone (a red
    line, an identity or complaint question, an injection tripwire), with no
    model call? Then its escalation need not wait for the owner-first window.
    A burst the shield flagged never escalates."""
    if any(unsafe(m) for m in burst):
        return False
    signals: set[redlines.Signal] = set()
    for message in burst:
        signals |= redlines.classify_inbound(message_text(message))
    return bool(redlines.escalation_reasons(signals)) or "injection" in signals


async def _decide(
    session: AsyncSession, chat: _Chat, state: JarvisChatState, *, now: datetime
) -> Outcome:
    key = chat.chat_key
    if any(unsafe(m) for m in chat.burst):
        # The shield's owner warning (JV-09) is the owner's news; never a reply.
        return Outcome(key, UNSAFE)
    signals: set[redlines.Signal] = set()
    for message in chat.burst:
        signals |= redlines.classify_inbound(message_text(message))
    escalating = redlines.escalation_reasons(signals)
    await _settle_escalation(session, chat, now=now)
    if await _open_escalation_id(session, key) is not None:
        # A second push while the owner decides: silence, the escalation's
        # history grows (§5.3: at most one stall per open escalation).
        return await _escalate(
            session, chat, escalating, code=REPEAT, stall=False, now=now
        )
    if (state.ai_turns_since_client or 0) >= settings.jarvis_max_ai_turns:
        return await _escalate(
            session, chat, escalating or [OTHER], code=AI_TURNS, stall=False, now=now
        )
    sent_today = await caps.chat_ai_sent_today(session, key, now)
    if sent_today >= settings.jarvis_per_chat_daily_cap:
        return await _escalate(
            session, chat, escalating or [OTHER], code=CHAT_CAP, stall=False, now=now
        )
    if "injection" in signals:
        # A tripwire: the owner reads it; no model reads it, no stall answers it.
        return await _escalate(
            session, chat, escalating or [OTHER], code=INJECTION, stall=False, now=now
        )
    if escalating:
        return await _escalate(
            session,
            chat,
            escalating,
            code=RED_LINE,
            stall=True,
            now=now,
            stall_for=escalating,
        )
    if await costs.over_cap(session, now):
        return await _escalate(
            session, chat, [OTHER], code=COST_CAP, stall=True, now=now, cost_capped=True
        )

    thread = envelope.render(await thread_turns(session, chat.tg_chat_id))
    try:
        found = await intent.classify(
            session,
            text=chat.text,
            thread=thread,
            now=now,
            source_interaction_id=chat.trigger.id,
        )
    except costs.CostCapReached:
        return await _escalate(
            session, chat, [OTHER], code=COST_CAP, stall=True, now=now, cost_capped=True
        )
    except (costs.ModelUnavailable, intent.IntentError) as exc:
        return await _needs_review(session, chat, "intent", exc, now=now)
    if found.escalates:
        return await _escalate(
            session,
            chat,
            [found.intent],
            code=INTENT_ESCALATES,
            stall=True,
            now=now,
            stall_for=[found.intent],
        )

    knowledge = (
        () if found.intent == intent.GREETING else await _kb_for(session, chat.text)
    )
    questions = (
        await outbox.owner_question_bank(session)
        if found.intent in intent.AUTO_ELIGIBLE
        else ()
    )
    inputs = draft.DraftInputs(
        thread=thread,
        intent=found.intent,
        kb=knowledge,
        style=await _style_for(session, chat, found.intent),
        questions=questions,
    )
    try:
        written = await draft.write(
            session, inputs, now=now, source_interaction_id=chat.trigger.id
        )
    except costs.CostCapReached:
        return await _escalate(
            session, chat, [OTHER], code=COST_CAP, stall=True, now=now, cost_capped=True
        )
    except (costs.ModelUnavailable, draft.DraftError) as exc:
        return await _needs_review(session, chat, "draft", exc, now=now)
    if written.needs_owner:
        asked = [written.reason_code] if written.reason_code != draft.NO_REASON else []
        return await _escalate(
            session,
            chat,
            asked,
            code=NEEDS_OWNER,
            stall=True,
            now=now,
            stall_for=[r for r in asked if r != OTHER],
        )

    kb_ids = [k.id for k in knowledge]
    verdict = await outbox.preview(
        session,
        channel=chat.channel,
        chat_key=key,
        origin="auto_reply",
        authored_by="ai",
        body=written.reply,
        now=now,
        person_id=chat.person_id,
        trigger_interaction_id=chat.trigger.id,
        kb_entry_ids=kb_ids,
    )
    if not verdict.passed:
        # The deterministic guard is binding: the model guard is not asked.
        return await _escalate(
            session,
            chat,
            _guard_reasons(verdict.flags),
            code=GUARD_BLOCK,
            stall=True,
            now=now,
            detail={"det_hits": list(verdict.det_hits)[:20]},
        )
    checked = await guard.check(
        session,
        reply=written.reply,
        thread=thread,
        now=now,
        source_interaction_id=chat.trigger.id,
    )
    if checked.failed and checked.code == guard.COST_CAP:
        # The guard's call went over the cap: the same stop as the intent and
        # the draft steps, so the owner is told drafting stopped.
        return await _escalate(
            session, chat, [OTHER], code=COST_CAP, stall=True, now=now, cost_capped=True
        )
    if checked.suspicious:
        return await _escalate(
            session,
            chat,
            [OTHER],
            code=SUSPICIOUS,
            stall=False,
            now=now,
            detail={"thread": checked.thread},
        )
    if not checked.passed:
        return await _escalate(
            session,
            chat,
            [OTHER],
            code=MODEL_BLOCK,
            stall=True,
            now=now,
            detail={"model_verdict": checked.stored},
        )
    row_id = await _propose(
        session,
        chat,
        origin="auto_reply",
        intent_name=found.intent,
        body=written.reply,
        now=now,
        kb_entry_ids=kb_ids,
        model_verdict=checked.stored,
    )
    if row_id is None:
        return Outcome(key, ERROR)
    log.info(
        "reply drafted",
        extra={"chat_key": key, "outbox_id": row_id, "intent": found.intent},
    )
    return Outcome(key, DRAFTED, (row_id,))


async def _open_escalation_id(session: AsyncSession, chat_key: str) -> int | None:
    found: int | None = await session.scalar(
        sa.select(Escalation.id).where(
            Escalation.chat_key == chat_key,
            Escalation.state.in_(ESCALATION_OPEN_STATES),
        )
    )
    return found


async def _needs_review(
    session: AsyncSession, chat: _Chat, step: str, exc: Exception, *, now: datetime
) -> Outcome:
    """A model failure (§3.3, rule 3): nothing is sent and nothing is
    dropped; the chat goes to the owner, the escalation's history says
    ``needs_review`` and which step failed."""
    log.warning(
        "responder model step failed",
        extra={
            "chat_key": chat.chat_key,
            "interaction_id": chat.trigger.id,
            "step": step,
            "error": type(exc).__name__,
        },
    )
    return await _escalate(
        session,
        chat,
        [OTHER],
        code=NEEDS_REVIEW,
        stall=False,
        now=now,
        detail={"step": step, "error": type(exc).__name__[:64]},
    )


async def respond(
    session: AsyncSession, candidate: Candidate, *, now: datetime
) -> Outcome:
    """Read one claimed chat and store what it gets: a proposal, an
    escalation (with a stall), or nothing. The lease is released at the end
    (or moved to when the owner-first window ends)."""
    loaded = await _load(session, candidate, now=now)
    if isinstance(loaded, datetime):
        await _finish(session, candidate, until=loaded)
        return Outcome(candidate.chat_key, WAITING)
    if isinstance(loaded, Outcome):
        await _finish(session, candidate)
        log.info(
            "no reply for a chat",
            extra={"chat_key": candidate.chat_key, "code": loaded.code},
        )
        return loaded
    state = await session.get(JarvisChatState, candidate.chat_key)
    if state is None:
        return Outcome(candidate.chat_key, GONE)
    try:
        # A savepoint: whatever fails inside leaves the session usable, so
        # the lease is released below and the model calls are not repeated.
        async with session.begin_nested():
            outcome = await _decide(session, loaded, state, now=now)
    except Exception as exc:
        # Contained (rule 25): the chat is let go so a failure never repeats
        # its model calls; the error and the ids are in the log.
        log.error(
            "responder failed for one chat",
            extra={
                "chat_key": candidate.chat_key,
                "interaction_id": loaded.trigger.id,
                "interaction_ids": loaded.burst_ids[:20],
                "error": type(exc).__name__,
            },
        )
        outcome = Outcome(candidate.chat_key, ERROR)
    await _finish(session, candidate)
    return outcome


@dataclass(frozen=True, slots=True)
class RunResult:
    claimed: int
    outcomes: tuple[Outcome, ...]

    @property
    def cost_capped(self) -> bool:
        return any(o.cost_capped for o in self.outcomes)


async def run(
    *,
    now: datetime,
    limit: int = BATCH,
    alert: Callable[[Outcome], Awaitable[object]] | None = None,
) -> RunResult:
    """One ``jarvis_respond`` tick: claim the due chats, then read each in
    a transaction of its own. One chat that fails is logged with its ids and
    the others go on (rule 25).

    ``alert`` gets each chat's outcome as soon as that chat's transaction
    committed (JV-18 1a: the owner's card or escalation alert goes out in the
    same run, never after the other chats' model calls). It is contained: a
    failure is logged with the ids and the run goes on; the card sweeper
    (``jarvis_cards``) retries whatever it did not deliver."""
    started = _monotonic()
    async with session_scope() as session:
        candidates = await claim_due(session, now=now, limit=limit)
    outcomes: list[Outcome] = []
    for candidate in candidates:
        # The model calls of earlier chats take real time: each chat is read
        # at its own clock, so the age limit and the day's caps stay true.
        chat_now = now + timedelta(seconds=_monotonic() - started)
        if chat_now >= candidate.lease:
            # The lease ran out while earlier chats were read: the next tick
            # claims it again.
            log.info(
                "reply candidate left for the next run",
                extra={
                    "chat_key": candidate.chat_key,
                    "interaction_id": candidate.interaction_id,
                },
            )
            continue
        try:
            async with session_scope() as session:
                outcome = await respond(session, candidate, now=chat_now)
            outcomes.append(outcome)
        except Exception as exc:
            log.error(
                "responder failed for one chat",
                extra={
                    "chat_key": candidate.chat_key,
                    "interaction_id": candidate.interaction_id,
                    "error": type(exc).__name__,
                },
            )
            continue
        if alert is not None:
            await _alert(alert, outcome)
    return RunResult(claimed=len(candidates), outcomes=tuple(outcomes))


async def _alert(alert: Callable[[Outcome], Awaitable[object]], outcome: Outcome) -> None:
    try:
        await alert(outcome)
    except Exception as exc:
        log.error(
            "owner alert failed for one chat",
            extra={
                "chat_key": outcome.chat_key,
                "escalation_id": outcome.escalation_id,
                "outbox_ids": list(outcome.outbox_ids),
                "error": type(exc).__name__,
            },
        )


__all__ = [
    "BATCH",
    "CLAIM_LEASE",
    "Candidate",
    "Outcome",
    "RunResult",
    "claim_due",
    "enqueue_candidate",
    "escalates_at_once",
    "fresh",
    "message_text",
    "respond",
    "run",
    "thread_turns",
    "unsafe",
]
