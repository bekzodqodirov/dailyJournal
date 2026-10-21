"""The rollout controller: per-intent graduation, auto mode and demotion
(docs/jarvis-plan.md §6.4, JV-23).

Autonomy grows one channel × intent at a time, and only on the owner's tap.

* **Metrics** (``metrics``), counted in SQL over ``outbox_messages`` since
  the window began (the channel × intent's last mode change or incident):
  the days since its first draft card, the drafts the owner decided on, how
  many of them they approved with the AI's words unchanged, and whether the
  channel's breaker tripped in the last 7 days. Drafts the statement
  allowlist sent to the card (``unapproved_statement``, ``free_question``)
  count like any other.
* **The offer** (``offers_due``, the daily ``jarvis_rollout`` job): an intent
  that may ever be autonomous (``policy.AUTO_INTENTS``: a greeting,
  information gathering, a general question), still in ``draft``, with at
  least JARVIS_GRADUATION_MIN_DAYS days, JARVIS_GRADUATION_MIN_DRAFTS
  drafts and JARVIS_GRADUATION_MIN_APPROVAL_PCT % unchanged, no «Bu xato
  edi», no red-line incident and no breaker trip, is offered with
  ``jg:y`` / ``jg:n``, at most once every JARVIS_GRADUATION_MIN_DAYS days.
  Nothing promotes silently: only ``accept``, the owner's «✅ Ha,
  avtomatik» on the offer they were shown, re-checked under the scope's row
  lock, stores ``auto``, and the env ceiling JARVIS_MODE must be ``auto``
  too (the bot shows the hint otherwise). A price, a deadline, a promise, a
  payment, a complaint, an identity question, a legal matter, other people
  and the holding reply (``stall``) are never autonomous: ``accept`` and
  ``policy.set_mode`` refuse them.
* **Demotion**, automatic and immediate (§6.4): the owner's «↩️ Bu xato edi»
  under the silent notice of an autonomous send (``mistake``), a red line
  found in an autonomous send by today's guard (``recheck_sends``), a
  breaker trip (``policy.trip_breaker``) and the cost cap reached on two
  days of a week (``check_cost_cap``). Each takes the channel × intent back
  to ``draft`` and restarts its window; the owner is told once
  (``demotions_untold``). An approved row not sent yet goes back to the
  owner's card at the claim (``gate`` re-resolves the mode).
* **Review cases** for the red-line corpus: the text of an autonomous send
  the owner called a mistake, of one that breaks a red line today, and of a
  draft whose sentences the owner took out in their edit when those carried
  a red-line signal or a free-form statement (``note_owner_edit``). They
  are kept on the outbox row (``outbox.REVIEW_FIELD``) for whoever extends
  ``tests/fixtures/redline_corpus.py``; one with a red-line signal is an
  incident too.

The rollout's own state is the ``history`` of each ``channel:<c>:intent:<i>``
scope of ``jarvis_policy``: its offers, incidents, mode changes and told
demotions. Functions take the caller's session and never commit; every step
that runs over many rows contains each one (rule 25).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import JarvisPolicy, OutboxMessage, Person
from miya.jarvis import caps, chatkey, costs, gate, outbox, policy, redlines
from miya.jarvis.policy import Mode
from miya.services.reminders import day_start

log = logging.getLogger(__name__)

# The intents that may graduate, in the order the owner sees them.
GRADUATING: Final[tuple[str, ...]] = ("greeting", "info_gathering", "general_question")
# Never autonomous, whatever is set (§6.2 as JV-23 binds it); in the order
# /jarvis rejim lists them.
NEVER_AUTO_INTENTS: Final[tuple[str, ...]] = (
    "price",
    "discount",
    "payment_terms",
    "deadline",
    "promise",
    "guarantee",
    "payment_claim",
    "identity",
    "complaint",
    "legal",
    "other_people",
    "stall",
    "other",
)

# The button letters: ``jg:y|n:<channel>:<intent>`` under an offer and
# ``jv:x:<outbox id>`` under the notice of an autonomous send (keyboards.py
# builds the payloads from the same letters).
ACCEPT_ACTION: Final = "y"
DECLINE_ACTION: Final = "n"
MISTAKE_ACTION: Final = "x"

BY_BUTTON: Final = "button"
BY_REPLY: Final = "reply"
BY_POLICY: Final = "auto:policy"
# history fields of a channel × intent scope.
OFFER_FIELD: Final = "offer"
INCIDENT_FIELD: Final = "incident"
TOLD_FIELD: Final = "demotion_told"
MODE_FIELD: Final = "mode"
# The offer's codes: shown, and the owner's answer.
OFFERED: Final = "offered"
ACCEPTED: Final = "accepted"
DECLINED: Final = "declined"
GRADUATED: Final = "graduated"
# Why an intent went back to draft (the mode entry's code).
MISTAKE: Final = "mistake"
BREAKER: Final = "breaker"
INCIDENT: Final = "incident"
COST_CAP: Final = "cost_cap"
DEMOTION_CODES: Final = (MISTAKE, BREAKER, INCIDENT, COST_CAP)
# An incident code of an owner's edit that took a red-line sentence out.
EDIT_INCIDENT: Final = "owner_edit"
# The breaker trip that holds graduation back, and the cost-cap rule.
BREAKER_LOOKBACK: Final = timedelta(days=7)
COST_WEEK_DAYS: Final = 7
COST_CAP_HITS: Final = 2
DRAFT_ORIGIN: Final = "auto_reply"
# The signals of a sentence the owner took out that make it a red-line case.
RED_SIGNALS: Final = frozenset(
    {
        "price",
        "discount",
        "payment_terms",
        "deadline",
        "promise",
        "guarantee",
        "payment_claim",
    }
)
STATEMENT_REASON: Final = "statement"


def scope_of(channel: str, intent: str) -> str:
    return policy.channel_intent_scope(channel, intent)


def _at(entry: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return None


def _entries(history: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [e for e in history or [] if isinstance(e, dict)]


def window_start(history: Iterable[Any] | None) -> datetime | None:
    """When a channel × intent's metrics start: its last mode change or
    incident (a demotion and a mistake both restart the clock)."""
    latest: datetime | None = None
    for entry in _entries(history):
        if entry.get("field") not in (MODE_FIELD, INCIDENT_FIELD):
            continue
        at = _at(entry)
        if at is not None and (latest is None or at > latest):
            latest = at
    return latest


def _breaker_since(history: Iterable[Any] | None, since: datetime) -> bool:
    for entry in _entries(history):
        if entry.get("field") != "breaker" or entry.get("new") is None:
            continue
        at = _at(entry)
        if at is not None and at >= since:
            return True
    return False


def pending_offer(history: Iterable[Any] | None) -> int | None:
    """The bot message id of the offer still waiting for the owner, if any."""
    for entry in reversed(_entries(history)):
        if entry.get("field") != OFFER_FIELD:
            continue
        value = entry.get("new")
        if entry.get("code") == OFFERED and isinstance(value, int):
            return value
        return None
    return None


def last_offer_at(history: Iterable[Any] | None) -> datetime | None:
    for entry in reversed(_entries(history)):
        if entry.get("field") == OFFER_FIELD:
            return _at(entry)
    return None


# --- metrics ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metrics:
    """One channel × intent's graduation numbers (§6.4), plain values."""

    channel: str
    intent: str
    stored: Mode
    effective: Mode
    since: datetime | None = None
    first_draft_at: datetime | None = None
    days: int = 0
    # Drafts the owner decided on (sent, edited, cancelled, expired …), and
    # how many of them they approved with the AI's words unchanged.
    drafts: int = 0
    unchanged: int = 0
    # Drafts still waiting for the owner: not counted either way.
    pending: int = 0
    breaker_recent: bool = False
    # The cost cap reached on COST_CAP_HITS days of the week: the next daily
    # job would demote the intent again, so it is not offered and a tap does
    # not promote it.
    cost_capped: bool = False
    last_offer_at: datetime | None = None

    @property
    def pct(self) -> int:
        """Approved unchanged, in whole percent, rounded down."""
        return (self.unchanged * 100) // self.drafts if self.drafts else 0

    @property
    def graduating(self) -> bool:
        return self.intent in policy.AUTO_INTENTS

    def ready(self) -> bool:
        """Every criterion of §6.4 holds (incidents restart the window, so
        none is in it) and the intent is in draft mode now."""
        return (
            self.graduating
            and self.stored == "draft"
            and self.days >= settings.jarvis_graduation_min_days
            and self.drafts >= settings.jarvis_graduation_min_drafts
            and self.pct >= settings.jarvis_graduation_min_approval_pct
            and not self.breaker_recent
            and not self.cost_capped
        )


