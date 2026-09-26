"""One ranked queue of everything that wants the owner's tap (WP-17).

The owner's answer 3 (2026-09-25): «tasdiq so'rasin, 5–10 ta tasdiq bosa
olaman» — ask for confirmation, but 5 to 10 taps a day is what he can give.
Before this module six jobs each decided on their own when to ask, and
nothing saw the total, so neither "ten a day" nor "money first" could hold.

Here every tap-request — a counterparty claim, a money text, a missed call,
a "Hali ochiqmi?", an unanswered question, a new-groups digest, a big file —
is collected into one list, ranked money first, then by kind, then oldest
first, and ``plan`` decides how many of them a given slot (the morning
brief, the evening report, a daytime push) may spend. ``question_log`` is
the ledger: one row per tap-request shown, so the day's spend is a count.

Stakes are ordering weights only (loops.RANK_WEIGHT_UZS); no number here is
ever rendered.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Currency
from miya.db.models import Claim, Interaction, QuestionLog, ReminderLog
from miya.services import approvals, chats, claims, loops, nudges, queries, reminders
from miya.services.brief import BRIEF_KIND

KIND_CLAIM = "claim"
KIND_MONEY = "money"
KIND_MISSED = "missed"
KIND_STILL_OPEN = "still_open"
KIND_NUDGE = "nudge"
KIND_GROUPS = "groups"
KIND_MEDIA = "media"
KIND_TIER = {
    KIND_CLAIM: 0,
    KIND_MONEY: 0,
    KIND_MISSED: 1,
    KIND_STILL_OPEN: 2,
    KIND_NUDGE: 3,
    KIND_GROUPS: 4,
    KIND_MEDIA: 5,
}

VIA_PUSH = "push"
VIA_BRIEF = "brief"
VIA_EVENING = "evening"
VIA_RECEIPT = "receipt"

SLOT_DAY = "day"
SLOT_BRIEF = "brief"
SLOT_EVENING = "evening"

# Money review rows worth one of the owner's taps: probably a completed
# payment the reader could not book alone. Declined, pending, reminders,
# adverts and unreadable amounts stay in /tekshir only.
PUSHABLE_MONEY_REASONS = (
    "conflict",
    "no_direction",
    "no_evidence",
    "otp_conflict",
    "autobook_off",
    "reversal",
)


@dataclass(slots=True, kw_only=True)
class Pending:
    kind: str
    ref: str
    # Ordering only; never rendered.
    stake_rank: Decimal
    since: datetime
    urgent: bool
    offers: int
    # Claim | MissedCall | reminders.Question | UnansweredQuestion |
    # list[ChatMonitor] | Interaction (media and money)
    subject: Any


@dataclass(slots=True)
class QueueSummary:
    waiting: int
    money: int


def rank_key(p: Pending) -> tuple:
    return (-p.stake_rank, KIND_TIER[p.kind], p.since, p.ref)


def is_urgent(kind: str, stake: Decimal) -> bool:
    return kind in (KIND_CLAIM, KIND_MISSED) and stake >= Decimal(
        settings.question_urgent_min_uzs
    )


def cost(item: Pending) -> int:
    """Slots one item spends: each listed group is its own tap-request."""
    return len(item.subject) if item.kind == KIND_GROUPS else 1


def _at_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=settings.tz)


def _pending(kind: str, ref: str, stake: Decimal, since: datetime, subject) -> Pending:
    return Pending(
        kind=kind,
        ref=ref,
        stake_rank=stake,
        since=since,
        urgent=is_urgent(kind, stake),
        offers=0,
        subject=subject,
    )


def pending_of_claim(claim: Claim, now: datetime | None = None) -> Pending:
    view = claims.view(claim)
    stake = (
        loops.rank_stake(view.amount, view.currency)
        if claim.kind
        in (claims.KIND_DEBT, claims.KIND_SETTLEMENT, claims.KIND_TRANSACTION)
        else Decimal(0)
    )
    return _pending(KIND_CLAIM, claims.ref(claim.id), stake, claim.created_at, claim)


def _still_open_since(q: reminders.Question) -> datetime:
    if q.balance is not None and q.balance.earliest_due is not None:
        return _at_midnight(q.balance.earliest_due)
    record = q.record
    due = getattr(record, "due_date", None)
    if due is not None:
        return _at_midnight(due)
    created = getattr(record, "created_at", None)
    return created or datetime.now(settings.tz)


def _money_of(row: Interaction) -> dict:
    return (row.media or {}).get("money") or {}


async def collect(
    session: AsyncSession, *, now: datetime | None = None, for_push: bool = True
) -> list[Pending]:
    """Every waiting tap-request, ranked. ``for_push`` applies each source's
    "not again yet" rule; the pull view (/savollar) shows everything."""
    now = now or datetime.now(settings.tz)
    items: list[Pending] = []

    # (a) Claims.
    for claim in await claims.askable(session, now=now, for_push=for_push):
        items.append(pending_of_claim(claim, now))

    # (b) Missed calls.
    missed = (
        await nudges.collect_missed(session, now=now)
        if for_push
        else await loops.missed_calls(session, now=now)
    )
    for m in missed:
        items.append(
            _pending(KIND_MISSED, f"m{m.interaction_id}", m.stake_rank, m.called_at, m)
        )

    # (c) Still open.
    for q in await reminders.collect_questions(session, now=now):
        stake = (
            loops.rank_stake(q.balance.outstanding, q.balance.currency)
            if q.kind == "debt" and q.balance is not None
            else Decimal(0)
        )
        items.append(
            _pending(KIND_STILL_OPEN, f"{q.kind}:{q.ref}", stake, _still_open_since(q), q)
        )

    # (d) Unanswered questions.
    nudged = (
        await nudges.collect(session, now=now)
        if for_push
        else await nudges.unanswered_questions(session, now=now)
    )
    for q in nudged:
        items.append(
            _pending(KIND_NUDGE, f"q{q.interaction_id}", q.stake_rank, q.asked_at, q)
        )

    # (e) New groups: one digest, each listed group its own tap.
    monitors = await chats.awaiting_join_question(
        session,
        now=now,
        limit=settings.question_group_digest_size,
        for_push=for_push,
    )
    if monitors:
        since = min((m.last_seen_at or m.updated_at) for m in monitors)
        day = now.astimezone(settings.tz).date().isoformat()
        items.append(_pending(KIND_GROUPS, f"digest:{day}", Decimal(0), since, monitors))

    # (f) Big files.
    for row in await approvals.awaiting_question(
        session, limit=20, pushable_only=for_push
    ):
        items.append(_pending(KIND_MEDIA, f"i{row.id}", Decimal(0), row.occurred_at, row))

    # (g) Money texts the reader could not book alone.
    rows, _ = await queries.flagged_money(session, limit=50)
    money_rows = [
        row
        for row in rows
        if _money_of(row).get("amount")
        and (not for_push or _money_of(row).get("reason") in PUSHABLE_MONEY_REASONS)
    ]
    if for_push and money_rows:
        offered = await offers_of(session, KIND_MONEY, [f"s{r.id}" for r in money_rows])
        money_rows = [
            r
            for r in money_rows
            if f"s{r.id}" not in offered and not _money_of(r).get("asked_at")
        ]
    for row in money_rows:
        money = _money_of(row)
        stake = loops.rank_stake(
            Decimal(money["amount"]), Currency(money.get("currency") or "UZS")
        )
        items.append(_pending(KIND_MONEY, f"s{row.id}", stake, row.occurred_at, row))

    # How often each was offered before, for the renderers' "yana" marks.
    by_kind: dict[str, list[Pending]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    for kind, group in by_kind.items():
        if kind == KIND_GROUPS:
            continue
        counts = await offers_of(session, kind, [p.ref for p in group])
        for item in group:
            item.offers = counts.get(item.ref, (0, None))[0]

    return sorted(items, key=rank_key)


async def offers_of(
    session: AsyncSession, kind: str, refs: list[str]
) -> dict[str, tuple[int, datetime]]:
    """How many times each ref was shown, and when last."""
    if not refs:
        return {}
    rows = await session.execute(
        sa.select(QuestionLog.ref, sa.func.count(), sa.func.max(QuestionLog.sent_at))
        .where(QuestionLog.kind == kind)
        .where(QuestionLog.ref.in_(refs))
        .group_by(QuestionLog.ref)
    )
    return {ref: (int(count), last) for ref, count, last in rows.all()}


async def spent_today(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Tap-requests shown since midnight in Tashkent."""
    now = now or datetime.now(settings.tz)
    return int(
        await session.scalar(
            sa.select(sa.func.count(QuestionLog.id)).where(
                QuestionLog.sent_at >= reminders.day_start(now)
            )
        )
        or 0
    )


