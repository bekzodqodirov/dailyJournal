"""Draft and escalation cards: what the owner sees and taps (docs/jarvis-plan.md
§3.3 step 9, JV-18).

* **Draft cards.** A reply the responder stored as a ``draft`` reaches the
  owner as a card: the client's words, the exact text, and the buttons
  ``jv:y|e|o|n:<id>`` (send, edit, I'll write, cancel). A draft made over
  JARVIS_DRAFT_DAILY_CAP gets no card: the owner is told once that day and
  the morning brief names the chats.
* **Escalation alerts.** A message only the owner may answer (a price, a
  deadline, a promise, an identity question) is alerted with the client's
  messages, their name and code, their open balance read from SQL at render
  time (never a figure from a model; the stored summary is shown only when
  it holds no figure), the holding reply and the buttons ``je:y|o|s:<id>``
  (approve and send, I'll write, remind in an hour). ``alert_now`` sends it
  in the same ``jarvis_respond`` run that opened the escalation (1a), so it
  never waits for the owner-first window; ``sweep`` (``jarvis_cards``,
  every minute) only retries the ones whose ``alerted_at`` is still empty.
  Past JARVIS_ESCALATION_DAILY_CAP the day's further alerts fold into one
  message, edited as more come. Right after the alert went out, one
  EXTRACT_MODEL sentence (``jarvis_summary``) says what the client wants:
  kept only when it holds no figure, stored on the escalation and edited
  into the alert on screen («📝»).
* **Snooze.** «⏰ 1 soatdan keyin eslat» stores ``snoozed_until`` on the row
  (the scheduler's job store is in memory, so a timer would die with a
  restart); the sweep alerts it again once that passes. The snooze that
  reaches JARVIS_SNOOZE_MAX sends the escalation to the morning brief instead,
  and it expires once a brief has named it.
* **The owner's words.** A reply to a card (typed, or a voice note the bot
  transcribed) replaces a draft's text, or becomes the escalation's own
  reply, ``authored_by='owner'``: the guard reads it in the ``owner`` role,
  which only flags; the card says so, and shows the personal-chat and
  shield warnings the gate wants on record before such a row may leave.
* **Copy mode.** Until the channel has a sender (``outbox.channel_send_enabled``)
  no card carries a send button: the card shows the text to copy instead.

Every tap is honoured only from the card the owner is looking at now (its
message id is on the row), runs under a row lock and is re-validated by the
outbox; a second tap is stale. The ``jv:`` and ``je:`` buttons are never in
the bot's ``_button_handlers``, so a typed batch answer cannot approve a
send. Jarvis never writes ``question_log`` and never spends the question
budget. Quiet hours do not hold a card or an alert: it arrives silently.

Rendering is the bot's (``replies.jarvis_card``, ``keyboards.jarvis_card``):
this module hands it plain ``Card`` values through a ``Presenter`` and records
what was shown. Failures are contained per row (rule 25): one card that
cannot be built or delivered is logged with its ids and the rest go on; a
delivery that failed is retried by the next sweep, never lost.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from miya.config import settings
from miya.db.enums import Currency, DebtDirection, Direction
from miya.db.models import (
    ESCALATION_OPEN_STATES,
    ChatMonitor,
    Escalation,
    Interaction,
    JarvisChatState,
    OutboxMessage,
    Person,
    ReminderLog,
)
from miya.db.session import session_scope
from miya.jarvis import (
    caps,
    chat_state,
    chatkey,
    costs,
    envelope,
    gate,
    outbox,
    responder,
)
from miya.services import codes, queries, reminders, safety
from miya.services.brief import BRIEF_KIND

log = logging.getLogger(__name__)

CardKind = Literal["draft", "escalation"]
DRAFT_CARD: Final[CardKind] = "draft"
ESCALATION_CARD: Final[CardKind] = "escalation"

# The draft buttons (``jv:<action>:<id>``) and the escalation buttons
# (``je:<action>:<id>``); keyboards.py builds the payloads from these.
SEND: Final = "y"
EDIT: Final = "e"
OWN: Final = "o"
CANCEL: Final = "n"
SNOOZE: Final = "s"
DRAFT_ACTIONS: Final = (SEND, EDIT, OWN, CANCEL)
ESCALATION_ACTIONS: Final = (SEND, OWN, SNOOZE)

# What a tap or a reply came to (the bot says it in Uzbek).
TapCode = Literal[
    "queued",
    "own",
    "snoozed",
    "cancelled",
    "edit",
    "edited",
    "stale",
    "answered",
    "copy",
    "failed",
    "too_long",
]

# history ``by`` values (String(16)).
BY_BUTTON: Final = "button"
BY_REPLY: Final = "reply"
BY_POLICY: Final = outbox.BY_POLICY
BY_EXPIRY: Final = outbox.BY_EXPIRY
# Outbox and escalation codes.
OWNER_WILL_WRITE: Final = "owner_will_write"
OWNER_TEXT: Final = "owner_text"
SUPERSEDED: Final = "superseded"
CANCELLED_CODE: Final = "cancelled"
# The escalation's ✅ approved its holding reply, not the owner's words.
STALL_APPROVED: Final = "stall_approved"
TO_BRIEF: Final = "to_brief"
BRIEFED: Final = "briefed"
# The escalation history entry of every alert shown, with how it went out.
ALERT_FIELD: Final = "alert"
ALERT_CARD: Final = "card"
ALERT_FOLD: Final = "fold"
ALERT_AGAIN: Final = "realert"
SNOOZE_FIELD: Final = "snooze"
# Escalation states and the resolution of one the owner's tap answered.
OPEN: Final = "open"
SNOOZED: Final = "snoozed"
OWNER_REPLYING: Final = "owner_replying"
RESOLVED: Final = "resolved"
EXPIRED: Final = "expired"
RESOLUTION_SENT: Final = "sent"
# ``last_error`` of a row that expired unanswered (outbox.expire).
EXPIRED_CODE: Final = "expired"
# The modes whose escalations and drafts are shown (§6.1: shadow is stored,
# never shown).
SHOWN_MODES: Final = ("draft", "auto")
# The reply rows of an escalation: the AI's stall, and the owner's own words.
STALL_ORIGIN: Final = "stall"
REPLY_ORIGIN: Final = outbox.ESCALATION_ORIGIN
DRAFT_ORIGIN: Final = "auto_reply"
# A draft or stall that has gone out (or is going) reads «Mijozga yuborildi».
GONE_OUT: Final = (outbox.SENDING, outbox.SENT)
SHOWN_BODY_STATES: Final = (outbox.DRAFT, outbox.APPROVED, *GONE_OUT)
# The guard's flags that make the owner-text warning («narx yoki muddat»).
RED_LINE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "price",
        "discount",
        "payment_terms",
        "deal",
        "deadline",
        "promise",
        "guarantee",
        "payment_claim",
        "digits",
    }
)
# The ReminderLog kind of «today's draft cap was reached» (ref: the day).
DRAFT_CAP_KIND: Final = "jarvis_draft_cap"
# The longest owner text a card takes (the card must show the exact text
# whole, inside Telegram's message limit, beside the client's words, the
# warnings and the footer; the bot checks the whole card as well).
OWNER_TEXT_MAX: Final = 2000
# How many of the client's messages a card quotes.
CLIENT_LINES: Final = 3
# Rows one sweep step looks at.
SWEEP_BATCH: Final = 50
# How far back a draft card looks for the client's words before its trigger.
CLIENT_LOOKBACK: Final = timedelta(hours=2)
# The alert's «📝» line: one EXTRACT_MODEL sentence, written after the alert.
SUMMARY_FIELD: Final = "summary"
SUMMARY_MAX_TOKENS: Final = 120
SUMMARY_TIMEOUT_SECONDS: Final = 30
SUMMARY_PROMPT: Final = f"""\
You write a one-sentence note for a small business owner in Uzbekistan (a cargo \
and logistics business) about what one client wants from them, so that the owner \
can answer the client themselves.
Write in Uzbek, Latin script, at most twenty words, as plain prose about the \
client's latest messages: what they ask for or tell the owner. Never write a \
digit, a number in words, a price, an amount of money, a date, a time, a phone \
number or a code. Never answer the client, promise anything or give advice.