async def _scope_history(session: AsyncSession, scope: str) -> list[Any]:
    row = await session.get(JarvisPolicy, scope, populate_existing=True)
    return list(row.history or []) if row is not None else []


async def metrics(
    session: AsyncSession,
    channel: str,
    intent: str,
    *,
    now: datetime,
    policies: policy.Policies | None = None,
    cost_capped: bool | None = None,
) -> Metrics:
    """The graduation numbers of one channel × intent, from SQL
    (``cost_capped`` when the caller counted the cost-cap days already)."""
    policies = policies if policies is not None else await policy.load(session)
    if cost_capped is None:
        cost_capped = await cost_cap_days(session, now=now) >= COST_CAP_HITS
    history = await _scope_history(session, scope_of(channel, intent))
    channel_history = await _scope_history(session, policy.channel_scope(channel))
    since = window_start(history)
    unchanged = sa.and_(
        OutboxMessage.approved_by.is_not(None),
        OutboxMessage.approved_by != outbox.AUTO_APPROVER,
        OutboxMessage.authored_by == gate.AI,
    )
    decided = OutboxMessage.state != outbox.DRAFT
    where: list[sa.ColumnElement[bool]] = [
        OutboxMessage.channel == channel,
        OutboxMessage.intent == intent,
        OutboxMessage.origin == DRAFT_ORIGIN,
        caps.became(outbox.DRAFT),
    ]
    if since is not None:
        where.append(OutboxMessage.created_at >= since)
    first, drafts, kept, pending = (
        await session.execute(
            sa.select(
                sa.func.min(OutboxMessage.created_at),
                sa.func.count().filter(decided),
                sa.func.count().filter(decided, unchanged),
                sa.func.count().filter(OutboxMessage.state == outbox.DRAFT),
            ).where(*where)
        )
    ).one()
    stop = policies.get(policy.channel_scope(channel))
    tripped = stop is not None and stop.tripped(now)
    stored = policy.stored_mode(policies, channel, intent)
    return Metrics(
        channel=channel,
        intent=intent,
        stored=stored,
        effective=policy.effective_mode(
            policies, channel, intent, env_ceiling=policy.ceiling()
        ),
        since=since,
        first_draft_at=first,
        days=max((now - first).days, 0) if first is not None else 0,
        drafts=int(drafts or 0),
        unchanged=int(kept or 0),
        pending=int(pending or 0),
        breaker_recent=tripped or _breaker_since(channel_history, now - BREAKER_LOOKBACK),
        cost_capped=cost_capped,
        last_offer_at=last_offer_at(history),
    )


