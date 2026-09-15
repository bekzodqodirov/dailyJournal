"""Due reminders (spec §8), escalating and then stopping.

Runs hourly, never during quiet hours. An item with a due date is pinged on
the day it falls due, then at +1 day, then at +3 days; after that third ping
it is asked — "Hali ochiqmi?" with Ha / Bajarildi / Yop buttons — a week
after the due date, and the question repeats every week until it is
answered. "Ha" keeps it open and re-asks a week later. A question the owner
misses is therefore asked again, not dropped: the owner's decision is
"re-remind after one week", and an unanswered question is the one case where
silence would quietly bury the debt.

Undated promises and tasks (the same decision) get the question a week after
they were made, and every week after.

All of this state lives in ``reminder_log`` — one row per thing sent, keyed
by kind and ref — so a worker restart changes nothing and the sweep can
count what already went out:

    kind          "debt" | "promise" | "task" | "event"   a ping went out
    "ask:<kind>"  the question went out
    "ack:<kind>"  the owner answered "Ha" (still open)

A debt ref is "person:direction:currency" (a balance spans rows); a promise,
task or event ref is its row id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Debt, ReminderLog
from miya.services import queries

log = logging.getLogger(__name__)

# Days after the due date on which a ping goes out, in order.
ESCALATION_DAYS = (0, 1, 3)
# Then the question, first on this day after the due date.
ASK_AFTER_DAYS = 7
# How often the question repeats: undated items, dated ones the owner said
# are still open, and questions he never answered.
NUDGE_EVERY = timedelta(days=7)
# One "Hali ochiqmi?" message carries at most this many rows; the rest keep
# qualifying and come next sweep.
MAX_QUESTIONS = 20

PING = "ping"
ASK = "ask"


def ask_kind(kind: str) -> str:
    return f"ask:{kind}"


def ack_kind(kind: str) -> str:
    return f"ack:{kind}"


def debt_ref(person_id: int, direction, currency) -> str:
    return f"{person_id}:{direction.value}:{currency.value}"


def ref_for(kind: str, record) -> str:
    """The reminder_log ref of a row, so a button on d12 acks its balance."""
    if kind == "debt":
        debt: Debt = record
        return debt_ref(debt.person_id, debt.direction, debt.currency)
    return str(record.id)


@dataclass(slots=True)
class Question:
    """One 'Hali ochiqmi?' line: what it is about and which rows its buttons hit.

    A debt question is about a balance (person, direction, currency) and its
    ``refs`` list every open row in it. The owner thinks in balances per
    person, so the line gets one answer row: "Ha" acks the balance and ✅
    settles every row of it — a two-row balance is never half-answered.
    """

    kind: str
    ref: str
    refs: list[tuple[str, int]]
    record: object = None
    person: object = None
    balance: object = None


@dataclass(slots=True)
class DueBundle:
    debts: list = field(default_factory=list)
    promises: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    events: list = field(default_factory=list)
    keys: list[tuple[str, str]] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.debts or self.promises or self.tasks or self.events or self.questions
        )


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True inside QUIET_HOURS, which normally wraps midnight (23:30–07:30)."""
    now = now or datetime.now(settings.tz)
    start, end = settings.quiet_hours_parsed
    current = now.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def day_start(now: datetime) -> datetime:
    """Midnight of ``now``'s calendar day in the owner's timezone."""
    return datetime.combine(
        now.astimezone(settings.tz).date(), time.min, tzinfo=settings.tz
    )


async def _already_sent(
    session: AsyncSession, kind: str, ref: str, now: datetime
) -> bool:
    """Was this item pinged already today (owner's calendar day)?

    Calendar day, not a rolling 24h: with an hourly sweep at minute 0, a
    ping at 15:00:01 on day D suppressed the 15:00 sweep on D+1 by one
    second, so every escalation slipped a sweep. By calendar day, "ping on
    D, D+1, D+3" means exactly those days.
    """
    cutoff = day_start(now)
    found = await session.scalar(
        sa.select(ReminderLog.id)
        .where(ReminderLog.kind == kind, ReminderLog.ref == ref)
        .where(ReminderLog.sent_at >= cutoff)
        .limit(1)
    )
    return found is not None


async def _history(
    session: AsyncSession, kind: str, ref: str
) -> list[tuple[str, datetime]]:
    """Every ping, question and answer for one item, oldest first."""
    rows = await session.execute(
        sa.select(ReminderLog.kind, ReminderLog.sent_at)
        .where(ReminderLog.kind.in_([kind, ask_kind(kind), ack_kind(kind)]))
        .where(ReminderLog.ref == ref)
        .order_by(ReminderLog.sent_at)
    )
    return [(k, sent.astimezone(settings.tz)) for k, sent in rows.all()]


