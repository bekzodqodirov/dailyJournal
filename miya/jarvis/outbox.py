"""The one outbound chokepoint (docs/jarvis-plan.md §3.2, rule 12).

Nothing reaches any of the owner's accounts except as a row in
``outbox_messages``, and this module is the only code that creates those
rows or changes their state (an AST test holds every other module to it).
An adapter — the Telegram sender (JV-19), the WhatsApp bridge claim (JV-30),
``gmail_send`` (JV-27) — gets a row only through ``claim_ready``, which
re-runs the whole gate (``gate.evaluate``) inside one ``SKIP LOCKED``
transaction, then does its network I/O with no transaction held, and
reports back with ``complete`` or ``fail``. A lease that runs out with no
report goes to ``reconcile``: an echo found means sent, none means
``unknown`` and the owner is told; nothing is ever resent blindly.

The lifecycle::

    create ─▶ shadow | draft | approved (auto intents with shapes_ok only) | blocked
    draft ──approve──▶ approved ──claim_ready──▶ sending ──complete──▶ sent
                          │  ▲                     │   └──fail──▶ failed | approved
                          │  └──── hold ───────────┘   └──reconcile──▶ sent | unknown
                          └─▶ draft (redraft) · blocked · expired · cancelled

An AI row is approved on its own (``auto:policy``) only when its intent
graduated and its bound verdict carries ``shapes_ok`` (the statement
allowlist, rule 13a): every sentence a courtesy, a question, a stall or an
approved KB text. A text with a free-form statement becomes a draft card
(``unapproved_statement``) instead; ``gate.evaluate`` holds the same line at
the claim.

Every transition appends ``{at, field, old, new, by}`` to ``history``.
Functions take the caller's session and flush; the caller commits. Logs
carry ids and codes only, never a body (rule 23).
"""

from __future__ import annotations

import hashlib
import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import (
    ChatMonitor,
    Interaction,
    JarvisChatState,
    OutboxMessage,
    Person,
)
from miya.jarvis import caps, chatkey, costs, gate, policy, redlines, style
from miya.services import authorship, codes, people, reminders, safety

log = logging.getLogger(__name__)

# The red-line guard (JV-13): every row this module creates, approves or
# lets an adapter claim is checked by it. Registered here, where the rows
# are, so no path to a send exists without it.
redlines.install()

SHADOW: Final = "shadow"
DRAFT: Final = "draft"
APPROVED: Final = "approved"
SENDING: Final = "sending"
SENT: Final = "sent"
FAILED: Final = "failed"
UNKNOWN: Final = "unknown"
CANCELLED: Final = "cancelled"
EXPIRED: Final = "expired"
BLOCKED: Final = "blocked"

# Rows that have not left yet and may still be cancelled or expired.
PENDING: Final = (SHADOW, DRAFT, APPROVED)

# The only state changes there are (§3.2). "new" is a row being created.
TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "new": frozenset({SHADOW, DRAFT, APPROVED, BLOCKED}),
    SHADOW: frozenset({CANCELLED, EXPIRED}),
    DRAFT: frozenset({APPROVED, BLOCKED, CANCELLED, EXPIRED}),
    APPROVED: frozenset({SENDING, DRAFT, BLOCKED, CANCELLED, EXPIRED}),
    SENDING: frozenset({SENT, FAILED, APPROVED, UNKNOWN}),
    UNKNOWN: frozenset({SENT, FAILED}),
    SENT: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    EXPIRED: frozenset(),
    BLOCKED: frozenset(),
}

# history ``by`` values (String(16)).
BY_GATE: Final = "gate"
BY_ADAPTER: Final = "adapter"
BY_RECONCILE: Final = "reconcile"
BY_EXPIRY: Final = "auto:expiry"
BY_POLICY: Final = "auto:policy"
BY_OWN: Final = "auto:own"
BY_COMMAND: Final = "command"

AUTO_APPROVER: Final = gate.AUTO_APPROVER
# create() takes no approved statements from its caller (rule 13a): see there.
APPROVED_TEXTS_REFUSED: Final = "approved_texts_unverified"
# approve() is the owner's tap: the autonomous approver is create()'s alone.
BAD_APPROVER: Final = "bad_approver"
ORIGINS: Final = ("auto_reply", "stall", "owner_command", "escalation")
AUTHORS: Final = (gate.AI, gate.OWNER)

# A held row with no end in sight (a kill switch, an open-ended pause) is
# looked at again after this long, so a busy queue is not rescanned every
# few seconds.
HOLD_POLL: Final = timedelta(minutes=1)
# A transient adapter failure is retried at most this many claims.
MAX_ATTEMPTS: Final = 5
# How far back the inbound messages that may lend a code to AI text reach.
TOKEN_LOOKBACK: Final = timedelta(days=30)
TOKEN_MESSAGES: Final = 200
# The safety window of a row without a trigger (an owner command).
UNTRIGGERED_SAFETY_LOOKBACK: Final = timedelta(hours=24)
# How far before the claim an untagged echo may have been stored.
ECHO_SLACK: Final = timedelta(minutes=2)

# Pacing defaults, the plan's values for the JV-19 settings
# (JARVIS_REPLY_DELAY_MIN/MAX_SECONDS, JARVIS_TYPING_CHARS_PER_SECOND,
# JARVIS_TYPING_MAX_SECONDS) until those keys exist.
DELAY_MIN_SECONDS: Final = 3.0
DELAY_MAX_SECONDS: Final = 7.0
TYPING_CHARS_PER_SECOND: Final = 6
TYPING_MAX_SECONDS: Final = 15

# The send switch and the platform's daily cap of each channel. The keys
# land with their adapters (TELEGRAM_SEND_ENABLED in JV-19,
# GMAIL_SEND_ENABLED and GMAIL_DAILY_SEND_CAP in JV-27, WHATSAPP_* in JV-30);
# a channel whose switch does not exist yet is off.
SEND_SWITCHES: Final[Mapping[str, str]] = {
    chatkey.TELEGRAM: "telegram_send_enabled",
    chatkey.WHATSAPP: "whatsapp_send_enabled",
    chatkey.GMAIL: "gmail_send_enabled",
}
CHANNEL_CAPS: Final[Mapping[str, str]] = {
    chatkey.WHATSAPP: "whatsapp_daily_send_cap",
    chatkey.GMAIL: "gmail_daily_send_cap",
}
AUTO_IN_QUIET_HOURS: Final = "jarvis_auto_in_quiet_hours"