async def all_metrics(session: AsyncSession, *, now: datetime) -> list[Metrics]:
    """Every channel × graduating intent."""
    policies = await policy.load(session)
    capped = await cost_cap_days(session, now=now) >= COST_CAP_HITS
    return [
        await metrics(
            session, channel, intent, now=now, policies=policies, cost_capped=capped
        )
        for channel in chatkey.CHANNELS
        for intent in GRADUATING
    ]


def offer_due(found: Metrics, now: datetime) -> bool:
    """Ready, with drafts shown at all (the ceiling at draft or auto), and
    not offered in the last JARVIS_GRADUATION_MIN_DAYS days."""
    if not found.ready() or policy.ceiling() not in ("draft", "auto"):
        return False
    last = found.last_offer_at
    return last is None or now - last >= timedelta(
        days=settings.jarvis_graduation_min_days
    )


async def offers_due(session: AsyncSession, *, now: datetime) -> list[Metrics]:
    """The channel × intents to offer for promotion today."""
    return [m for m in await all_metrics(session, now=now) if offer_due(m, now)]


async def record_offer(
    session: AsyncSession,
    channel: str,
    intent: str,
    *,
    message_id: int,
    now: datetime,
) -> None:
    """The offer reached the owner as ``message_id``: only a tap there counts."""
    await policy.note(
        session,
        scope=scope_of(channel, intent),
        field=OFFER_FIELD,
        old=None,
        new=message_id,
        by=BY_POLICY,
        now=now,
        code=OFFERED,
    )