def decide(
    kind: str,
    history: list[tuple[str, datetime]],
    *,
    due: date | None,
    created_at: datetime | None,
    now: datetime,
) -> str | None:
    """PING, ASK or None for one item, from what was already sent.

    Pure, so the schedule is testable without a clock: the caller supplies
    ``now`` and the log rows.
    """
    if due is None:
        # Weekly nudge: a week after it was made, then a week after whatever
        # was last sent or answered.
        last = history[-1][1] if history else created_at
        if last is None:
            return None
        return ASK if now >= last + NUDGE_EVERY else None

    since = datetime.combine(due, time.min, tzinfo=settings.tz)
    pings = [sent for k, sent in history if k == kind and sent >= since]
    asks = [sent for k, sent in history if k == ask_kind(kind) and sent >= since]
    acks = [sent for k, sent in history if k == ack_kind(kind) and sent >= since]

    if acks and (not asks or acks[-1] >= asks[-1]):
        # "Ha, still open": the item is on a weekly question from that answer.
        last = max(sent for _, sent in history if sent >= acks[-1])
        return ASK if now >= last + NUDGE_EVERY else None
    if asks:
        # Asked and never answered: quiet for a week, then the same question
        # again. Returning None here for good is how a missed "Hali ochiqmi?"
        # used to turn into a debt the owner never heard of again.
        return ASK if now >= asks[-1] + NUDGE_EVERY else None

    today = now.date()
    stage = len(pings)
    if stage < len(ESCALATION_DAYS):
        fire_on = due + timedelta(days=ESCALATION_DAYS[stage])
        return PING if today >= fire_on else None
    fire_on = due + timedelta(days=ASK_AFTER_DAYS)
    return ASK if today >= fire_on else None


async def collect_due(
    session: AsyncSession, *, now: datetime | None = None, horizon_days: int = 0
) -> DueBundle:
    """Everything worth sending this sweep: pings, plus the questions."""
    now = now or datetime.now(settings.tz)
    bundle = DueBundle()
    due = await queries.due_items(session, horizon_days=horizon_days)

    for balance in due["debts"]:
        # Direction matters: "they owe me" and "I owe them" are two different
        # reminders, and one must not suppress the other for 24h.
        ref = debt_ref(balance.person.id, balance.direction, balance.currency)
        verdict = decide(
            "debt",
            await _history(session, "debt", ref),
            due=balance.earliest_due,
            created_at=None,
            now=now,
        )
        if verdict == PING and not await _already_sent(session, "debt", ref, now):
            bundle.debts.append(balance)
            bundle.keys.append(("debt", ref))
        elif verdict == ASK:
            bundle.questions.append(
                Question(
                    "debt",
                    ref,
                    [("debt", i) for i in balance.ids],
                    person=balance.person,
                    balance=balance,
                )
            )

    for promise, person in due["promises"]:
        ref = str(promise.id)
        verdict = decide(
            "promise",
            await _history(session, "promise", ref),
            due=promise.due_date,
            created_at=promise.created_at,
            now=now,
        )
        if verdict == PING and not await _already_sent(session, "promise", ref, now):
            bundle.promises.append((promise, person))
            bundle.keys.append(("promise", ref))
        elif verdict == ASK:
            bundle.questions.append(
                Question("promise", ref, [("promise", promise.id)], promise, person)
            )

    for task in due["tasks"]:
        ref = str(task.id)
        verdict = decide(
            "task",
            await _history(session, "task", ref),
            due=task.due_date,
            created_at=task.created_at,
            now=now,
        )
        if verdict == PING and not await _already_sent(session, "task", ref, now):
            bundle.tasks.append(task)
            bundle.keys.append(("task", ref))
        elif verdict == ASK:
            bundle.questions.append(Question("task", ref, [("task", task.id)], task))

    undated = await queries.undated_open(session)
    for promise, person in undated["promises"]:
        ref = str(promise.id)
        verdict = decide(
            "promise",
            await _history(session, "promise", ref),
            due=None,
            created_at=promise.created_at.astimezone(settings.tz),
            now=now,
        )
        if verdict == ASK:
            bundle.questions.append(
                Question("promise", ref, [("promise", promise.id)], promise, person)
            )
    for task in undated["tasks"]:
        ref = str(task.id)
        verdict = decide(
            "task",
            await _history(session, "task", ref),
            due=None,
            created_at=task.created_at.astimezone(settings.tz),
            now=now,
        )
        if verdict == ASK:
            bundle.questions.append(Question("task", ref, [("task", task.id)], task))

    for event in await queries.upcoming_events(session, within_minutes=60):
        ref = str(event.id)
        if await _already_sent(session, "event", ref, now):
            continue
        bundle.events.append(event)
        bundle.keys.append(("event", ref))

    bundle.questions = bundle.questions[:MAX_QUESTIONS]
    return bundle


async def mark_sent(
    session: AsyncSession,
    bundle: DueBundle,
    *,
    rendered: dict[str, int] | None = None,
) -> None:
    """Record the ping only for the items that actually went out.

    ``rendered`` says how many of each kind the message had room for, in the
    same order they were collected. Logging a clipped item would suppress it
    for 24 hours while the next sweep rebuilt the identical head — the tail
    would never be seen at all.
    """
    seen: dict[str, int] = {}
    for kind, ref in bundle.keys:
        index = seen.get(kind, 0)
        seen[kind] = index + 1
        if rendered is not None and index >= rendered.get(kind, 0):
            continue
        session.add(ReminderLog(kind=kind, ref=ref))


async def mark_asked(
    session: AsyncSession, bundle: DueBundle, *, rendered: int | None = None
) -> None:
    """The question went out: log it so the item goes quiet until answered.

    ``rendered`` is how many questions the message actually showed (the body
    is clipped and the keyboard has at most MAX_ROWS rows). Only those are
    logged; a question the owner never saw must not go quiet for a week —
    left unlogged, it qualifies again next sweep and comes out then.
    """
    shown = bundle.questions if rendered is None else bundle.questions[:rendered]
    for question in shown:
        session.add(ReminderLog(kind=ask_kind(question.kind), ref=question.ref))


async def acknowledge(session: AsyncSession, kind: str, record) -> None:
    """The owner tapped "Ha": still open, ask again in a week."""
    session.add(ReminderLog(kind=ack_kind(kind), ref=ref_for(kind, record)))