{envelope.RULE}
The client's latest messages are the CLIENT lines after the last OWNER or MIYA line.
Answer with the sentence only."""


# --- what a card shows ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Balance:
    """One open balance with the client, from the ledger (SQL)."""

    direction: DebtDirection
    amount: Decimal
    currency: Currency


@dataclass(frozen=True, slots=True)
class ClientLine:
    """One message of the client's, quoted on the card."""

    text: str
    at: datetime


@dataclass(frozen=True, slots=True)
class SafetyNote:
    """The newest message in the chat the shield flagged (JV-09): an owner
    text going there is warned about it on the card."""

    interaction_id: int
    verdict: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Card:
    """Everything a card shows, as plain values. Names and the client's words
    are the counterparty's: the bot shows them escaped and stripped of bidi
    and zero-width characters."""

    kind: CardKind
    # The outbox row (``j<id>``) of a draft card, the escalation (``e<id>``).
    id: int
    channel: str
    name: str | None = None
    code: str | None = None
    messages: tuple[ClientLine, ...] = ()
    # The text the client would get: the draft, the stall, or the owner's.
    body: str | None = None
    body_sent: bool = False
    # The row the card's ✅ approves, and whether its words are the owner's.
    outbox_id: int | None = None
    owner_text: bool = False
    redline_warning: bool = False
    personal_warning: bool = False
    safety: SafetyNote | None = None
    reasons: tuple[str, ...] = ()
    summary: str | None = None
    balances: tuple[Balance, ...] = ()
    can_send: bool = False
    can_snooze: bool = False
    # No sender for this channel yet: the card shows the text to copy.
    copy_mode: bool = True
    # 0 for the first alert, n for the n-th reminder after a snooze.
    reminder: int = 0


class Presenter(Protocol):
    """The bot side: renders and delivers, returns the bot message's id (None
    when Telegram did not take it)."""

    async def card(
        self, card: Card, *, silent: bool, reply_to: int | None = None
    ) -> int | None: ...

    async def fold(
        self, cards: Sequence[Card], *, cap: int, silent: bool, edit: int | None
    ) -> int | None: ...

    async def draft_cap(self, *, cap: int, silent: bool) -> bool: ...

    async def edit(self, card: Card, *, message_id: int) -> bool: ...


def silent_at(now: datetime) -> bool:
    """Quiet hours hold nothing here: cards and alerts arrive silently."""
    return reminders.in_quiet_hours(now.astimezone(settings.tz))


def snooze_for() -> timedelta:
    return timedelta(minutes=settings.jarvis_escalation_snooze_minutes)


def _entry(
    field: str,
    old: object,
    new: object,
    *,
    by: str,
    now: datetime,
    code: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "at": now.isoformat(),
        "field": field,
        "old": old,
        "new": new,
        "by": by,
    }
    if code is not None:
        entry["code"] = code
    return entry


Entries = list[dict[str, object]]


def _state_entries(
    escalation: Escalation, new: str, *, by: str, now: datetime, code: str
) -> Entries:
    old = escalation.state
    return [] if old == new else [_entry("state", old, new, by=by, now=now, code=code)]


async def _save(
    session: AsyncSession,
    escalation: Escalation,
    values: dict[str, object],
    entries: Sequence[dict[str, object]] = (),
) -> None:
    """One UPDATE of an escalation's columns and its new history entries (the
    way the responder and chat_state write escalations), then the row is
    read back."""
    await session.execute(
        sa.update(Escalation)
        .where(Escalation.id == escalation.id)
        .values(
            **values,
            history=[*(escalation.history or []), *entries],
            updated_at=sa.func.now(),
        )
    )
    await session.refresh(escalation)


def created_mode(history: Sequence[object] | None) -> str | None:
    """The mode an escalation was opened in (JV-16 records it), or None."""
    first = history[0] if history else None
    mode = first.get("mode") if isinstance(first, dict) else None
    return mode if isinstance(mode, str) else None


def _created_mode_clause() -> sa.ColumnElement[str]:
    """The SQL of ``created_mode``: the creation entry's mode, '' for none."""
    mode: sa.ColumnElement[str] = sa.func.coalesce(
        Escalation.history[0]["mode"].astext, ""
    )
    return mode


def _created_code(history: Sequence[object] | None) -> str | None:
    first = history[0] if history else None
    code = first.get("code") if isinstance(first, dict) else None
    return code if isinstance(code, str) else None


def _capped_clause() -> sa.ColumnElement[bool]:
    """The SQL of ``_capped``: the creation entry's code."""
    return (
        sa.func.coalesce(OutboxMessage.history[0]["code"].astext, "")
        == caps.DRAFT_CAP_CODE
    )


async def _capped_today(session: AsyncSession, now: datetime) -> bool:
    """A draft over today's cap is waiting with no card."""
    found = await session.scalar(
        sa.select(OutboxMessage.id)
        .where(
            OutboxMessage.state == outbox.DRAFT,
            OutboxMessage.origin == DRAFT_ORIGIN,
            OutboxMessage.card_tg_message_id.is_(None),
            OutboxMessage.created_at >= reminders.day_start(now),
            _capped_clause(),
        )
        .limit(1)
    )
    return found is not None


def _capped(row: OutboxMessage) -> bool:
    """A draft made over JARVIS_DRAFT_DAILY_CAP: never a card (the creation
    entry says so; ``last_error`` moves on)."""
    return _created_code(row.history) == caps.DRAFT_CAP_CODE


def _text(row: Interaction) -> str:
    parts = [p.strip() for p in (row.raw_text, row.transcript) if p and p.strip()]
    return "\n".join(parts)


def _shown(row: Interaction) -> bool:
    """A client message a card may quote: not deleted, not one the shield
    flagged (its own warning tells the owner about it)."""
    meta = row.meta if isinstance(row.meta, dict) else {}
    media = row.media if isinstance(row.media, dict) else {}
    risk = media.get("risk")
    risky = isinstance(risk, dict) and risk.get("kind") not in (None, safety.OK)
    return (
        not meta.get(outbox.DELETED_KEY)
        and not safety.blocks_reply(meta)
        and not risky
        and bool(_text(row))
    )


def _lines(rows: Sequence[Interaction]) -> tuple[ClientLine, ...]:
    kept = [r for r in rows if _shown(r)]
    kept.sort(key=lambda r: (r.occurred_at, r.id))
    return tuple(ClientLine(_text(r), r.occurred_at) for r in kept[-CLIENT_LINES:])