# --- the owner's answer -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Graduation:
    """What a ``jg:`` tap came to: ``promoted``, ``declined``, ``never_auto``,
    ``not_ready`` (something changed since the offer) or ``stale`` (an old
    offer, or one answered already). ``ceiling_ok``: JARVIS_MODE is auto."""

    code: str
    channel: str
    intent: str
    metrics: Metrics | None = None
    ceiling_ok: bool = False


async def accept(
    session: AsyncSession,
    channel: str,
    intent: str,
    *,
    message_id: int | None,
    now: datetime,
) -> Graduation:
    """«✅ Ha, avtomatik»: the owner's tap on the offer they were shown, the
    only way an intent becomes ``auto``. Under the scope's row lock the
    criteria are read again; a never-auto intent is refused whatever the
    payload says."""
    if intent not in policy.AUTO_INTENTS or channel not in chatkey.CHANNELS:
        return Graduation(policy.NEVER_AUTO, channel, intent)
    scope = scope_of(channel, intent)
    row = await policy.lock_scope(session, scope)
    if message_id is None or pending_offer(row.history) != message_id:
        return Graduation("stale", channel, intent)
    found = await metrics(session, channel, intent, now=now)
    if not found.ready():
        await policy.note(
            session,
            scope=scope,
            field=OFFER_FIELD,
            old=message_id,
            new=None,
            by=BY_BUTTON,
            now=now,
            code="not_ready",
        )
        return Graduation("not_ready", channel, intent, found)
    await policy.set_mode(
        session, scope=scope, mode="auto", by=BY_BUTTON, now=now, code=GRADUATED
    )
    await policy.note(
        session,
        scope=scope,
        field=OFFER_FIELD,
        old=message_id,
        new=ACCEPTED,
        by=BY_BUTTON,
        now=now,
        code=ACCEPTED,
    )
    log.info("jarvis intent graduated", extra={"channel": channel, "intent": intent})
    return Graduation(
        "promoted", channel, intent, found, ceiling_ok=policy.ceiling() == "auto"
    )


async def decline(
    session: AsyncSession,
    channel: str,
    intent: str,
    *,
    message_id: int | None,
    now: datetime,
) -> Graduation:
    """«⏳ Yana kuzataylik»: the intent stays a draft; the next offer comes
    no sooner than JARVIS_GRADUATION_MIN_DAYS days from now."""
    if intent not in policy.AUTO_INTENTS or channel not in chatkey.CHANNELS:
        return Graduation(policy.NEVER_AUTO, channel, intent)
    scope = scope_of(channel, intent)
    row = await policy.lock_scope(session, scope)
    if message_id is None or pending_offer(row.history) != message_id:
        return Graduation("stale", channel, intent)
    await policy.note(
        session,
        scope=scope,
        field=OFFER_FIELD,
        old=message_id,
        new=DECLINED,
        by=BY_BUTTON,
        now=now,
        code=DECLINED,
    )
    return Graduation("declined", channel, intent)


# --- demotion ---------------------------------------------------------------------------


async def record_incident(
    session: AsyncSession,
    *,
    channel: str,
    intent: str,
    code: str,
    by: str,
    now: datetime,
) -> None:
    """An incident of one channel × intent: its window starts again."""
    await policy.note(
        session,
        scope=scope_of(channel, intent),
        field=INCIDENT_FIELD,
        old=None,
        new=code,
        by=by,
        now=now,
        code=code,
    )