async def last_push_at(session: AsyncSession, *, now: datetime) -> datetime | None:
    return await session.scalar(
        sa.select(sa.func.max(QuestionLog.sent_at))
        .where(QuestionLog.sent_at >= reminders.day_start(now))
        .where(QuestionLog.via.in_((VIA_PUSH, VIA_RECEIPT)))
    )


async def brief_sent_today(session: AsyncSession, *, now: datetime) -> bool:
    found = await session.scalar(
        sa.select(ReminderLog.id)
        .where(
            ReminderLog.kind == BRIEF_KIND,
            ReminderLog.ref == now.astimezone(settings.tz).date().isoformat(),
        )
        .limit(1)
    )
    return found is not None


def _take(ranked: list[Pending], room: int) -> list[Pending]:
    """Greedy, in rank order, counting slots; a groups digest that does not
    fit whole is cut to the room left."""
    picked: list[Pending] = []
    left = room
    for item in ranked:
        if left < 1:
            break
        if item.kind == KIND_GROUPS and cost(item) > left:
            item = dataclasses.replace(item, subject=item.subject[:left])
        picked.append(item)
        left -= cost(item)
    return picked


def plan(
    pending: list[Pending],
    *,
    spent: int,
    slot: str,
    now: datetime,
    last_push: datetime | None,
    brief_sent: bool,
    interrupting: bool = True,
) -> list[Pending]:
    """How many of the waiting items this slot may show. Pure."""
    remaining = max(0, settings.question_budget_per_day - spent)
    if remaining == 0:
        return []
    batch_max = settings.question_batch_max
    ranked = sorted(pending, key=rank_key)
    if slot == SLOT_BRIEF:
        return _take(ranked, min(remaining, settings.question_brief_slots, batch_max))
    if slot == SLOT_EVENING:
        return _take(ranked, min(remaining, settings.question_evening_slots, batch_max))

    local = now.astimezone(settings.tz)
    report_at = datetime.combine(
        local.date(), settings.report_time_parsed, tzinfo=settings.tz
    )
    reserve = settings.question_evening_slots if now < report_at else 0
    urgent = [p for p in ranked if p.urgent]
    picked = _take(urgent, min(remaining, batch_max))
    used = sum(cost(p) for p in picked)
    gap_ok = (
        not interrupting
        or last_push is None
        or now - last_push >= timedelta(minutes=settings.question_push_gap_minutes)
    )
    if brief_sent and now < report_at and gap_ok:
        room = min(batch_max - used, remaining - reserve - used)
        if room > 0:
            rest = [p for p in ranked if not p.urgent]
            picked += _take(rest, room)
    return sorted(picked, key=rank_key)