# Where a sent message's id is kept on its echo interaction.
ECHO_ID_KEYS: Final[Mapping[str, str]] = {
    chatkey.TELEGRAM: "tg_message_id",
    chatkey.WHATSAPP: "wa_message_id",
    chatkey.GMAIL: "gmail_message_id",
}
# reply_rules.address prefixes (channel-qualified snapshots).
ADDRESS_PREFIX: Final[Mapping[str, str]] = {
    chatkey.TELEGRAM: "tg",
    chatkey.WHATSAPP: "wa",
    chatkey.GMAIL: "mail",
}
# JV-17 marks a client message the client deleted.
DELETED_KEY: Final = "deleted_at"
# The advisory lock that serialises claims (hashtext of this text).
CLAIM_LOCK: Final = "miya:outbox-claim"


class OutboxError(Exception):
    """Base for the refusals; the message is a code, never text."""


class AlreadyAnswered(OutboxError):
    """A second tap: the row is no longer waiting for the owner."""


class IllegalTransition(OutboxError):
    """A state change §3.2 does not have."""


class JarvisDisabled(OutboxError):
    """JARVIS_ENABLED is false, or the mode is off: nothing is created."""


class NotPlainText(OutboxError):
    """Rule 27: an outbox row carries a text body and nothing else."""


def channel_send_enabled(channel: str) -> bool:
    key = SEND_SWITCHES.get(channel)
    return bool(getattr(settings, key, False)) if key else False


def channel_daily_cap(channel: str) -> int | None:
    key = CHANNEL_CAPS.get(channel)
    value = getattr(settings, key, None) if key else None
    return value if isinstance(value, int) else None


def idempotency_key(
    *, origin: str, interaction_id: int | None, chat_key: str, body_sha256: str
) -> str:
    """One row per (origin, trigger or command message, chat, exact body)."""
    material = f"{origin}|{interaction_id if interaction_id is not None else '-'}|"
    return hashlib.sha256(f"{material}{chat_key}|{body_sha256}".encode()).hexdigest()


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def _note(
    row: OutboxMessage,
    field: str,
    old: object,
    new: object,
    *,
    by: str,
    now: datetime,
    code: str | None = None,
) -> None:
    entry: dict[str, object] = {
        "at": now.isoformat(),
        "field": field,
        "old": _iso(old),
        "new": _iso(new),
        "by": by,
    }
    if code is not None:
        entry["code"] = code
    row.history = [*(row.history or []), entry]


def _transition(
    row: OutboxMessage, new: str, *, by: str, now: datetime, code: str | None = None
) -> None:
    old = row.state
    if new not in TRANSITIONS.get(old, frozenset()):
        raise IllegalTransition(f"{old}->{new}")
    row.state = new
    row.last_error = code
    _note(row, "state", None if old == "new" else old, new, by=by, now=now, code=code)


def _bind(row: OutboxMessage, result: gate.GuardResult) -> None:
    sha = gate.body_hash(row.body)
    row.body_sha256 = sha
    row.guard = result.bound_to(sha)


def _log_guard_error(row: OutboxMessage, result: gate.GuardResult) -> None:
    """A guard that raised is a block (gate.run_guard); say so, with ids only."""
    if gate.GUARD_ERROR in result.flags:
        log.warning(
            "red-line guard raised",
            extra={
                "outbox_id": row.id,
                "chat_key": row.chat_key,
                "error": ",".join(result.det_hits),
            },
        )


def _bound(row: OutboxMessage) -> bool:
    sha = gate.body_hash(row.body)
    return row.body_sha256 == sha and (row.guard or {}).get("body_sha256") == sha