async def mark_demotion_told(
    session: AsyncSession, channel: str, intent: str, *, code: str, now: datetime
) -> None:
    await policy.note(
        session,
        scope=scope_of(channel, intent),
        field=TOLD_FIELD,
        old=None,
        new=code,
        by=BY_POLICY,
        now=now,
    )


@dataclass(frozen=True, slots=True)
class Mistake:
    """What «↩️ Bu xato edi» came to: ``demoted``, ``stale`` or ``answered``."""

    code: str
    channel: str | None = None
    intent: str | None = None


async def mistake(
    session: AsyncSession, row_id: int, *, message_id: int | None, now: datetime
) -> Mistake:
    """The owner's «↩️ Bu xato edi» under an autonomous send's notice: the
    row is kept for the corpus review, and its channel × intent goes back to
    draft at once with its window restarted. The tap's answer tells the
    owner, so the demotion is marked told."""
    try:
        row = await outbox.flag_mistake(
            session, row_id, message_id=message_id, by=BY_BUTTON, now=now
        )
    except outbox.StaleTap:
        return Mistake("stale")
    except outbox.AlreadyAnswered:
        return Mistake("answered")
    channel, intent = row.channel, row.intent or "other"
    await policy.demote(
        session, channel=channel, intent=intent, code=MISTAKE, by=BY_BUTTON, now=now
    )
    await record_incident(
        session, channel=channel, intent=intent, code=MISTAKE, by=BY_BUTTON, now=now
    )
    await mark_demotion_told(session, channel, intent, code=MISTAKE, now=now)
    return Mistake("demoted", channel, intent)


@dataclass(frozen=True, slots=True)
class Demotion:
    """An automatic demotion the owner has not been told about."""

    channel: str
    intent: str
    code: str
    at: datetime | None


def _untold(scope: str, history: Iterable[Any] | None) -> Demotion | None:
    found: Demotion | None = None
    parts = scope.split(":")
    if len(parts) != 4 or parts[0] != "channel" or parts[2] != "intent":
        return None
    for entry in _entries(history):
        field = entry.get("field")
        code = entry.get("code")
        if (
            field == MODE_FIELD
            and entry.get("new") == policy.DEMOTED
            and code in DEMOTION_CODES
        ):
            found = Demotion(parts[1], parts[3], str(code), _at(entry))
        elif field == TOLD_FIELD or (field == MODE_FIELD and entry.get("new") == "auto"):
            found = None
    return found


async def demotions_untold(session: AsyncSession) -> list[Demotion]:
    """Automatic demotions (a breaker, an incident, the cost cap) the owner
    has not been told about yet."""
    rows = await session.execute(
        sa.select(JarvisPolicy.scope, JarvisPolicy.history)
        .where(JarvisPolicy.scope.like("channel:%:intent:%"))
        .order_by(JarvisPolicy.scope)
    )
    found: list[Demotion] = []
    for scope, history in rows:
        demotion = _untold(str(scope), history)
        if demotion is not None:
            found.append(demotion)
    return found


async def recheck_sends(session: AsyncSession, *, now: datetime) -> list[Demotion]:
    """Every autonomous send of the last week read again with today's guard:
    a red line it breaks now is an incident found after the send, which
    demotes its intent (§6.4). Each row is contained (rule 25)."""
    demoted: list[Demotion] = []
    last = 0
    while page := await outbox.sent_auto_ids(session, now=now, after=last):
        last = page[-1]
        for row_id in page:
            await _recheck_one(session, row_id, now=now, demoted=demoted)
    return demoted


async def _recheck_one(
    session: AsyncSession, row_id: int, *, now: datetime, demoted: list[Demotion]
) -> None:
    """One autonomous send read again (``recheck_sends``), in its own
    savepoint: a failure is logged with the row id and never raises."""
    try:
        async with session.begin_nested():
            row = await outbox.recheck_sent(session, row_id, now=now)
            if row is None:
                return
            intent = row.intent or "other"
            await record_incident(
                session,
                channel=row.channel,
                intent=intent,
                code=INCIDENT,
                by=BY_POLICY,
                now=now,
            )
            if await policy.demote(
                session,
                channel=row.channel,
                intent=intent,
                code=INCIDENT,
                by=BY_POLICY,
                now=now,
            ):
                demoted.append(Demotion(row.channel, intent, INCIDENT, now))
    except Exception as exc:
        log.error(
            "autonomous send recheck failed",
            extra={"outbox_id": row_id, "error": type(exc).__name__},
        )