async def _chat_title(session: AsyncSession, tg_chat_id: int | None) -> str | None:
    """The chat's own title (``chat_monitors``), for a chat no person was
    resolved for."""
    if tg_chat_id is None:
        return None
    title = await session.scalar(
        sa.select(ChatMonitor.title).where(ChatMonitor.tg_chat_id == tg_chat_id)
    )
    return title if isinstance(title, str) and title.strip() else None


def _escalation_chat(escalation: Escalation) -> int | None:
    try:
        return chatkey.tg_chat_id(escalation.chat_key)
    except chatkey.BadChatKey:
        return None


async def _who(
    session: AsyncSession, person_id: int | None, tg_chat_id: int | None
) -> tuple[str | None, str | None]:
    """The recipient's name and code; with no person, the chat's own title
    (the card always says which chat the text goes to; «Mijoz» only when
    neither exists)."""
    person = await session.get(Person, person_id) if person_id is not None else None
    if person is None:
        return await _chat_title(session, tg_chat_id), None
    found = await codes.codes_of(session, person.id)
    return person.display_name, (found[0] if found else None)


async def _balances(session: AsyncSession, person_id: int | None) -> tuple[Balance, ...]:
    """The client's open balances from the ledger: the only figures a card
    shows that are not the client's own words (rule 1)."""
    if person_id is None:
        return ()
    return tuple(
        Balance(b.direction, Decimal(b.outstanding), b.currency)
        for b in await queries.open_debts(session, person_id=person_id)
    )


def _clean_summary(summary: str | None) -> str | None:
    """The stored summary, only when it holds no figure (``summary`` is
    digit-free prose, refused like ``recaps.clean_prose``)."""
    from miya.services import recaps

    cleaned = recaps.clean_prose(summary)
    return cleaned if isinstance(cleaned, str) else None


def _red_flags(row: OutboxMessage) -> bool:
    flags = (row.guard or {}).get("flags")
    return isinstance(flags, list) and any(f in RED_LINE_FLAGS for f in flags)


async def _safety_note(
    session: AsyncSession, unsafe_ids: Sequence[int]
) -> SafetyNote | None:
    if not unsafe_ids:
        return None
    latest = max(unsafe_ids)
    row = await session.get(Interaction, latest)
    meta = row.meta if row is not None and isinstance(row.meta, dict) else {}
    verdict_of = meta.get("safety")
    verdict = verdict_of.get("verdict") if isinstance(verdict_of, dict) else None
    reasons = verdict_of.get("reasons") if isinstance(verdict_of, dict) else None
    return SafetyNote(
        interaction_id=latest,
        verdict=verdict if isinstance(verdict, str) else safety.VERDICT_SCAM,
        reasons=tuple(r for r in (reasons or []) if isinstance(r, str)),
    )


async def _owner_bits(
    session: AsyncSession, row: OutboxMessage
) -> tuple[bool, bool, SafetyNote | None]:
    """(red-line flag, personal warning, shield warning) of an owner text."""
    if row.authored_by != gate.OWNER:
        return False, False, None
    warnings = await outbox.card_warnings(session, row)
    return (
        _red_flags(row),
        warnings.personal,
        await _safety_note(session, warnings.unsafe_ids),
    )


async def _draft_lines(
    session: AsyncSession, row: OutboxMessage
) -> tuple[ClientLine, ...]:
    """The client's words the draft answers: its trigger and the unanswered
    messages just before it."""
    if row.trigger_interaction_id is None:
        return ()
    trigger = await session.get(Interaction, row.trigger_interaction_id)
    if trigger is None:
        return ()
    if trigger.tg_chat_id is None:
        return _lines([trigger])
    last_out = await session.scalar(
        sa.select(sa.func.max(Interaction.occurred_at)).where(
            Interaction.tg_chat_id == trigger.tg_chat_id,
            Interaction.direction == Direction.out,
            Interaction.occurred_at <= trigger.occurred_at,
        )
    )
    floor = trigger.occurred_at - CLIENT_LOOKBACK
    since = max(floor, last_out) if isinstance(last_out, datetime) else floor
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(
                Interaction.tg_chat_id == trigger.tg_chat_id,
                Interaction.direction == Direction.in_,
                Interaction.occurred_at <= trigger.occurred_at,
                Interaction.occurred_at > since,
                Interaction.id <= trigger.id,
            )
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
            .limit(CLIENT_LINES)
        )
    )
    return _lines(rows or [trigger])


async def draft_card(session: AsyncSession, row_id: int) -> Card | None:
    """The card of one draft as it reads now; None when the row is gone."""
    row = await session.get(OutboxMessage, row_id, populate_existing=True)
    if row is None:
        return None
    name, code = await _who(session, row.person_id, row.tg_chat_id)
    redline, personal, shield = await _owner_bits(session, row)
    return Card(
        kind=DRAFT_CARD,
        id=row.id,
        channel=row.channel,
        name=name,
        code=code,
        messages=await _draft_lines(session, row),
        body=row.body,
        outbox_id=row.id,
        owner_text=row.authored_by == gate.OWNER,
        redline_warning=redline,
        personal_warning=personal,
        safety=shield,
        can_send=row.state == outbox.DRAFT and outbox.channel_send_enabled(row.channel),
        copy_mode=not outbox.channel_send_enabled(row.channel),
    )


def _escalation_ids(history: Sequence[object] | None, first: int) -> list[int]:
    """The client messages an escalation is about: its burst and every push
    added to it since."""
    ids = [first]
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        burst = entry.get("interaction_ids")
        if isinstance(burst, list):
            ids += [i for i in burst if isinstance(i, int) and not isinstance(i, bool)]
        if entry.get("field") == "repeat" and isinstance(entry.get("new"), int):
            ids.append(int(entry["new"]))
    return sorted(set(ids))


async def _reply_rows(session: AsyncSession, escalation_id: int) -> list[OutboxMessage]:
    """An escalation's stall and the owner's own replies, newest first."""
    return list(
        await session.scalars(
            sa.select(OutboxMessage)
            .where(
                OutboxMessage.escalation_id == escalation_id,
                OutboxMessage.origin.in_((STALL_ORIGIN, REPLY_ORIGIN)),
            )
            .order_by(OutboxMessage.id.desc())
            .execution_options(populate_existing=True)
        )
    )


def _pending_reply(rows: Sequence[OutboxMessage]) -> OutboxMessage | None:
    """The row the escalation's ✅ approves: the owner's own words waiting,
    else the stall waiting."""
    for origin in (REPLY_ORIGIN, STALL_ORIGIN):
        for row in rows:
            if row.origin == origin and row.state == outbox.DRAFT:
                return row
    return None


def _shown_reply(rows: Sequence[OutboxMessage]) -> OutboxMessage | None:
    """The reply the card shows: the one waiting, else the one that went out."""
    pending = _pending_reply(rows)
    if pending is not None:
        return pending
    for row in rows:
        if row.state in SHOWN_BODY_STATES:
            return row
    return None