async def record_shown(
    session: AsyncSession,
    items: list[Pending],
    *,
    via: str,
    now: datetime,
    tg_message_id: int | None = None,
) -> None:
    """Log each shown tap-request and apply its source's own "asked" mark."""
    for item in items:
        if item.kind == KIND_GROUPS:
            for monitor in item.subject:
                chats.mark_offered(monitor, now=now)
                session.add(
                    QuestionLog(
                        kind=KIND_GROUPS,
                        ref=f"g{monitor.id}",
                        via=via,
                        tg_message_id=tg_message_id,
                        sent_at=now,
                    )
                )
            continue
        session.add(
            QuestionLog(
                kind=item.kind,
                ref=item.ref,
                via=via,
                tg_message_id=tg_message_id,
                sent_at=now,
            )
        )
        subject = item.subject
        if item.kind == KIND_CLAIM:
            claims.mark_asked(subject, now=now)
        elif item.kind == KIND_MONEY:
            money = _money_of(subject)
            subject.media = {
                **(subject.media or {}),
                "money": {**money, "asked_at": now.isoformat()},
            }
        elif item.kind == KIND_MISSED:
            nudges.mark_missed_nudged(session, [subject])
        elif item.kind == KIND_NUDGE:
            nudges.mark_nudged(session, [subject])
        elif item.kind == KIND_STILL_OPEN:
            session.add(
                ReminderLog(kind=reminders.ask_kind(subject.kind), ref=subject.ref)
            )
        elif item.kind == KIND_MEDIA:
            approvals.set_state(subject, approvals.ASKED, shown_at=now.isoformat())
    await session.flush()


def summarise(pending: list[Pending], shown: list[Pending]) -> QueueSummary:
    shown_refs = {p.ref for p in shown}
    waiting = [p for p in pending if p.ref not in shown_refs]
    return QueueSummary(
        waiting=len(waiting), money=sum(1 for p in waiting if p.stake_rank > 0)
    )


async def auto_resolve(session: AsyncSession, *, now: datetime) -> dict[str, int]:
    """Settle what needs no tap before anything is asked."""
    return {
        "media_expired": await approvals.expire_stale(session, now=now),
        "groups_decided": await chats.apply_default_rules(session, now=now),
    }