async def _locked(session: AsyncSession, row_id: int) -> OutboxMessage | None:
    row: OutboxMessage | None = await session.scalar(
        sa.select(OutboxMessage)
        .where(OutboxMessage.id == row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row


# --- facts the gate reads ----------------------------------------------------------


async def chat_facts(
    session: AsyncSession,
    *,
    chat_key: str,
    channel: str,
    tg_chat_id: int | None,
    person_id: int | None,
) -> policy.ChatFacts:
    chat_type: str | None = None
    is_bot = False
    is_self = False
    owner_id = settings.owner_telegram_id
    if channel == chatkey.TELEGRAM and tg_chat_id is not None:
        monitor = await session.scalar(
            sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == tg_chat_id)
        )
        if monitor is not None:
            chat_type = monitor.chat_type.value
        else:
            # Never seen: a positive id is a user, a negative one a group or
            # channel (treated as a group: no AI reply unless enabled).
            chat_type = "private" if tg_chat_id > 0 else "group"
        is_self = owner_id is not None and tg_chat_id == owner_id
    is_owner = False
    if person_id is not None:
        person = await session.get(Person, person_id)
        if person is not None:
            is_bot = policy.looks_like_bot(person.telegram_username)
            # The owner under one of their own names (§5.4), by account or
            # by an alias the owner set (OWNER_ALIASES).
            is_owner = (
                owner_id is not None and person.telegram_id == owner_id
            ) or people.is_owner_alias(person.display_name)
    return policy.ChatFacts(
        chat_key=chat_key,
        channel=channel,
        tg_chat_id=tg_chat_id,
        chat_type=chat_type,
        is_bot=is_bot,
        is_self=is_self,
        is_owner=is_owner,
    )


def _addresses(channel: str, tg_chat_id: int | None, to_address: str | None) -> list[str]:
    found: list[str] = []
    if channel == chatkey.TELEGRAM and tg_chat_id is not None and tg_chat_id > 0:
        found.append(f"tg:{tg_chat_id}")
    if to_address and channel != chatkey.TELEGRAM:
        prefix = ADDRESS_PREFIX.get(channel)
        if prefix:
            found.append(f"{prefix}:{to_address.strip().casefold()}")
    return found


async def _rules(
    session: AsyncSession, row: OutboxMessage
) -> tuple[policy.RuleSnapshot, ...]:
    return await policy.load_rules(
        session,
        chat_key=row.chat_key,
        person_id=row.person_id,
        addresses=_addresses(row.channel, row.tg_chat_id, row.to_address),
    )


def _edited_at(meta: Mapping[str, Any]) -> datetime | None:
    latest: datetime | None = None
    edits = meta.get("edits")
    for edit in edits if isinstance(edits, list) else []:
        at = edit.get("at") if isinstance(edit, dict) else None
        try:
            moment = datetime.fromisoformat(at) if isinstance(at, str) else None
        except ValueError:
            moment = None
        if moment is not None and (latest is None or moment > latest):
            latest = moment
    return latest


async def _trigger(session: AsyncSession, row: OutboxMessage) -> gate.Trigger | None:
    if row.trigger_interaction_id is None:
        return None
    interaction = await session.get(Interaction, row.trigger_interaction_id)
    if interaction is None:
        return None
    meta = interaction.meta if isinstance(interaction.meta, dict) else {}
    return gate.Trigger(
        id=interaction.id,
        occurred_at=interaction.occurred_at,
        edited_at=_edited_at(meta),
        deleted=bool(meta.get(DELETED_KEY)),
        to_me=meta.get("to_me") is True,
    )


def _risky(media: object) -> bool:
    risk = media.get("risk") if isinstance(media, dict) else None
    return isinstance(risk, dict) and risk.get("kind") not in (None, safety.OK)


def safety_readable(channel: str, tg_chat_id: int | None) -> bool:
    """Whether ``unsafe_inbound`` can read this chat's shield verdicts.

    Only Telegram chats can be read today. A WhatsApp or Gmail chat fails
    closed (``gate._safety``) until its send WP (JV-30, JV-27) reads the
    verdicts through ``wa_chat_id`` / ``email_thread_id``.
    """
    return channel == chatkey.TELEGRAM and tg_chat_id is not None


async def unsafe_inbound(
    session: AsyncSession, row: OutboxMessage, trigger: gate.Trigger | None
) -> tuple[int, ...]:
    """The trigger and later inbound messages of this chat that the shield
    flagged (spam, scam, hijack, a dangerous file) or that carried a risky
    file (JV-01, JV-09)."""
    if row.tg_chat_id is None:
        return ()
    since = (
        trigger.occurred_at
        if trigger is not None
        else row.created_at - UNTRIGGERED_SAFETY_LOOKBACK
    )
    found = await session.execute(
        sa.select(Interaction.id, Interaction.meta, Interaction.media).where(
            Interaction.tg_chat_id == row.tg_chat_id,
            Interaction.direction == Direction.in_,
            Interaction.occurred_at >= since,
            sa.or_(Interaction.meta.has_key("safety"), Interaction.media.has_key("risk")),
        )
    )
    return tuple(
        sorted(
            interaction_id
            for interaction_id, meta, media in found
            if safety.blocks_reply(meta if isinstance(meta, dict) else None)
            or _risky(media)
        )
    )


async def allowed_tokens(session: AsyncSession, row: OutboxMessage) -> frozenset[str]:
    """The GS codes and waybills written in this chat's inbound messages, as
    the client wrote them: the only digits AI text may carry (rule 14). The
    guard compares their digits verbatim, so no canonical form (leading
    zeros stripped) is added here."""
    if row.tg_chat_id is None:
        return frozenset()
    texts = await session.execute(
        sa.select(Interaction.raw_text, Interaction.transcript)
        .where(
            Interaction.tg_chat_id == row.tg_chat_id,
            Interaction.direction == Direction.in_,
            Interaction.occurred_at >= row.created_at - TOKEN_LOOKBACK,
        )
        .order_by(Interaction.occurred_at.desc())
        .limit(TOKEN_MESSAGES)
    )
    tokens: set[str] = set()
    for raw_text, transcript in texts:
        for text in (raw_text, transcript):
            if not text:
                continue
            for pattern in (codes.client_code_re(), codes.waybill_re()):
                tokens.update(match.group(0).strip() for match in pattern.finditer(text))
    return frozenset(t for t in tokens if t)


@dataclass(frozen=True, slots=True)
class GuardNames:
    """What §5.2 step 2 checks besides the red lines: the names an AI text
    may carry (the counterparty's, the owner's aliases), the names it may not
    (every other person MIYA knows), and the script the client wrote in."""

    allowed: tuple[str, ...] = ()
    others: tuple[str, ...] = ()
    client_script: str | None = None


async def guard_names(session: AsyncSession, row: OutboxMessage) -> GuardNames:
    """The names and the script the red-line guard checks ``row`` against.

    The owner's own words carry what they like (role ``owner`` checks
    neither). A stall is a fixed template or the owner's override of one,
    never a name the model chose, so only an AI reply is checked for other
    people's names; every AI row is checked for the client's script."""
    if row.authored_by != gate.AI:
        return GuardNames()
    allowed = list(settings.owner_aliases_parsed)
    if row.person_id is not None:
        person = await session.get(Person, row.person_id)
        if person is not None:
            allowed.extend([person.display_name, *(person.aliases or [])])
    others: list[str] = []
    if gate.guard_role(row.origin, row.authored_by) == "reply":
        query = sa.select(Person.display_name, Person.aliases)
        if row.person_id is not None:
            query = query.where(Person.id != row.person_id)
        for display_name, aliases in await session.execute(query):
            others.extend(n for n in (display_name, *(aliases or [])) if n)
    script = None
    if row.trigger_interaction_id is not None:
        trigger = await session.get(Interaction, row.trigger_interaction_id)
        if trigger is not None:
            script = redlines.script_of(trigger.raw_text or trigger.transcript or "")
    return GuardNames(
        tuple(n for n in allowed if n), tuple(dict.fromkeys(others)), script
    )


def _run_guard(
    row: OutboxMessage,
    tokens: frozenset[str],
    names: GuardNames,
    *,
    approved_kb: Sequence[str] = (),
    stall_texts: Sequence[str] = (),
) -> gate.GuardResult:
    return gate.run_guard(
        gate.registered_guard(),
        row.body,
        role=gate.guard_role(row.origin, row.authored_by),
        allowed_tokens=tokens,
        allowed_names=names.allowed,
        other_names=names.others,
        client_script=names.client_script,
        approved_kb=tuple(t for t in approved_kb if isinstance(t, str)),
        stall_texts=tuple(t for t in stall_texts if isinstance(t, str)),
    )


async def owner_stall_texts(session: AsyncSession) -> tuple[str, ...]:
    """The owner's stall overrides in force (JV-14): the active style
    profile's, each passing the stall role. They are read from the owner's
    own records here, never taken from a caller. A failure to read them is
    logged and counts as none (an auto row that leaned on one goes back to
    the card); it never raises into a create, an approval or a claim."""
    try:
        async with session.begin_nested():
            return await style.stall_texts(session)
    except Exception as exc:
        log.error("stall overrides unreadable", extra={"error": type(exc).__name__})
        return ()


async def _chat_state(session: AsyncSession, chat_key: str) -> gate.ChatState | None:
    state = await session.get(JarvisChatState, chat_key)
    if state is None:
        return None
    return gate.ChatState(
        owner_seen_at=state.owner_seen_at,
        owner_composing_at=state.owner_composing_at,
        owner_replied_at=state.owner_replied_at,
        ai_paused_until=state.ai_paused_until,
    )


def quiet_until(now: datetime) -> datetime | None:
    """The end of the quiet hours ``now`` is inside, else None."""
    # On the owner's clock: a moment read back from the database is in UTC,
    # and in_quiet_hours compares the wall time it is given.
    local = now.astimezone(settings.tz)
    if not reminders.in_quiet_hours(local):
        return None
    end = settings.quiet_hours_parsed[1]
    candidate = datetime.combine(local.date(), end, tzinfo=settings.tz)
    return candidate if candidate > now else candidate + timedelta(days=1)


def snapshot(row: OutboxMessage) -> gate.Row:
    return gate.Row(
        id=row.id,
        channel=row.channel,
        chat_key=row.chat_key,
        origin=row.origin,
        authored_by=row.authored_by,
        body=row.body,
        body_sha256=row.body_sha256,
        guard=dict(row.guard or {}),
        created_at=row.created_at,
        intent=row.intent,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        not_before=row.not_before,
        expires_at=row.expires_at,
        trigger_interaction_id=row.trigger_interaction_id,
        history=tuple(e for e in (row.history or []) if isinstance(e, dict)),
    )


# --- create ------------------------------------------------------------------------


async def create(
    session: AsyncSession,
    *,
    channel: str,
    chat_key: str,
    origin: str,
    authored_by: str,
    body: str,
    now: datetime,
    intent: str | None = None,
    person_id: int | None = None,
    to_address: str | None = None,
    trigger_interaction_id: int | None = None,
    command_interaction_id: int | None = None,
    escalation_id: int | None = None,
    reply_to_external_id: str | None = None,
    not_before: datetime | None = None,
    by: str | None = None,
    approved_kb: Sequence[str] = (),
    stall_texts: Sequence[str] = (),
) -> OutboxMessage:
    """Store a proposal: shadow, draft, approved (a graduated auto intent
    whose text passed the statement allowlist only) or blocked. The same
    proposal twice is the same row.

    ``approved_kb`` are the active owner-approved KB texts the draft was
    written from (JV-15) and ``stall_texts`` the owner's stall overrides
    (JV-14): verbatim, they are approved statements; they never loosen a
    red line. Texts a caller hands in are never trusted as approved: the
    outbox loads the owner's stall overrides itself from the active
    ``owner_style_profiles`` row (``owner_stall_texts``), and until JV-15
    loads the KB entries by id, any caller text is refused, and so is a
    bare string (it would be read one character a text)."""
    if isinstance(approved_kb, str) or isinstance(stall_texts, str):
        raise OutboxError(APPROVED_TEXTS_REFUSED)
    if approved_kb or stall_texts:
        # The outbox resolves the approved texts from the owner's own
        # records, and the claim rechecks them still in force.
        raise OutboxError(APPROVED_TEXTS_REFUSED)
    if not settings.jarvis_enabled:
        raise JarvisDisabled("jarvis_disabled")
    if policy.ceiling() == "off":
        raise JarvisDisabled("mode_off")
    if origin not in ORIGINS or authored_by not in AUTHORS:
        raise OutboxError("bad_row")
    if authored_by == gate.AI and trigger_interaction_id is None:
        # Rule 17: the AI only ever answers a client message.
        raise OutboxError(gate.NO_TRIGGER)
    if trigger_interaction_id is None and command_interaction_id is None:
        # The idempotency key needs the message the row comes from; without
        # one, the same short text a day later would be taken for this row.
        raise OutboxError("no_source_interaction")
    if chatkey.channel_of(chat_key) != channel:
        raise OutboxError("chat_key_channel")
    if not gate.plain_text(body):
        raise NotPlainText(gate.PAYLOAD_NOT_TEXT)
    tg_chat_id = chatkey.tg_chat_id(chat_key)
    sha = gate.body_hash(body)
    key = idempotency_key(
        origin=origin,
        interaction_id=(
            trigger_interaction_id
            if trigger_interaction_id is not None
            else command_interaction_id
        ),
        chat_key=chat_key,
        body_sha256=sha,
    )
    existing = await _by_key(session, key)
    if existing is not None:
        return existing

    ai = authored_by == gate.AI
    facts = await chat_facts(
        session,
        chat_key=chat_key,
        channel=channel,
        tg_chat_id=tg_chat_id,
        person_id=person_id,
    )
    row = OutboxMessage(
        channel=channel,
        chat_key=chat_key,
        tg_chat_id=tg_chat_id,
        to_address=to_address,
        person_id=person_id,
        trigger_interaction_id=trigger_interaction_id,
        command_interaction_id=command_interaction_id,
        escalation_id=escalation_id,
        reply_to_external_id=reply_to_external_id,
        origin=origin,
        intent=intent,
        authored_by=authored_by,
        body=body,
        ai_body=body if ai else None,
        body_sha256=sha,
        guard={},
        state="new",
        not_before=not_before,
        expires_at=(not_before or now)
        + timedelta(hours=settings.jarvis_draft_expiry_hours),
        attempts=0,
        idempotency_key=key,
        history=[],
        created_at=now,
    )
    rules = await _rules(session, row)
    result = _run_guard(
        row,
        await allowed_tokens(session, row),
        await guard_names(session, row),
        approved_kb=approved_kb,
        stall_texts=await owner_stall_texts(session) if ai else (),
    )
    _bind(row, result)
    state, code = await _initial_state(
        session, row=row, facts=facts, rules=rules, guard=result, now=now
    )
    if state == APPROVED:
        row.approved_by = AUTO_APPROVER
        row.approved_at = now
    _transition(
        row, state, by=by or (BY_POLICY if ai else BY_COMMAND), now=now, code=code
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # The same proposal from a concurrent run: that row is this one.
        again = await _by_key(session, key)
        if again is None:
            raise
        return again
    log.info(
        "outbox row created",
        extra={"outbox_id": row.id, "state": row.state, "code": code, "origin": origin},
    )
    # Logged once the row has its id (rule 25), and only for a row stored.
    _log_guard_error(row, result)
    return row


async def _by_key(session: AsyncSession, key: str) -> OutboxMessage | None:
    found: OutboxMessage | None = await session.scalar(
        sa.select(OutboxMessage).where(OutboxMessage.idempotency_key == key)
    )
    return found


async def _initial_state(
    session: AsyncSession,
    *,
    row: OutboxMessage,
    facts: policy.ChatFacts,
    rules: Sequence[policy.RuleSnapshot],
    guard: gate.GuardResult,
    now: datetime,
) -> tuple[str, str | None]:
    excluded = policy.hard_exclusion(facts)
    if row.authored_by == gate.OWNER:
        # The owner's words always wait for their tap on the exact text.
        return (BLOCKED, excluded) if excluded else (DRAFT, None)
    policies = await policy.load(session)
    mode = policy.effective_mode(
        policies, row.channel, row.intent, env_ceiling=policy.ceiling()
    )
    if mode == "off":
        raise JarvisDisabled("mode_off")
    refused = policy.ai_refusal(facts, rules, addressed=None)
    if refused is not None:
        return BLOCKED, refused
    if mode == "shadow":
        return SHADOW, None
    if not guard.passed:
        return BLOCKED, guard.verdict[:64]
    auto = (
        mode == "auto"
        and row.intent in gate.AUTO_INTENTS
        and policy.rule_mode(rules) != policy.RULE_DRAFT
        and not await costs.over_cap(session, now)
    )
    if auto and guard.shapes_ok:
        return APPROVED, None
    if await caps.draft_cap_reached(session, now):
        return DRAFT, caps.DRAFT_CAP_CODE
    if auto:
        # Rule 13a: a free-form statement is never sent on its own; the
        # owner reads it on a card first.
        return DRAFT, gate.UNAPPROVED_STATEMENT
    return DRAFT, None


# --- the owner's decisions ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approval:
    row: OutboxMessage
    approved: bool
    # Why not, when not: expired, jarvis_disabled, guard_unavailable, …
    code: str | None = None


async def approve(
    session: AsyncSession, row_id: int, *, by: str, now: datetime
) -> Approval:
    """The owner's tap on a draft card, under a row lock.

    A row that is no longer a draft raises ``AlreadyAnswered`` (the second
    tap). Everything is re-validated: an expired draft expires, AI text is
    approved only with a passing guard bound to its exact body, and a chat
    that became personal blocks the AI row. The CHECK
    ``ck_outbox_messages_guarded`` holds the same line in the database.
    """
    if by == AUTO_APPROVER:
        # Only create() approves on its own, and only a row with shapes_ok:
        # a draft card is a free-form statement waiting for the owner.
        raise OutboxError(BAD_APPROVER)
    row = await _locked(session, row_id)
    if row is None or row.state != DRAFT:
        raise AlreadyAnswered(str(row_id))
    if not settings.jarvis_enabled:
        return Approval(row, False, "jarvis_disabled")
    if row.expires_at is not None and row.expires_at <= now:
        _transition(row, EXPIRED, by=BY_EXPIRY, now=now, code="expired")
        await session.flush()
        return Approval(row, False, "expired")
    if row.authored_by == gate.AI:
        rules = await _rules(session, row)
        facts = await chat_facts(
            session,
            chat_key=row.chat_key,
            channel=row.channel,
            tg_chat_id=row.tg_chat_id,
            person_id=row.person_id,
        )
        refused = policy.ai_refusal(facts, rules, addressed=None)
        if refused:
            _transition(row, BLOCKED, by=by, now=now, code=refused)
            await session.flush()
            return Approval(row, False, refused)
        mode = policy.effective_mode(
            await policy.load(session),
            row.channel,
            row.intent,
            env_ceiling=policy.ceiling(),
        )
        if mode in ("off", "shadow"):
            return Approval(row, False, f"mode_{mode}")
        if not (_bound(row) and (row.guard or {}).get("verdict") == gate.GUARD_PASS):
            result = _run_guard(
                row,
                await allowed_tokens(session, row),
                await guard_names(session, row),
                stall_texts=await owner_stall_texts(session),
            )
            _bind(row, result)
            _log_guard_error(row, result)
            if not result.passed:
                code = result.verdict[:64]
                row.last_error = code
                _note(row, "guard", None, result.verdict, by=by, now=now, code=code)
                await session.flush()
                log.warning(
                    "outbox approval refused by the guard",
                    extra={"outbox_id": row.id, "code": code},
                )
                return Approval(row, False, code)
    elif not _bound(row):
        # The owner's own words: flagged, never blocked; bound to this body.
        owned = gate.run_guard(
            gate.registered_guard(),
            row.body,
            role="owner",
            allowed_tokens=await allowed_tokens(session, row),
        )
        _bind(row, owned)
        _log_guard_error(row, owned)
    _transition(row, APPROVED, by=by, now=now)
    row.approved_by = by
    row.approved_at = now
    row.next_attempt_at = None
    await session.flush()
    return Approval(row, True)


async def cancel(
    session: AsyncSession, row_id: int, *, by: str, now: datetime, code: str = "cancelled"
) -> OutboxMessage:
    """The owner's ✖️, the owner's own reply (``auto:own``), a deleted trigger."""
    row = await _locked(session, row_id)
    if row is None or row.state not in PENDING:
        raise AlreadyAnswered(str(row_id))
    _transition(row, CANCELLED, by=by, now=now, code=code)
    await session.flush()
    return row


async def expire(
    session: AsyncSession, row_id: int, *, now: datetime, by: str = BY_EXPIRY
) -> bool:
    """Expire a pending row whose ``expires_at`` has passed."""
    row = await _locked(session, row_id)
    if row is None or row.state not in PENDING:
        return False
    if row.expires_at is None or row.expires_at > now:
        return False
    _transition(row, EXPIRED, by=by, now=now, code="expired")
    await session.flush()
    return True


async def expire_due(
    session: AsyncSession, *, now: datetime, limit: int = 200
) -> list[int]:
    """Every pending row past ``expires_at``: never sent (JARVIS_DRAFT_EXPIRY_HOURS)."""
    ids = list(
        await session.scalars(
            sa.select(OutboxMessage.id)
            .where(OutboxMessage.state.in_(PENDING), OutboxMessage.expires_at <= now)
            .order_by(OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    done: list[int] = []
    for row_id in ids:
        try:
            async with session.begin_nested():
                if await expire(session, row_id, now=now):
                    done.append(row_id)
        except Exception as exc:
            log.error(
                "outbox expiry failed for one row",
                extra={"outbox_id": row_id, "error": type(exc).__name__},
            )
    return done


async def record_warning(
    session: AsyncSession,
    row_id: int,
    *,
    kind: str,
    value: object,
    by: str,
    now: datetime,
) -> OutboxMessage:
    """A card showed the owner a warning (a personal chat, a flagged message
    in the chat); an owner-authored row passes that gate line only after it."""
    if kind not in (gate.WARNED_PERSONAL, gate.WARNED_SAFETY):
        raise OutboxError("bad_warning")
    row = await _locked(session, row_id)
    if row is None or row.state != DRAFT:
        raise AlreadyAnswered(str(row_id))
    _note(row, kind, None, value, by=by, now=now)
    await session.flush()
    return row


# --- the claim -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PacingPlan:
    """How the adapter sends it like a person would (JV-19, JV-30)."""

    pre_delay_seconds: float
    typing_ms: int
    mark_read: bool


@dataclass(frozen=True, slots=True)
class ClaimedSend:
    """A frozen copy of a claimed row: everything an adapter needs, nothing
    it could write back."""

    id: int
    channel: str
    chat_key: str
    tg_chat_id: int | None
    to_address: str | None
    body: str
    reply_to_external_id: str | None
    trigger_interaction_id: int | None
    idempotency_key: str
    authored_by: str
    lease_until: datetime
    pacing: PacingPlan


def pacing_for(
    body: str, *, channel: str, has_trigger: bool, rng: random.Random
) -> PacingPlan:
    typing_seconds = min(len(body) / TYPING_CHARS_PER_SECOND, TYPING_MAX_SECONDS)
    return PacingPlan(
        pre_delay_seconds=rng.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS),
        typing_ms=int(typing_seconds * 1000),
        # A chat is marked read only when a reply is actually sent into it.
        mark_read=has_trigger and channel != chatkey.GMAIL,
    )


@dataclass(slots=True)
class _Counters:
    auto_sent: int
    channel_sent: int
    last_send_at: datetime | None
    cost_over_cap: bool
    chat_ai_sent: dict[str, int]


async def _context(
    session: AsyncSession,
    row: OutboxMessage,
    *,
    now: datetime,
    policies: policy.Policies,
    counters: _Counters,
    stall_texts: tuple[str, ...] = (),
) -> gate.Context:
    trigger = await _trigger(session, row)
    names = await guard_names(session, row)
    if row.chat_key not in counters.chat_ai_sent:
        counters.chat_ai_sent[row.chat_key] = await caps.chat_ai_sent_today(
            session, row.chat_key, now
        )
    return gate.Context(
        now=now,
        jarvis_enabled=settings.jarvis_enabled,
        channel_send_enabled=channel_send_enabled(row.channel),
        env_ceiling=policy.ceiling(),
        policies=policies,
        chat=await chat_facts(
            session,
            chat_key=row.chat_key,
            channel=row.channel,
            tg_chat_id=row.tg_chat_id,
            person_id=row.person_id,
        ),
        rules=await _rules(session, row),
        trigger=trigger,
        unsafe_inbound_ids=await unsafe_inbound(session, row, trigger),
        safety_readable=safety_readable(row.channel, row.tg_chat_id),
        chat_state=await _chat_state(session, row.chat_key),
        caps=gate.Caps(
            auto_sent=counters.auto_sent,
            chat_ai_sent=counters.chat_ai_sent[row.chat_key],
            channel_sent=counters.channel_sent,
            last_send_at=counters.last_send_at,
            cost_over_cap=counters.cost_over_cap,
            auto_cap=settings.jarvis_auto_daily_cap,
            per_chat_cap=settings.jarvis_per_chat_daily_cap,
            gap_seconds=settings.jarvis_send_min_gap_seconds,
            channel_cap=channel_daily_cap(row.channel),
            resets_at=caps.next_day(now),
        ),
        guard=gate.registered_guard(),
        allowed_tokens=await allowed_tokens(session, row),
        allowed_names=names.allowed,
        other_names=names.others,
        client_script=names.client_script,
        # The approved texts of the statement allowlist: the owner's stall
        # overrides in force now (JV-14), read once per claim; JV-15 passes
        # the active KB entries. Until then a body re-guarded here matches no
        # KB text, and an auto row that leaned on one goes back to the card.
        approved_kb=(),
        stall_texts=stall_texts,
        quiet_until=quiet_until(now),
        auto_in_quiet_hours=bool(getattr(settings, AUTO_IN_QUIET_HOURS, False)),
    )


def _apply(
    row: OutboxMessage, decision: gate.Decision, *, now: datetime, claimed_by: str
) -> bool:
    """Write one gate decision; True when the row was claimed."""
    if decision.guard is not None:
        row.guard = decision.guard.bound_to(
            decision.body_sha256 or gate.body_hash(row.body)
        )
        row.body_sha256 = decision.body_sha256 or gate.body_hash(row.body)
        _log_guard_error(row, decision.guard)
    if decision.action == "send":
        _transition(row, SENDING, by=BY_GATE, now=now)
        row.lease_until = now + caps.SEND_LEASE
        row.claimed_by = claimed_by[:32]
        row.attempts = (row.attempts or 0) + 1
        row.next_attempt_at = None
        return True
    code = decision.code or decision.action
    if decision.action == "hold":
        row.next_attempt_at = decision.retry_at or now + HOLD_POLL
        if row.last_error != code:
            row.last_error = code
            _note(row, "hold", None, code, by=BY_GATE, now=now)
        return False
    if decision.action == "redraft":
        _transition(row, DRAFT, by=BY_GATE, now=now, code=code)
        row.approved_by = None
        row.approved_at = None
        row.next_attempt_at = None
        return False
    target = {"block": BLOCKED, "expire": EXPIRED, "cancel": CANCELLED}[decision.action]
    by = {"expire": BY_EXPIRY, "cancel": BY_OWN if code == "owner_replied" else BY_GATE}
    _transition(row, target, by=by.get(decision.action, BY_GATE), now=now, code=code)
    return False


async def claim_ready(
    session: AsyncSession,
    channel: str,
    *,
    now: datetime,
    account: str | None = None,
    limit: int = 5,
    rng: random.Random | None = None,
) -> list[ClaimedSend]:
    """The only way an adapter gets a row (rule 12).

    One transaction: approved rows of ``channel`` that are due are locked
    with ``SKIP LOCKED`` (a concurrent claim never sees them), the full gate
    runs on each, and the ones that pass become ``sending`` with a lease.
    The caller commits before any network I/O. A row whose gate run raises
    is held with ``gate_error`` and the rest go on (rule 25).

    Claims are serialised by a transaction-scoped advisory lock: the caps and
    the anti-ban gap are read from committed rows, so two claimers running at
    once would both see room for one more send. A claimer that finds the lock
    held returns nothing at once (it never waits); its next poll counts the
    other claim.
    """
    if not settings.jarvis_enabled:
        return []
    if not await session.scalar(
        sa.select(sa.func.pg_try_advisory_xact_lock(sa.func.hashtext(CLAIM_LOCK)))
    ):
        log.debug("outbox claim skipped: another claim runs", extra={"channel": channel})
        return []
    rows = list(
        await session.scalars(
            sa.select(OutboxMessage)
            .where(
                OutboxMessage.channel == channel,
                OutboxMessage.state == APPROVED,
                sa.or_(
                    OutboxMessage.next_attempt_at.is_(None),
                    OutboxMessage.next_attempt_at <= now,
                ),
            )
            .order_by(
                sa.func.coalesce(
                    OutboxMessage.not_before,
                    OutboxMessage.approved_at,
                    OutboxMessage.created_at,
                ),
                OutboxMessage.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    if not rows:
        return []
    counts = await caps.day_counts(session, now)
    counters = _Counters(
        auto_sent=counts.auto_sent,
        channel_sent=await caps.channel_sent_today(session, channel, now),
        last_send_at=await caps.last_send_at(session, channel, account),
        cost_over_cap=await costs.over_cap(session, now),
        chat_ai_sent={},
    )
    policies = await policy.load(session)
    owner_stalls = await owner_stall_texts(session)
    claimed_by = account or channel
    rng = rng or random.Random()
    claimed: list[ClaimedSend] = []
    for row in rows:
        row_id = row.id
        try:
            async with session.begin_nested():
                ctx = await _context(
                    session,
                    row,
                    now=now,
                    policies=policies,
                    counters=counters,
                    stall_texts=owner_stalls,
                )
                decision = gate.evaluate(snapshot(row), ctx)
                taken = _apply(row, decision, now=now, claimed_by=claimed_by)
                await session.flush()
        except Exception as exc:
            log.error(
                "outbox gate failed for one row",
                extra={"outbox_id": row_id, "error": type(exc).__name__},
            )
            await _hold_after_error(session, row, row_id, now=now)
            continue
        if decision.action != "send" or not taken:
            log.info(
                "outbox row not claimed",
                extra={
                    "outbox_id": row_id,
                    "action": decision.action,
                    "line": decision.line,
                    "code": decision.code,
                },
            )
            continue
        counters.channel_sent += 1
        counters.last_send_at = now
        if row.authored_by == gate.AI:
            counters.chat_ai_sent[row.chat_key] += 1
        if row.approved_by == AUTO_APPROVER:
            counters.auto_sent += 1
        lease = row.lease_until or now + caps.SEND_LEASE
        claimed.append(
            ClaimedSend(
                id=row.id,
                channel=row.channel,
                chat_key=row.chat_key,
                tg_chat_id=row.tg_chat_id,
                to_address=row.to_address,
                body=row.body,
                reply_to_external_id=row.reply_to_external_id,
                trigger_interaction_id=row.trigger_interaction_id,
                idempotency_key=row.idempotency_key,
                authored_by=row.authored_by,
                lease_until=lease,
                pacing=pacing_for(
                    row.body,
                    channel=row.channel,
                    has_trigger=row.trigger_interaction_id is not None,
                    rng=rng,
                ),
            )
        )
        log.info("outbox row claimed", extra={"outbox_id": row.id, "channel": channel})
    return claimed


async def _hold_after_error(
    session: AsyncSession, row: OutboxMessage, row_id: int, *, now: datetime
) -> None:
    """A row the gate could not judge stays approved and waits; never sent.
    What the failed run changed in memory is discarded: the row is reloaded
    as the database holds it (``row_id`` was read before the run, so an
    expired row is never touched synchronously)."""
    try:
        async with session.begin_nested():
            await session.execute(
                sa.update(OutboxMessage)
                .where(OutboxMessage.id == row_id, OutboxMessage.state == APPROVED)
                .values(last_error="gate_error", next_attempt_at=now + HOLD_POLL)
                .execution_options(synchronize_session=False)
            )
        await session.refresh(row)
    except Exception as exc:
        log.error(
            "could not hold an outbox row after a gate error",
            extra={"outbox_id": row_id, "error": type(exc).__name__},
        )


# --- the adapter's report ----------------------------------------------------------


def _tg_message_id(external_id: str) -> int | None:
    _chat, sep, message = external_id.rpartition(":")
    try:
        return int(message) if sep else int(external_id)
    except ValueError:
        return None


async def _tg_echo(
    session: AsyncSession, tg_chat_id: int, message_id: int
) -> Interaction | None:
    found: Interaction | None = await session.scalar(
        sa.select(Interaction)
        .where(
            Interaction.source == InteractionSource.telegram_userbot,
            Interaction.tg_chat_id == tg_chat_id,
            Interaction.meta.has_key("tg_message_id"),
            Interaction.meta["tg_message_id"].astext.cast(sa.BigInteger) == message_id,
        )
        .execution_options(populate_existing=True)
    )
    return found


def _tag(interaction: Interaction, row: OutboxMessage) -> None:
    echo = authorship.Echo(
        outbox_id=row.id, authored_by=authorship.author_of(row.authored_by)
    )
    interaction.meta = authorship.tagged(
        interaction.meta if isinstance(interaction.meta, dict) else None, echo
    )


async def _write_echo(
    session: AsyncSession, row: OutboxMessage, external_id: str, sent_at: datetime
) -> Interaction | None:
    """Write or tag the interaction of the sent message (§3.2, JV-10).

    The userbot may have stored the echo first, untagged, because the
    external id was not on the row yet; that row is tagged instead of a
    second one being written (``ux_interactions_tg_message``).
    """
    if row.channel == chatkey.TELEGRAM and row.tg_chat_id is not None:
        message_id = _tg_message_id(external_id)
        if message_id is None:
            return None
        echo = await _tg_echo(session, row.tg_chat_id, message_id)
        if echo is None:
            fresh = Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.out,
                person_id=row.person_id,
                tg_chat_id=row.tg_chat_id,
                occurred_at=sent_at,
                raw_text=row.body,
                meta={"tg_message_id": message_id},
            )
            _tag(fresh, row)
            try:
                async with session.begin_nested():
                    session.add(fresh)
                    await session.flush()
                return fresh
            except IntegrityError:
                echo = await _tg_echo(session, row.tg_chat_id, message_id)
        if echo is not None:
            _tag(echo, row)
            await session.flush()
        return echo
    key = ECHO_ID_KEYS.get(row.channel)
    if key is None:
        return None
    # WhatsApp and Gmail echoes are stored by their ingest (JV-26, JV-29);
    # here they are only tagged.
    found: Interaction | None = await session.scalar(
        sa.select(Interaction).where(Interaction.meta[key].astext == external_id)
    )
    if found is not None:
        _tag(found, row)
        await session.flush()
    return found


async def _note_ai_sent(
    session: AsyncSession, row: OutboxMessage, sent_at: datetime
) -> None:
    if row.authored_by != gate.AI:
        return
    await session.execute(
        pg_insert(JarvisChatState)
        .values(
            chat_key=row.chat_key,
            channel=row.channel,
            person_id=row.person_id,
            last_ai_sent_at=sent_at,
            ai_turns_since_client=1,
        )
        .on_conflict_do_update(
            index_elements=[JarvisChatState.chat_key],
            set_={
                "last_ai_sent_at": sent_at,
                "ai_turns_since_client": JarvisChatState.ai_turns_since_client + 1,
                "updated_at": sa.func.now(),
            },
        )
    )


async def complete(
    session: AsyncSession,
    row_id: int,
    *,
    external_id: str,
    sent_at: datetime,
    by: str = BY_ADAPTER,
) -> OutboxMessage | None:
    """The adapter sent it: ``sent``, the external id recorded, the echo
    interaction written or tagged. Idempotent for the same external id;
    None when the row is gone (a purge)."""
    row = await _locked(session, row_id)
    if row is None:
        log.warning("completed outbox row is gone", extra={"outbox_id": row_id})
        return None
    if row.state == SENT and row.external_message_id == external_id:
        return row
    if row.state not in (SENDING, UNKNOWN):
        raise IllegalTransition(f"{row.state}->{SENT}")
    _transition(row, SENT, by=by, now=sent_at)
    row.external_message_id = external_id
    row.sent_at = sent_at
    row.lease_until = None
    if row.trigger_interaction_id is not None and row.channel != chatkey.GMAIL:
        row.read_ack_at = sent_at
    await session.flush()
    echo = await _write_echo(session, row, external_id, sent_at)
    if echo is not None:
        row.sent_interaction_id = echo.id
    await _note_ai_sent(session, row, sent_at)
    await session.flush()
    log.info("outbox row sent", extra={"outbox_id": row.id, "channel": row.channel})
    return row


async def fail(
    session: AsyncSession,
    row_id: int,
    *,
    code: str,
    now: datetime,
    retry_at: datetime | None = None,
    by: str = BY_ADAPTER,
) -> OutboxMessage | None:
    """The adapter could not send it. With ``retry_at`` (a flood wait) the
    row waits approved; without it, or after MAX_ATTEMPTS, it failed for
    good. ``code`` is an error code, never text."""
    row = await _locked(session, row_id)
    if row is None:
        return None
    if row.state not in (SENDING, UNKNOWN):
        raise IllegalTransition(f"{row.state}->{FAILED}")
    code = code[:64]
    row.lease_until = None
    # A row in doubt (unknown) is never queued again: it may have been sent.
    retry = row.state == SENDING and retry_at is not None
    if retry and (row.attempts or 0) < MAX_ATTEMPTS:
        _transition(row, APPROVED, by=by, now=now, code=code)
        row.next_attempt_at = retry_at
    else:
        _transition(row, FAILED, by=by, now=now, code=code)
    await session.flush()
    log.warning(
        "outbox send failed",
        extra={"outbox_id": row.id, "code": code, "state": row.state},
    )
    return row


@dataclass(frozen=True, slots=True)
class Reconciled:
    id: int
    state: str


async def _find_echo(session: AsyncSession, row: OutboxMessage) -> Interaction | None:
    tagged: Interaction | None = await session.scalar(
        sa.select(Interaction)
        .where(
            Interaction.meta.has_key(authorship.OUTBOX_ID_KEY),
            Interaction.meta[authorship.OUTBOX_ID_KEY].astext == str(row.id),
        )
        .limit(1)
    )
    if tagged is not None or row.channel != chatkey.TELEGRAM or row.tg_chat_id is None:
        return tagged
    claimed_at = (row.lease_until or datetime.now(settings.tz)) - caps.SEND_LEASE
    untagged: Interaction | None = await session.scalar(
        sa.select(Interaction)
        .where(
            Interaction.source == InteractionSource.telegram_userbot,
            Interaction.tg_chat_id == row.tg_chat_id,
            authorship.owner_words_clause(),
            sa.not_(Interaction.meta.has_key(authorship.SENT_BY_KEY)),
            Interaction.meta.has_key("tg_message_id"),
            Interaction.raw_text == row.body,
            Interaction.occurred_at >= claimed_at - ECHO_SLACK,
        )
        .order_by(Interaction.occurred_at)
        .limit(1)
    )
    return untagged


async def reconcile(
    session: AsyncSession, *, now: datetime, limit: int = 50
) -> list[Reconciled]:
    """Rows whose lease ran out with no report: an echo found is ``sent``;
    none is ``unknown`` (the owner is told) and is never resent."""
    rows = list(
        await session.scalars(
            sa.select(OutboxMessage)
            .where(OutboxMessage.state == SENDING, OutboxMessage.lease_until < now)
            .order_by(OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    done: list[Reconciled] = []
    for row in rows:
        row_id = row.id
        try:
            async with session.begin_nested():
                echo = await _find_echo(session, row)
                message_id = (
                    (echo.meta or {}).get(ECHO_ID_KEYS.get(row.channel, ""))
                    if echo is not None
                    else None
                )
                if echo is not None and message_id is not None:
                    external = (
                        authorship.telegram_external_id(row.tg_chat_id, int(message_id))
                        if row.channel == chatkey.TELEGRAM and row.tg_chat_id is not None
                        else str(message_id)
                    )
                    _transition(row, SENT, by=BY_RECONCILE, now=now, code="reconciled")
                    row.external_message_id = external
                    row.sent_at = echo.occurred_at
                    row.sent_interaction_id = echo.id
                    row.lease_until = None
                    _tag(echo, row)
                    await _note_ai_sent(session, row, echo.occurred_at)
                else:
                    # The lease stays: its claim moment is when the row went on
                    # the wire, and every cap counts an unknown row from it
                    # (caps._wire_moment). ix_outbox_messages_lease covers only
                    # 'sending'; complete() and fail() clear it.
                    _transition(
                        row, UNKNOWN, by=BY_RECONCILE, now=now, code="lease_expired"
                    )
                await session.flush()
            done.append(Reconciled(id=row_id, state=row.state))
            log.warning(
                "outbox lease expired", extra={"outbox_id": row_id, "state": row.state}
            )
        except Exception as exc:
            log.error(
                "outbox reconcile failed for one row",
                extra={"outbox_id": row_id, "error": type(exc).__name__},
            )
    return done


# history ``field`` of the note that the owner was told about an unknown row.
TOLD_FIELD: Final = "owner_told"


async def unknown_untold(session: AsyncSession, *, limit: int = 20) -> list[int]:
    """``unknown`` rows the owner has not been told about yet."""
    told = OutboxMessage.history.op("@>")(
        sa.cast(sa.literal([{"field": TOLD_FIELD}], JSONB), JSONB)
    )
    return list(
        await session.scalars(
            sa.select(OutboxMessage.id)
            .where(OutboxMessage.state == UNKNOWN, sa.not_(told))
            .order_by(OutboxMessage.id)
            .limit(limit)
        )
    )


async def mark_told(session: AsyncSession, row_id: int, *, now: datetime) -> None:
    """The owner's «❓ yuborilganmi — aniq emas» went out for this row."""
    row = await _locked(session, row_id)
    if row is None:
        return
    _note(row, TOLD_FIELD, None, row.state, by=BY_RECONCILE, now=now)
    await session.flush()


__all__ = [
    "APPROVED",
    "BLOCKED",
    "CANCELLED",
    "DRAFT",
    "EXPIRED",
    "FAILED",
    "PENDING",
    "SENDING",
    "SENT",
    "SHADOW",
    "TRANSITIONS",
    "UNKNOWN",
    "AlreadyAnswered",
    "Approval",
    "ClaimedSend",
    "IllegalTransition",
    "JarvisDisabled",
    "NotPlainText",
    "OutboxError",
    "PacingPlan",
    "Reconciled",
    "allowed_tokens",
    "approve",
    "cancel",
    "channel_send_enabled",
    "chat_facts",
    "claim_ready",
    "complete",
    "create",
    "expire",
    "expire_due",
    "fail",
    "idempotency_key",
    "mark_told",
    "pacing_for",
    "quiet_until",
    "reconcile",
    "record_warning",
    "safety_readable",
    "snapshot",
    "unknown_untold",
    "unsafe_inbound",
]