async def escalation_card(
    session: AsyncSession, escalation_id: int, *, reminder: int = 0
) -> Card | None:
    """The alert of one escalation as it reads now; None when it is gone."""
    escalation = await session.get(Escalation, escalation_id, populate_existing=True)
    if escalation is None:
        return None
    name, code = await _who(session, escalation.person_id, _escalation_chat(escalation))
    messages = list(
        await session.scalars(
            sa.select(Interaction).where(
                Interaction.id.in_(
                    _escalation_ids(escalation.history, escalation.interaction_id)
                )
            )
        )
    )
    rows = await _reply_rows(session, escalation.id)
    shown = _shown_reply(rows)
    pending = _pending_reply(rows)
    redline, personal, shield = (
        await _owner_bits(session, shown) if shown is not None else (False, False, None)
    )
    waiting = escalation.state in ESCALATION_OPEN_STATES
    return Card(
        kind=ESCALATION_CARD,
        id=escalation.id,
        channel=escalation.channel,
        name=name,
        code=code,
        messages=_lines(messages),
        body=shown.body if shown is not None else None,
        body_sent=shown is not None and shown.state in GONE_OUT,
        outbox_id=shown.id if shown is not None else None,
        owner_text=shown is not None and shown.authored_by == gate.OWNER,
        redline_warning=redline,
        personal_warning=personal,
        safety=shield,
        reasons=tuple(escalation.reasons or ()),
        summary=_clean_summary(escalation.summary),
        balances=await _balances(session, escalation.person_id),
        can_send=(
            waiting
            and pending is not None
            and outbox.channel_send_enabled(escalation.channel)
        ),
        can_snooze=escalation.state == OPEN,
        copy_mode=not outbox.channel_send_enabled(escalation.channel),
        reminder=reminder,
    )


async def record_shown(session: AsyncSession, card: Card, *, now: datetime) -> None:
    """The card carried the personal-chat or the shield warning for the
    owner's own text: recorded on that row, which the gate then lets pass
    those lines (§3.2). Contained: a row that moved on keeps its history."""
    if not card.owner_text or card.outbox_id is None:
        return
    warnings: list[tuple[str, object]] = []
    if card.personal_warning:
        warnings.append((gate.WARNED_PERSONAL, True))
    if card.safety is not None:
        warnings.append((gate.WARNED_SAFETY, card.safety.interaction_id))
    for kind, value in warnings:
        try:
            async with session.begin_nested():
                await outbox.record_warning(
                    session, card.outbox_id, kind=kind, value=value, by=BY_POLICY, now=now
                )
        except outbox.AlreadyAnswered:
            continue
        except Exception as exc:
            log.error(
                "card warning not recorded",
                extra={
                    "outbox_id": card.outbox_id,
                    "warning": kind,
                    "error": type(exc).__name__,
                },
            )


# --- delivery: drafts ------------------------------------------------------------------


async def _tell_cap(
    session: AsyncSession, presenter: Presenter, *, now: datetime
) -> bool:
    """«Bugungi qoralama limiti tugadi», once a day; True when told now."""
    day = reminders.day_start(now).date().isoformat()
    told = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ReminderLog)
        .where(ReminderLog.kind == DRAFT_CAP_KIND, ReminderLog.ref == day)
    )
    if told:
        return False
    if not await presenter.draft_cap(
        cap=settings.jarvis_draft_daily_cap, silent=silent_at(now)
    ):
        return False
    session.add(ReminderLog(kind=DRAFT_CAP_KIND, ref=day))
    await session.flush()
    return True


async def card_now(presenter: Presenter, row_id: int, *, now: datetime) -> bool:
    """Deliver one draft's card, if it is a draft with none yet. True when a
    card went out. A draft over the daily cap gets none: the owner is told
    once that day instead. Contained: never raises."""
    try:
        async with session_scope() as session:
            row = await session.scalar(
                sa.select(OutboxMessage)
                .where(
                    OutboxMessage.id == row_id,
                    OutboxMessage.state == outbox.DRAFT,
                    OutboxMessage.origin == DRAFT_ORIGIN,
                    OutboxMessage.card_tg_message_id.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return False
            if _capped(row):
                await _tell_cap(session, presenter, now=now)
                return False
            card = await draft_card(session, row.id)
            if card is None:
                return False
            message_id = await presenter.card(card, silent=silent_at(now))
            if message_id is None:
                log.warning("draft card not delivered", extra={"outbox_id": row_id})
                return False
            await outbox.mark_carded(session, row.id, message_id=message_id, now=now)
            await record_shown(session, card, now=now)
        log.info("draft card delivered", extra={"outbox_id": row_id})
        return True
    except Exception as exc:
        log.error(
            "draft card failed", extra={"outbox_id": row_id, "error": type(exc).__name__}
        )
        return False


# --- delivery: escalations -------------------------------------------------------------


def _alerted_today(start: datetime) -> sa.ColumnElement[bool]:
    return Escalation.alerted_at >= start


def _folded() -> sa.ColumnElement[bool]:
    return _shown_on(Escalation.history, {"field": ALERT_FIELD, "code": ALERT_FOLD})


async def alerts_today(session: AsyncSession, now: datetime) -> int:
    """Escalations alerted since the start of the owner's day (SQL, rule 22)."""
    value = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Escalation)
        .where(_alerted_today(reminders.day_start(now)))
    )
    return int(value or 0)


async def _alerted(
    session: AsyncSession,
    escalation: Escalation,
    message_id: int,
    *,
    code: str,
    now: datetime,
    values: dict[str, object] | None = None,
    entries: Entries | None = None,
) -> None:
    """The owner was shown this escalation in bot message ``message_id``:
    the first alert's moment stays, the count and the message move on, and
    every alert is in the history (a reply to an older one still finds it)."""
    await _save(
        session,
        escalation,
        {
            **(values or {}),
            "alerted_at": escalation.alerted_at or now,
            "alert_count": (escalation.alert_count or 0) + 1,
            "alert_tg_message_id": message_id,
        },
        [
            *(entries or []),
            _entry(
                ALERT_FIELD,
                escalation.alert_tg_message_id,
                message_id,
                by=BY_POLICY,
                now=now,
                code=code,
            ),
        ],
    )


async def _fold(
    session: AsyncSession, presenter: Presenter, escalation: Escalation, *, now: datetime
) -> int | None:
    """Past the daily cap: this alert joins the day's one folded message,
    edited in place (sent anew when it cannot be edited)."""
    start = reminders.day_start(now)
    earlier = list(
        await session.scalars(
            sa.select(Escalation)
            .where(_alerted_today(start), _folded(), Escalation.id != escalation.id)
            .order_by(Escalation.id)
        )
    )
    cards: list[Card] = []
    for row in [*earlier, escalation]:
        card = await escalation_card(session, row.id)
        if card is not None:
            cards.append(card)
    message_ids = [r.alert_tg_message_id for r in earlier if r.alert_tg_message_id]
    return await presenter.fold(
        cards,
        cap=settings.jarvis_escalation_daily_cap,
        silent=silent_at(now),
        edit=max(message_ids) if message_ids else None,
    )