async def cost_cap_days(session: AsyncSession, *, now: datetime) -> int:
    """On how many of the last COST_WEEK_DAYS days (today included) Jarvis's
    spend reached JARVIS_DAILY_COST_CAP_USD (a day with no Jarvis call at
    all never counts)."""
    hits = 0
    start = day_start(now)
    for back in range(COST_WEEK_DAYS):
        day = day_start(start - timedelta(days=back) + timedelta(hours=12))
        spend = await costs.spend_between(session, day, caps.next_day(day))
        spent = spend.usd > 0 or spend.unpriced > 0
        if spent and costs.cap_reached(spend, settings.jarvis_daily_cost_cap_usd):
            hits += 1
    return hits


async def check_cost_cap(session: AsyncSession, *, now: datetime) -> list[Demotion]:
    """The cost cap reached on COST_CAP_HITS days of a week demotes every
    channel's autonomous intents (§6.4)."""
    if await cost_cap_days(session, now=now) < COST_CAP_HITS:
        return []
    demoted: list[Demotion] = []
    for channel in chatkey.CHANNELS:
        try:
            async with session.begin_nested():
                for intent in await policy.demote_channel(
                    session, channel=channel, code=COST_CAP, now=now
                ):
                    demoted.append(Demotion(channel, intent, COST_CAP, now))
        except Exception as exc:
            log.error(
                "cost-cap demotion failed",
                extra={"channel": channel, "error": type(exc).__name__},
            )
    return demoted


# --- the owner's edits: red-line review cases -------------------------------------------


def edit_reasons(
    ai_body: str,
    owner_body: str,
    *,
    allowed_names: Iterable[str] = (),
    allowed_tokens: frozenset[str] = frozenset(),
    approved_kb: Iterable[str] = (),
) -> tuple[tuple[str, ...], bool]:
    """Why an owner's edit of an AI draft is a case for the red-line corpus
    review: the sentences of the AI's text the owner took out or changed,
    read for red-line signals (a price, a deadline, a promise, a payment)
    and for free-form statements, with the context the AI row was guarded
    with (the counterparty's names and the owner's aliases, the chat's
    codes, the approved KB answers). Returns (reasons, red line): no
    reasons, nothing to keep; a red-line signal is an incident too."""
    names = tuple(allowed_names)
    kb_texts = tuple(approved_kb)
    kept = {redlines.sentence_key(s) for s in redlines.sentences(owner_body)}
    removed = [
        s for s in redlines.sentences(ai_body) if redlines.sentence_key(s) not in kept
    ]
    if not removed:
        return (), False
    signals = sorted(
        str(signal)
        for signal in redlines.classify_inbound(" ".join(removed))
        if signal in RED_SIGNALS
    )
    statement = any(
        "statement"
        in redlines.check_shapes(
            s,
            allowed_names=names,
            allowed_tokens=allowed_tokens,
            approved_kb=kb_texts,
        ).shapes
        for s in removed
    )
    reasons = [*signals, *([STATEMENT_REASON] if statement else [])]
    return tuple(reasons), bool(signals)


async def _edit_names(session: AsyncSession, row: OutboxMessage) -> tuple[str, ...]:
    """The names the AI's text of ``row`` was guarded with: the owner's
    aliases and the counterparty's names (``outbox.guard_names`` gives none
    once the row is the owner's)."""
    names = list(settings.owner_aliases_parsed)
    if row.person_id is not None:
        person = await session.get(Person, row.person_id)
        if person is not None:
            names.extend([person.display_name, *(person.aliases or [])])
    return tuple(n for n in names if n)