async def alert_now(presenter: Presenter, escalation_id: int, *, now: datetime) -> bool:
    """The escalation's alert, at once (JV-18 1a): called by the responder's
    run right after the chat's transaction committed, and by the sweep for
    any alert still undelivered. Only an escalation opened in ``draft`` or
    ``auto`` mode, still waiting and never alerted, under a row lock (a
    concurrent sweep skips it). True when the owner got it. Contained:
    never raises."""
    try:
        async with session_scope() as session:
            escalation = await session.scalar(
                sa.select(Escalation)
                .where(
                    Escalation.id == escalation_id,
                    Escalation.alerted_at.is_(None),
                    Escalation.state.in_(ESCALATION_OPEN_STATES),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if escalation is None or created_mode(escalation.history) not in SHOWN_MODES:
                return False
            card: Card | None = None
            if await alerts_today(session, now) >= settings.jarvis_escalation_daily_cap:
                code = ALERT_FOLD
                message_id = await _fold(session, presenter, escalation, now=now)
            else:
                code = ALERT_CARD
                card = await escalation_card(session, escalation.id)
                if card is None:
                    return False
                message_id = await presenter.card(card, silent=silent_at(now))
            if message_id is None:
                log.warning(
                    "escalation alert not delivered",
                    extra={"escalation_id": escalation_id, "code": code},
                )
                return False
            await _alerted(session, escalation, message_id, code=code, now=now)
            if card is not None:
                await record_shown(session, card, now=now)
        log.info(
            "escalation alerted", extra={"escalation_id": escalation_id, "code": code}
        )
    except Exception as exc:
        log.error(
            "escalation alert failed",
            extra={"escalation_id": escalation_id, "error": type(exc).__name__},
        )
        return False
    # The «📝» line follows the alert, never delays it.
    await write_summary(presenter, escalation_id, now=now)
    return True


async def _summary_reply(escalation_id: int, *, now: datetime) -> str | None:
    """EXTRACT_MODEL's sentence about what the client wants, read from the
    chat's own thread (the envelope: masked, quoted as data); None when
    there is nothing to summarise or the model gave no answer."""
    async with session_scope() as session:
        escalation = await session.get(Escalation, escalation_id)
        if (
            escalation is None
            or escalation.summary is not None
            or escalation.state not in ESCALATION_OPEN_STATES
            or created_mode(escalation.history) not in SHOWN_MODES
        ):
            return None
        tg_chat_id = _escalation_chat(escalation)
        if tg_chat_id is None:
            return None
        turns = await responder.thread_turns(session, tg_chat_id)
        if not any(t.speaker == "CLIENT" for t in turns):
            return None
        try:
            return await costs.ask(
                session,
                model=settings.extract_model,
                operation=costs.SUMMARY_OPERATION,
                system=SUMMARY_PROMPT,
                user=envelope.render(turns),
                max_tokens=SUMMARY_MAX_TOKENS,
                timeout_seconds=SUMMARY_TIMEOUT_SECONDS,
                now=now,
                source_interaction_id=escalation.interaction_id,
            )
        except costs.ModelUnavailable as exc:
            log.info(
                "escalation summary not written",
                extra={"escalation_id": escalation_id, "code": str(exc)[:64]},
            )
            return None


def _last_entry(escalation: Escalation) -> dict[str, object]:
    history = escalation.history or []
    last = history[-1] if history else None
    return last if isinstance(last, dict) else {}


def _untouched_alert(escalation: Escalation) -> bool:
    """The alert on screen is a card nothing has happened to since it was
    shown (no tap's line under it, not the folded message)."""
    last = _last_entry(escalation)
    return (
        escalation.state == OPEN
        and escalation.alert_tg_message_id is not None
        and last.get("field") == ALERT_FIELD
        and last.get("code") in (ALERT_CARD, ALERT_AGAIN)
    )


async def write_summary(
    presenter: Presenter, escalation_id: int, *, now: datetime
) -> bool:
    """The alert's «📝» line (JV-18 Behaviour 2): one EXTRACT_MODEL sentence,
    written after the alert went out and kept only when it holds no figure
    (``recaps.clean_prose``; the card's figures come from SQL). Stored on the
    escalation, so every later card shows it, and the alert on screen is
    edited to carry it when nothing has happened to it since. Only for an
    escalation the owner is shown, once. True when one was stored.
    Contained: never raises, and never holds the alert back."""
    try:
        reply = await _summary_reply(escalation_id, now=now)
        summary = _clean_summary(reply)
        if summary is None:
            if reply is not None:
                log.info(
                    "escalation summary refused", extra={"escalation_id": escalation_id}
                )
            return False
        async with session_scope() as session:
            escalation = await _locked_escalation(session, escalation_id)
            if (
                escalation is None
                or escalation.summary is not None
                or escalation.state not in ESCALATION_OPEN_STATES
            ):
                return False
            edit = _untouched_alert(escalation)
            again = _last_entry(escalation).get("code") == ALERT_AGAIN
            reminder = max(1, escalation.snooze_count or 0) if again else 0
            message_id = escalation.alert_tg_message_id
            await _save(
                session,
                escalation,
                {"summary": summary},
                [_entry(SUMMARY_FIELD, None, True, by=BY_POLICY, now=now)],
            )
            card = await escalation_card(session, escalation_id, reminder=reminder)
        log.info("escalation summary written", extra={"escalation_id": escalation_id})
        if edit and card is not None and message_id is not None:
            edited = await presenter.edit(card, message_id=message_id)
            if not edited:
                log.info(
                    "escalation alert not edited with its summary",
                    extra={"escalation_id": escalation_id, "message_id": message_id},
                )
        return True
    except Exception as exc:
        log.error(
            "escalation summary failed",
            extra={"escalation_id": escalation_id, "error": type(exc).__name__},
        )
        return False


async def realert(presenter: Presenter, escalation_id: int, *, now: datetime) -> bool:
    """A snoozed escalation whose hour is up: shown again («⏰ Eslatma
    (n-marta)») as a fresh card, and open again. Contained: never raises."""
    try:
        async with session_scope() as session:
            escalation = await session.scalar(
                sa.select(Escalation)
                .where(
                    Escalation.id == escalation_id,
                    Escalation.state == SNOOZED,
                    Escalation.snoozed_until <= now,
                    Escalation.snooze_count < settings.jarvis_snooze_max,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if escalation is None:
                return False
            count = max(1, escalation.snooze_count or 0)
            # Open again first, so the card offers the snooze once more; a
            # card Telegram does not take rolls this back.
            await _save(
                session,
                escalation,
                {"state": OPEN, "snoozed_until": None},
                _state_entries(escalation, OPEN, by=BY_POLICY, now=now, code=ALERT_AGAIN),
            )
            card = await escalation_card(session, escalation.id, reminder=count)
            if card is None:
                return False
            message_id = await presenter.card(card, silent=silent_at(now))
            if message_id is None:
                # Rolled back: still snoozed and due, the next sweep tries again.
                raise _NotDelivered
            await _alerted(session, escalation, message_id, code=ALERT_AGAIN, now=now)
            await record_shown(session, card, now=now)
        log.info("escalation alerted again", extra={"escalation_id": escalation_id})
        return True
    except _NotDelivered:
        log.warning(
            "escalation reminder not delivered", extra={"escalation_id": escalation_id}
        )
        return False
    except Exception as exc:
        log.error(
            "escalation reminder failed",
            extra={"escalation_id": escalation_id, "error": type(exc).__name__},
        )
        return False


class _NotDelivered(Exception):
    """Telegram did not take the card; the transaction is rolled back."""


async def _expire_briefed(session: AsyncSession, *, now: datetime) -> list[int]:
    """Escalations the snooze limit sent to the morning brief, once a brief
    went out after that: expired, so the chat is no longer held."""
    briefed = (
        sa.exists()
        .where(
            ReminderLog.kind == BRIEF_KIND, ReminderLog.sent_at > Escalation.snoozed_until
        )
        .correlate(Escalation)
    )
    rows = list(
        await session.scalars(
            sa.select(Escalation)
            .where(
                Escalation.state == SNOOZED,
                Escalation.snooze_count >= settings.jarvis_snooze_max,
                Escalation.snoozed_until.isnot(None),
                briefed,
            )
            .order_by(Escalation.id)
            .limit(SWEEP_BATCH)
            .with_for_update(skip_locked=True)
        )
    )
    done: list[int] = []
    for escalation in rows:
        escalation_id = escalation.id
        try:
            async with session.begin_nested():
                await _save(
                    session,
                    escalation,
                    {"state": EXPIRED, "resolution": EXPIRED},
                    _state_entries(
                        escalation, EXPIRED, by=BY_EXPIRY, now=now, code=BRIEFED
                    ),
                )
            done.append(escalation_id)
        except Exception as exc:
            log.error(
                "briefed escalation not expired",
                extra={"escalation_id": escalation_id, "error": type(exc).__name__},
            )
    return done


# --- the sweep (jarvis_cards) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepResult:
    alerted: int = 0
    realerted: int = 0
    carded: int = 0
    expired: int = 0
    cap_told: bool = False


async def _ids(query: sa.Select[tuple[int]]) -> list[int]:
    async with session_scope() as session:
        return list(await session.scalars(query))


async def sweep(presenter: Presenter, *, now: datetime) -> SweepResult:
    """One ``jarvis_cards`` run: alerts still undelivered (the responder's
    run sends them at once; this only retries), snoozed escalations whose
    time is up, draft cards not shown yet, the daily-cap notice, and
    escalations a morning brief already named. Each step and each row is
    contained (rule 25)."""
    lookback = now - timedelta(hours=settings.jarvis_draft_expiry_hours)
    alerted = realerted = carded = expired = 0
    cap_told = False
    try:
        for escalation_id in await _ids(
            sa.select(Escalation.id)
            .where(
                Escalation.alerted_at.is_(None),
                Escalation.state.in_(ESCALATION_OPEN_STATES),
                Escalation.created_at >= lookback,
                # Shadow escalations are never alerted: they never fill the batch.
                _created_mode_clause().in_(SHOWN_MODES),
            )
            .order_by(Escalation.created_at, Escalation.id)
            .limit(SWEEP_BATCH)
        ):
            alerted += await alert_now(presenter, escalation_id, now=now)
    except Exception as exc:
        log.error("unalerted escalations unreadable", extra={"error": type(exc).__name__})
    try:
        for escalation_id in await _ids(
            sa.select(Escalation.id)
            .where(
                Escalation.state == SNOOZED,
                Escalation.snoozed_until <= now,
                Escalation.snooze_count < settings.jarvis_snooze_max,
            )
            .order_by(Escalation.snoozed_until, Escalation.id)
            .limit(SWEEP_BATCH)
        ):
            realerted += await realert(presenter, escalation_id, now=now)
    except Exception as exc:
        log.error("snoozed escalations unreadable", extra={"error": type(exc).__name__})
    try:
        for row_id in await _ids(
            sa.select(OutboxMessage.id)
            .where(
                OutboxMessage.state == outbox.DRAFT,
                OutboxMessage.origin == DRAFT_ORIGIN,
                OutboxMessage.card_tg_message_id.is_(None),
                sa.or_(
                    OutboxMessage.expires_at.is_(None), OutboxMessage.expires_at > now
                ),
                sa.not_(_capped_clause()),
            )
            .order_by(OutboxMessage.id)
            .limit(SWEEP_BATCH)
        ):
            carded += await card_now(presenter, row_id, now=now)
    except Exception as exc:
        log.error("uncarded drafts unreadable", extra={"error": type(exc).__name__})
    try:
        async with session_scope() as session:
            if await _capped_today(session, now):
                cap_told = await _tell_cap(session, presenter, now=now)
    except Exception as exc:
        log.error("draft cap notice failed", extra={"error": type(exc).__name__})
    try:
        async with session_scope() as session:
            expired = len(await _expire_briefed(session, now=now))
    except Exception as exc:
        log.error("briefed escalations not expired", extra={"error": type(exc).__name__})
    return SweepResult(alerted, realerted, carded, expired, cap_told)


# --- the owner's taps ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TapResult:
    """What a tap or a reply came to. ``card`` is the card as it reads now,
    for the bot to show again; ``until`` the end of a pause or a snooze."""

    code: TapCode
    card: Card | None = None
    channel: str | None = None
    name: str | None = None
    until: datetime | None = None
    # The snooze sent the escalation to the morning brief.
    to_brief: bool = False
    # The reason the outbox gave for not approving.
    detail: str | None = None


async def _name(
    session: AsyncSession, person_id: int | None, tg_chat_id: int | None
) -> str | None:
    name, _code = await _who(session, person_id, tg_chat_id)
    return name


async def _locked_row(session: AsyncSession, row_id: int) -> OutboxMessage | None:
    found: OutboxMessage | None = await session.scalar(
        sa.select(OutboxMessage)
        .where(OutboxMessage.id == row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found


async def tap_draft(
    session: AsyncSession,
    row_id: int,
    action: str,
    *,
    message_id: int | None,
    now: datetime,
) -> TapResult:
    """One button of a draft card, under the row's lock: a reply to the card
    that changes the text (and moves the card) either finished first, and
    this tap is from an old card (stale), or waits for it and finds the row
    answered. Honoured only from the card the row was last shown on; the
    outbox re-validates the row, and a row that is no longer a draft is
    answered (the second tap)."""
    row = await _locked_row(session, row_id)
    if row is None or action not in DRAFT_ACTIONS:
        return TapResult("answered")
    if (
        row.state == outbox.EXPIRED
        or message_id is None
        or message_id != row.card_tg_message_id
    ):
        return TapResult("stale")
    if row.state != outbox.DRAFT:
        return TapResult("answered")
    name = await _name(session, row.person_id, row.tg_chat_id)
    if action == EDIT:
        return TapResult("edit", channel=row.channel, name=name)
    if action == SEND:
        if not outbox.channel_send_enabled(row.channel):
            return TapResult("copy", channel=row.channel)
        try:
            approval = await outbox.approve(session, row_id, by=BY_BUTTON, now=now)
        except outbox.AlreadyAnswered:
            return TapResult("answered")
        if approval.approved:
            return TapResult("queued", channel=row.channel, name=name)
        if approval.code == "expired":
            return TapResult("stale")
        return TapResult("failed", detail=approval.code)
    try:
        await outbox.cancel(
            session,
            row_id,
            by=BY_BUTTON,
            now=now,
            code=OWNER_WILL_WRITE if action == OWN else CANCELLED_CODE,
        )
    except outbox.AlreadyAnswered:
        return TapResult("answered")
    if action == CANCEL:
        return TapResult("cancelled", channel=row.channel, name=name)
    until = await chat_state.pause_ai(
        session, row.chat_key, channel=row.channel, person_id=row.person_id, now=now
    )
    return TapResult("own", channel=row.channel, name=name, until=until)


async def _locked_escalation(
    session: AsyncSession, escalation_id: int
) -> Escalation | None:
    found: Escalation | None = await session.scalar(
        sa.select(Escalation)
        .where(Escalation.id == escalation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found


async def _cancel_rows(
    session: AsyncSession,
    rows: Sequence[OutboxMessage],
    *,
    keep: int | None,
    by: str,
    code: str,
    now: datetime,
) -> None:
    """Take an escalation's other waiting replies off the table, each in a
    savepoint; one that moved on meanwhile is left alone."""
    for row in rows:
        if row.id == keep or row.state not in (outbox.DRAFT, outbox.APPROVED):
            continue
        row_id = row.id
        try:
            async with session.begin_nested():
                await outbox.cancel(session, row_id, by=by, now=now, code=code)
        except outbox.AlreadyAnswered:
            continue
        except Exception as exc:
            log.error(
                "escalation reply not cancelled",
                extra={"outbox_id": row_id, "code": code, "error": type(exc).__name__},
            )


def _answered_code(escalation: Escalation) -> TapCode:
    return "stale" if escalation.state == EXPIRED else "answered"


async def tap_escalation(
    session: AsyncSession,
    escalation_id: int,
    action: str,
    *,
    message_id: int | None,
    now: datetime,
) -> TapResult:
    """One button of an escalation alert, under the escalation's row lock.
    Honoured only from the alert the owner is looking at now; one that was
    answered, expired or closed meanwhile is stale."""
    escalation = await _locked_escalation(session, escalation_id)
    if escalation is None or action not in ESCALATION_ACTIONS:
        return TapResult("answered")
    if escalation.state not in ESCALATION_OPEN_STATES:
        return TapResult(_answered_code(escalation))
    if message_id is None or message_id != escalation.alert_tg_message_id:
        return TapResult("stale")
    name = await _name(session, escalation.person_id, _escalation_chat(escalation))
    channel = escalation.channel
    if action == SEND:
        if not outbox.channel_send_enabled(channel):
            return TapResult("copy", channel=channel)
        rows = await _reply_rows(session, escalation.id)
        pending = _pending_reply(rows)
        if pending is None:
            return TapResult("answered")
        try:
            approval = await outbox.approve(session, pending.id, by=BY_BUTTON, now=now)
        except outbox.AlreadyAnswered:
            return TapResult("answered")
        if not approval.approved:
            if approval.code == "expired":
                return TapResult("stale")
            return TapResult("failed", detail=approval.code)
        if pending.authored_by != gate.OWNER:
            # Only the holding reply was approved: the client's question is
            # still the owner's to answer (§5.3, one stall per escalation),
            # so the escalation stays open and the card keeps «✍️» and the
            # snooze; the owner's own reply or message closes it.
            await _save(
                session,
                escalation,
                {},
                [
                    _entry(
                        "stall",
                        pending.id,
                        outbox.APPROVED,
                        by=BY_BUTTON,
                        now=now,
                        code=STALL_APPROVED,
                    )
                ],
            )
            return TapResult(
                "queued",
                channel=channel,
                name=name,
                card=await escalation_card(session, escalation.id),
            )
        await _cancel_rows(
            session, rows, keep=pending.id, by=BY_BUTTON, code=SUPERSEDED, now=now
        )
        await _save(
            session,
            escalation,
            {
                "state": RESOLVED,
                "resolution": RESOLUTION_SENT,
                "answered_by": BY_BUTTON,
                "answered_at": now,
                "snoozed_until": None,
            },
            _state_entries(
                escalation, RESOLVED, by=BY_BUTTON, now=now, code=RESOLUTION_SENT
            ),
        )
        return TapResult("queued", channel=channel, name=name)
    if action == OWN:
        if escalation.state == OWNER_REPLYING:
            # A second «✍️» (a double tap racing the buttons' removal): the
            # pause it set stands as it was.
            return TapResult("answered")
        await _cancel_rows(
            session,
            await _reply_rows(session, escalation.id),
            keep=None,
            by=BY_BUTTON,
            code=OWNER_WILL_WRITE,
            now=now,
        )
        await _save(
            session,
            escalation,
            {"state": OWNER_REPLYING, "snoozed_until": None},
            _state_entries(
                escalation, OWNER_REPLYING, by=BY_BUTTON, now=now, code=OWNER_WILL_WRITE
            ),
        )
        until = await chat_state.pause_ai(
            session,
            escalation.chat_key,
            channel=channel,
            person_id=escalation.person_id,
            now=now,
        )
        return TapResult("own", channel=channel, name=name, until=until)
    if escalation.state != OPEN:
        return TapResult("answered")
    count = (escalation.snooze_count or 0) + 1
    to_brief = count >= settings.jarvis_snooze_max
    # Brief-bound, ``snoozed_until`` is the moment it was sent there; the
    # brief after that moment names it (``_expire_briefed``).
    until = now if to_brief else now + snooze_for()
    code = TO_BRIEF if to_brief else SNOOZE_FIELD
    await _save(
        session,
        escalation,
        {"state": SNOOZED, "snooze_count": count, "snoozed_until": until},
        [
            *_state_entries(escalation, SNOOZED, by=BY_BUTTON, now=now, code=code),
            _entry(SNOOZE_FIELD, count - 1, count, by=BY_BUTTON, now=now, code=code),
        ],
    )
    return TapResult(
        "snoozed",
        channel=channel,
        name=name,
        until=next_brief(now) if to_brief else until,
        to_brief=to_brief,
        card=await escalation_card(session, escalation.id),
    )


def next_brief(now: datetime) -> datetime:
    """The next MORNING_BRIEF_TIME on the owner's clock."""
    local = now.astimezone(settings.tz)
    at = datetime.combine(
        local.date(), settings.morning_brief_time_parsed, tzinfo=settings.tz
    )
    return at if at > local else at + timedelta(days=1)


# --- the owner's own words: a reply to a card --------------------------------------


@dataclass(frozen=True, slots=True)
class CardRef:
    kind: CardKind
    id: int


def _shown_on(
    history: InstrumentedAttribute[list[Any]], entry: dict[str, object]
) -> sa.ColumnElement[bool]:
    found: sa.ColumnElement[bool] = history.op("@>")(
        sa.cast(sa.literal([entry], JSONB), JSONB)
    )
    return found


async def find_card(session: AsyncSession, message_id: int) -> CardRef | None:
    """The draft or escalation a bot message showed (the current card or an
    earlier one of the same row; never the folded message, which shows many)."""
    row_id = await session.scalar(
        sa.select(OutboxMessage.id)
        .where(
            sa.or_(
                OutboxMessage.card_tg_message_id == message_id,
                _shown_on(
                    OutboxMessage.history,
                    {"field": outbox.CARD_FIELD, "new": message_id},
                ),
            )
        )
        .order_by(OutboxMessage.id.desc())
        .limit(1)
    )
    if row_id is not None:
        return CardRef(DRAFT_CARD, int(row_id))
    found = list(
        await session.scalars(
            sa.select(Escalation.id).where(
                sa.or_(
                    _shown_on(
                        Escalation.history, {"field": ALERT_FIELD, "new": message_id}
                    ),
                    Escalation.alert_tg_message_id == message_id,
                ),
                sa.not_(
                    _shown_on(
                        Escalation.history,
                        {"field": ALERT_FIELD, "new": message_id, "code": ALERT_FOLD},
                    )
                ),
            )
        )
    )
    return CardRef(ESCALATION_CARD, int(found[0])) if len(found) == 1 else None


async def owner_reply(
    session: AsyncSession,
    ref: CardRef,
    text: str,
    *,
    now: datetime,
    interaction_id: int | None = None,
) -> TapResult:
    """The owner replied to a card with their own words (typed, or the
    transcript of a voice note: ``interaction_id`` is that note). A draft
    takes them as its text; an escalation gets them as its own reply row,
    which its ✅ then sends exactly (the stall waiting is cancelled). Both
    are the owner's (``authored_by='owner'``): flagged by the guard, never
    blocked. Returns the card to show again."""
    body = text.strip()
    if not body:
        return TapResult("failed")
    if len(body) > OWNER_TEXT_MAX:
        return TapResult("too_long")
    if ref.kind == DRAFT_CARD:
        row = await session.get(OutboxMessage, ref.id, populate_existing=True)
        if row is None or row.state != outbox.DRAFT or row.origin != DRAFT_ORIGIN:
            return TapResult(
                "stale" if row is not None and row.state == outbox.EXPIRED else "answered"
            )
        try:
            await outbox.owner_text(session, ref.id, body=body, by=BY_REPLY, now=now)
        except outbox.AlreadyAnswered:
            return TapResult("answered")
        except outbox.NotPlainText:
            return TapResult("failed")
        return TapResult(
            "edited", card=await draft_card(session, ref.id), channel=row.channel
        )
    escalation = await _locked_escalation(session, ref.id)
    if escalation is None:
        return TapResult("answered")
    if escalation.state not in ESCALATION_OPEN_STATES:
        return TapResult(_answered_code(escalation))
    rows = await _reply_rows(session, escalation.id)
    mine = next(
        (r for r in rows if r.origin == REPLY_ORIGIN and r.state == outbox.DRAFT), None
    )
    try:
        if mine is not None:
            await outbox.owner_text(session, mine.id, body=body, by=BY_REPLY, now=now)
            kept = mine.id
        else:
            created = await outbox.create(
                session,
                channel=escalation.channel,
                chat_key=escalation.chat_key,
                origin=REPLY_ORIGIN,
                authored_by=gate.OWNER,
                body=body,
                now=now,
                person_id=escalation.person_id,
                trigger_interaction_id=escalation.interaction_id,
                command_interaction_id=interaction_id,
                escalation_id=escalation.id,
                by=BY_REPLY,
            )
            if created.state != outbox.DRAFT:
                log.warning(
                    "owner reply not stored as a draft",
                    extra={
                        "escalation_id": escalation.id,
                        "outbox_id": created.id,
                        "state": created.state,
                        "code": created.last_error,
                    },
                )
                return TapResult("failed", detail=created.last_error)
            kept = created.id
    except (outbox.AlreadyAnswered, outbox.NotPlainText, outbox.JarvisDisabled) as exc:
        log.info(
            "owner reply refused",
            extra={"escalation_id": escalation.id, "error": type(exc).__name__},
        )
        return TapResult("failed")
    await _cancel_rows(session, rows, keep=kept, by=BY_REPLY, code=OWNER_TEXT, now=now)
    # The owner engaged with it: open again (a snooze or «I'll write» is over).
    await _save(
        session,
        escalation,
        {"state": OPEN, "snoozed_until": None},
        _state_entries(escalation, OPEN, by=BY_REPLY, now=now, code=OWNER_TEXT),
    )
    return TapResult(
        "edited",
        card=await escalation_card(session, escalation.id),
        channel=escalation.channel,
    )


async def recarded(
    session: AsyncSession, card: Card, *, message_id: int, now: datetime
) -> None:
    """The card was shown again as a new bot message (after the owner's
    reply): taps and replies follow it there, and its warnings are on record."""
    if card.kind == DRAFT_CARD:
        await outbox.mark_carded(
            session, card.id, message_id=message_id, now=now, by=BY_REPLY
        )
    else:
        escalation = await _locked_escalation(session, card.id)
        if escalation is None:
            return
        await _save(
            session,
            escalation,
            {"alert_tg_message_id": message_id},
            [
                _entry(
                    ALERT_FIELD,
                    escalation.alert_tg_message_id,
                    message_id,
                    by=BY_REPLY,
                    now=now,
                    code=ALERT_CARD,
                )
            ],
        )
    await record_shown(session, card, now=now)


# --- the morning brief ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BriefEscalation:
    name: str | None
    reasons: tuple[str, ...]
    since: datetime


@dataclass(frozen=True, slots=True)
class BriefItems:
    """What the cards left to the morning brief: escalations the snooze
    limit sent there, and the chats whose drafts came over the daily cap
    and were never shown."""

    escalations: tuple[BriefEscalation, ...] = ()
    unanswered: tuple[str | None, ...] = ()

    def is_empty(self) -> bool:
        return not (self.escalations or self.unanswered)


async def brief_items(session: AsyncSession, *, now: datetime) -> BriefItems:
    """The brief's Jarvis lines, from SQL. The capped drafts are those of
    the last day that were never answered: still waiting, or expired
    unanswered overnight (JARVIS_DRAFT_EXPIRY_HOURS ends most of them
    before the brief), in a chat the owner has not written in since. A
    chat with no person reads as its own title."""
    escalations = await session.execute(
        sa.select(Escalation.reasons, Escalation.created_at, Person.display_name)
        .outerjoin(Person, Person.id == Escalation.person_id)
        .where(
            Escalation.state == SNOOZED,
            Escalation.snooze_count >= settings.jarvis_snooze_max,
        )
        .order_by(Escalation.created_at)
    )
    capped = await session.execute(
        sa.select(
            OutboxMessage.chat_key,
            OutboxMessage.person_id,
            sa.func.coalesce(Person.display_name, ChatMonitor.title),
        )
        .outerjoin(Person, Person.id == OutboxMessage.person_id)
        .outerjoin(ChatMonitor, ChatMonitor.tg_chat_id == OutboxMessage.tg_chat_id)
        .outerjoin(JarvisChatState, JarvisChatState.chat_key == OutboxMessage.chat_key)
        .where(
            sa.or_(
                OutboxMessage.state == outbox.DRAFT,
                sa.and_(
                    OutboxMessage.state == outbox.EXPIRED,
                    OutboxMessage.last_error == EXPIRED_CODE,
                ),
            ),
            OutboxMessage.origin == DRAFT_ORIGIN,
            OutboxMessage.card_tg_message_id.is_(None),
            OutboxMessage.created_at >= now - timedelta(days=1),
            _capped_clause(),
            sa.or_(
                JarvisChatState.owner_replied_at.is_(None),
                JarvisChatState.owner_replied_at < OutboxMessage.created_at,
            ),
        )
        .order_by(OutboxMessage.created_at, OutboxMessage.id)
    )
    names: dict[object, str | None] = {}
    for chat_key, person_id, name in capped:
        names.setdefault(person_id if person_id is not None else chat_key, name)
    return BriefItems(
        escalations=tuple(
            BriefEscalation(name, tuple(reasons or ()), since)
            for reasons, since, name in escalations
        ),
        unanswered=tuple(names.values()),
    )


__all__ = [
    "CANCEL",
    "DRAFT_ACTIONS",
    "DRAFT_CARD",
    "EDIT",
    "ESCALATION_ACTIONS",
    "ESCALATION_CARD",
    "OWN",
    "OWNER_TEXT_MAX",
    "SEND",
    "SNOOZE",
    "Balance",
    "BriefEscalation",
    "BriefItems",
    "Card",
    "CardRef",
    "ClientLine",
    "Presenter",
    "SafetyNote",
    "SweepResult",
    "TapResult",
    "alert_now",
    "alerts_today",
    "brief_items",
    "card_now",
    "created_mode",
    "draft_card",
    "escalation_card",
    "find_card",
    "next_brief",
    "owner_reply",
    "realert",
    "recarded",
    "record_shown",
    "silent_at",
    "sweep",
    "tap_draft",
    "tap_escalation",
]