async def note_owner_edit(session: AsyncSession, row_id: int, *, now: datetime) -> bool:
    """After the owner's words replaced an AI draft (``outbox.owner_text``):
    keep it as a red-line review case when the edit took out a red-line
    signal or a free-form statement, once per row; a red-line signal is an
    incident of its channel × intent. Contained: it never raises into the
    owner's reply (rule 25). True when a case was kept."""
    try:
        async with session.begin_nested():
            row = await session.get(OutboxMessage, row_id, populate_existing=True)
            if (
                row is None
                or row.origin != DRAFT_ORIGIN
                or row.authored_by != gate.OWNER
                or not row.ai_body
            ):
                return False
            for entry in _entries(row.history):
                if (
                    entry.get("field") == outbox.REVIEW_FIELD
                    and entry.get("code") == outbox.REVIEW_EDIT
                ):
                    return False
            reasons, red = edit_reasons(
                row.ai_body,
                row.body,
                allowed_names=await _edit_names(session, row),
                allowed_tokens=await outbox.allowed_tokens(session, row),
                approved_kb=tuple((await outbox.owner_kb_texts(session)).values()),
            )
            if not reasons:
                return False
            await outbox.note_review(
                session,
                row_id,
                reasons=reasons,
                code=outbox.REVIEW_EDIT,
                by=BY_REPLY,
                now=now,
            )
            if red and row.intent:
                await record_incident(
                    session,
                    channel=row.channel,
                    intent=row.intent,
                    code=EDIT_INCIDENT,
                    by=BY_REPLY,
                    now=now,
                )
            log.info(
                "owner edit kept for the red-line review",
                extra={"outbox_id": row_id, "red_line": red},
            )
            return True
    except Exception as exc:
        log.error(
            "owner edit review failed",
            extra={"outbox_id": row_id, "error": type(exc).__name__},
        )
        return False


# --- what /jarvis rejim shows -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelModes:
    channel: str
    intents: tuple[Metrics, ...]


@dataclass(frozen=True, slots=True)
class Modes:
    """``/jarvis rejim``: per channel, each graduating intent's numbers and
    mode; the never-auto intents; the env ceiling; the review cases kept."""

    ceiling: Mode
    channels: tuple[ChannelModes, ...]
    never_auto: tuple[str, ...] = NEVER_AUTO_INTENTS
    reviews: int = 0


def shown_channels(found: Sequence[Metrics]) -> tuple[str, ...]:
    """Telegram, and any other channel that can send or has drafts."""
    shown: list[str] = []
    for channel in chatkey.CHANNELS:
        mine = [m for m in found if m.channel == channel]
        if (
            channel == chatkey.TELEGRAM
            or outbox.channel_send_enabled(channel)
            or any(m.drafts or m.pending or m.stored == "auto" for m in mine)
        ):
            shown.append(channel)
    return tuple(shown)


async def modes(session: AsyncSession, *, now: datetime) -> Modes:
    found = await all_metrics(session, now=now)
    channels = shown_channels(found)
    return Modes(
        ceiling=policy.ceiling(),
        channels=tuple(
            ChannelModes(channel, tuple(m for m in found if m.channel == channel))
            for channel in channels
        ),
        reviews=await outbox.review_count(session),
    )


__all__ = [
    "ACCEPTED",
    "ACCEPT_ACTION",
    "BREAKER",
    "COST_CAP",
    "DECLINED",
    "DECLINE_ACTION",
    "DEMOTION_CODES",
    "GRADUATED",
    "GRADUATING",
    "INCIDENT",
    "MISTAKE",
    "MISTAKE_ACTION",
    "NEVER_AUTO_INTENTS",
    "OFFERED",
    "OFFER_FIELD",
    "TOLD_FIELD",
    "ChannelModes",
    "Demotion",
    "Graduation",
    "Metrics",
    "Mistake",
    "Modes",
    "accept",
    "all_metrics",
    "check_cost_cap",
    "cost_cap_days",
    "decline",
    "demotions_untold",
    "edit_reasons",
    "mark_demotion_told",
    "metrics",
    "mistake",
    "modes",
    "note_owner_edit",
    "offer_due",
    "offers_due",
    "pending_offer",
    "recheck_sends",
    "record_incident",
    "record_offer",
    "scope_of",
    "shown_channels",
    "window_start",
]
