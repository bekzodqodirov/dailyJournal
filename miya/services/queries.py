"""Deterministic read queries (spec §8).

**Money and debt answers always come from SQL.** These functions are the only
source of financial numbers in the system; the reasoning model may phrase their
output in Uzbek but never produces the figures itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Currency, DebtDirection, DebtStatus, PromiseStatus, TaskStatus
from miya.db.models import (
    Debt,
    DebtPayment,
    Event,
    Interaction,
    Person,
    Promise,
    Task,
    Transaction,
    UsageLog,
)


@dataclass(slots=True)
class DebtBalance:
    person: Person
    direction: DebtDirection
    currency: Currency
    outstanding: Decimal
    earliest_due: date | None
    count: int


@dataclass(slots=True)
class DaySummary:
    day: date
    income: dict[Currency, Decimal] = field(default_factory=dict)
    expense: dict[Currency, Decimal] = field(default_factory=dict)
    by_category: list[tuple[str, Currency, Decimal]] = field(default_factory=list)
    people_seen: list[tuple[Person, int]] = field(default_factory=list)
    new_debts: list[Debt] = field(default_factory=list)
    new_promises: list[Promise] = field(default_factory=list)
    interactions: int = 0
    biggest: list[Transaction] = field(default_factory=list)


@dataclass(slots=True)
class PersonSummary:
    person: Person
    balances: list[DebtBalance] = field(default_factory=list)
    open_promises: list[Promise] = field(default_factory=list)
    last_interactions: list[Interaction] = field(default_factory=list)
    total_interactions: int = 0


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a local Tashkent day, as timezone-aware datetimes."""
    tz = settings.tz
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


# `debts.amount` minus everything paid against it — the outstanding balance.
_OUTSTANDING = Debt.amount - sa.func.coalesce(
    sa.select(sa.func.sum(DebtPayment.amount))
    .where(DebtPayment.debt_id == Debt.id)
    .correlate(Debt)
    .scalar_subquery(),
    0,
)


async def open_debts(
    session: AsyncSession,
    *,
    direction: DebtDirection | None = None,
    person_id: int | None = None,
) -> list[DebtBalance]:
    """Open balances grouped by person, direction and currency."""
    stmt = (
        sa.select(
            Person,
            Debt.direction,
            Debt.currency,
            sa.func.sum(_OUTSTANDING).label("outstanding"),
            sa.func.min(Debt.due_date).label("earliest_due"),
            sa.func.count(Debt.id).label("count"),
        )
        .join(Person, Person.id == Debt.person_id)
        .where(Debt.status != DebtStatus.settled)
        .group_by(Person.id, Debt.direction, Debt.currency)
        .having(sa.func.sum(_OUTSTANDING) > 0)
        .order_by(Debt.direction, sa.desc("outstanding"))
    )
    if direction is not None:
        stmt = stmt.where(Debt.direction == direction)
    if person_id is not None:
        stmt = stmt.where(Debt.person_id == person_id)

    return [
        DebtBalance(
            person=row[0],
            direction=row[1],
            currency=row[2],
            outstanding=row[3],
            earliest_due=row[4],
            count=row[5],
        )
        for row in (await session.execute(stmt)).all()
    ]


async def open_promises(
    session: AsyncSession, *, person_id: int | None = None, limit: int = 50
) -> list[tuple[Promise, Person]]:
    return await promises_by_status(
        session, status=PromiseStatus.open, person_id=person_id, limit=limit
    )


async def promises_by_status(
    session: AsyncSession,
    *,
    status: PromiseStatus,
    person_id: int | None = None,
    limit: int = 50,
) -> list[tuple[Promise, Person]]:
    stmt = (
        sa.select(Promise, Person)
        .join(Person, Person.id == Promise.person_id)
        .where(Promise.status == status)
        .order_by(Promise.due_date.nulls_last(), Promise.created_at)
        .limit(limit)
    )
    if person_id is not None:
        stmt = stmt.where(Promise.person_id == person_id)
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def _top_expenses(
    session: AsyncSession, start: datetime, end: datetime, *, per_currency: int = 3
) -> list[Transaction]:
    """Largest expenses ranked *within* each currency.

    A raw `ORDER BY amount` across currencies is meaningless — 200,000 UZS
    would outrank a $10,000 supplier payment. Ranking per currency keeps every
    currency's real top spenders in the list.
    """
    ranked = (
        sa.select(
            Transaction.id,
            sa.func.row_number()
            .over(
                partition_by=Transaction.currency,
                order_by=Transaction.amount.desc(),
            )
            .label("rank"),
        )
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end)
        .where(Transaction.type == "expense")
        .subquery()
    )
    return list(
        await session.scalars(
            sa.select(Transaction)
            .join(ranked, ranked.c.id == Transaction.id)
            .where(ranked.c.rank <= per_currency)
            .order_by(Transaction.currency, Transaction.amount.desc())
        )
    )


async def day_summary(session: AsyncSession, day: date | None = None) -> DaySummary:
    """Everything that happened on one local day (`/bugun`)."""
    day = day or datetime.now(settings.tz).date()
    start, end = day_bounds(day)
    summary = DaySummary(day=day)

    totals = await session.execute(
        sa.select(
            Transaction.type,
            Transaction.currency,
            sa.func.sum(Transaction.amount),
        )
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end)
        .group_by(Transaction.type, Transaction.currency)
    )
    for txn_type, currency, total in totals.all():
        bucket = summary.income if txn_type.value == "income" else summary.expense
        bucket[currency] = total

    categories = await session.execute(
        sa.select(
            sa.func.coalesce(Transaction.category, "other"),
            Transaction.currency,
            sa.func.sum(Transaction.amount).label("total"),
        )
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end)
        .where(Transaction.type == "expense")
        .group_by(Transaction.category, Transaction.currency)
        .order_by(sa.desc("total"))
        .limit(10)
    )
    summary.by_category = [(c, cur, total) for c, cur, total in categories.all()]

    people = await session.execute(
        sa.select(Person, sa.func.count(Interaction.id).label("n"))
        .join(Interaction, Interaction.person_id == Person.id)
        .where(Interaction.occurred_at >= start, Interaction.occurred_at < end)
        .group_by(Person.id)
        .order_by(sa.desc("n"))
    )
    summary.people_seen = [(row[0], row[1]) for row in people.all()]

    summary.new_debts = list(
        await session.scalars(
            sa.select(Debt).where(Debt.created_at >= start, Debt.created_at < end)
        )
    )
    summary.new_promises = list(
        await session.scalars(
            sa.select(Promise).where(
                Promise.created_at >= start, Promise.created_at < end
            )
        )
    )
    summary.interactions = await session.scalar(
        sa.select(sa.func.count(Interaction.id)).where(
            Interaction.occurred_at >= start, Interaction.occurred_at < end
        )
    )
    summary.biggest = await _top_expenses(session, start, end)
    return summary


async def person_summary(
    session: AsyncSession, person: Person, *, recent: int = 5
) -> PersonSummary:
    """Debts, promises and recent contact for one person (`/kim`)."""
    summary = PersonSummary(person=person)
    summary.balances = await open_debts(session, person_id=person.id)
    summary.open_promises = [
        p for p, _ in await open_promises(session, person_id=person.id)
    ]
    summary.last_interactions = list(
        await session.scalars(
            sa.select(Interaction)
            .where(Interaction.person_id == person.id)
            .order_by(Interaction.occurred_at.desc())
            .limit(recent)
        )
    )
    summary.total_interactions = await session.scalar(
        sa.select(sa.func.count(Interaction.id)).where(Interaction.person_id == person.id)
    )
    return summary


async def due_items(session: AsyncSession, *, horizon_days: int = 1) -> dict[str, list]:
    """Overdue and soon-due debts, promises and tasks (reminder source)."""
    today = datetime.now(settings.tz).date()
    limit = today + timedelta(days=horizon_days)

    debts = [
        b
        for b in await open_debts(session)
        if b.earliest_due is not None and b.earliest_due <= limit
    ]
    promises = [
        (p, person)
        for p, person in await open_promises(session)
        if p.due_date is not None and p.due_date <= limit
    ]
    tasks = list(
        await session.scalars(
            sa.select(Task)
            .where(Task.status.in_([TaskStatus.todo, TaskStatus.doing]))
            .where(Task.due_date.isnot(None), Task.due_date <= limit)
            .order_by(Task.due_date)
        )
    )
    return {"debts": debts, "promises": promises, "tasks": tasks}


async def upcoming_events(
    session: AsyncSession, *, within_minutes: int = 60
) -> list[Event]:
    now = datetime.now(settings.tz)
    return list(
        await session.scalars(
            sa.select(Event)
            .where(Event.status == "planned")
            .where(Event.start_at >= now)
            .where(Event.start_at <= now + timedelta(minutes=within_minutes))
            .order_by(Event.start_at)
        )
    )


@dataclass(slots=True)
class SpendingSummary:
    date_from: date
    date_to: date  # inclusive
    income: dict[Currency, Decimal] = field(default_factory=dict)
    expense: dict[Currency, Decimal] = field(default_factory=dict)
    by_category: list[tuple[str, Currency, Decimal]] = field(default_factory=list)
    biggest: list[Transaction] = field(default_factory=list)


async def spending_summary(
    session: AsyncSession, date_from: date, date_to: date
) -> SpendingSummary:
    """Income/expense over a local date range, inclusive on both ends."""
    start, _ = day_bounds(date_from)
    _, end = day_bounds(date_to)
    summary = SpendingSummary(date_from=date_from, date_to=date_to)

    totals = await session.execute(
        sa.select(Transaction.type, Transaction.currency, sa.func.sum(Transaction.amount))
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end)
        .group_by(Transaction.type, Transaction.currency)
    )
    for txn_type, currency, total in totals.all():
        bucket = summary.income if txn_type.value == "income" else summary.expense
        bucket[currency] = total

    categories = await session.execute(
        sa.select(
            sa.func.coalesce(Transaction.category, "other"),
            Transaction.currency,
            sa.func.sum(Transaction.amount).label("total"),
        )
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end)
        .where(Transaction.type == "expense")
        .group_by(Transaction.category, Transaction.currency)
        .order_by(sa.desc("total"))
        .limit(10)
    )
    summary.by_category = [(c, cur, total) for c, cur, total in categories.all()]

    summary.biggest = await _top_expenses(session, start, end)
    return summary


async def events_between(
    session: AsyncSession, start: datetime, end: datetime
) -> list[Event]:
    """Planned events in [start, end), soonest first."""
    return list(
        await session.scalars(
            sa.select(Event)
            .where(Event.status == "planned")
            .where(Event.start_at >= start, Event.start_at < end)
            .order_by(Event.start_at)
        )
    )


@dataclass(slots=True)
class CompletedToday:
    settled_debts: list[Debt] = field(default_factory=list)
    done_promises: list[Promise] = field(default_factory=list)
    done_tasks: list[Task] = field(default_factory=list)


async def completed_on(session: AsyncSession, day: date) -> CompletedToday:
    """What got closed out on one local day (for the daily report)."""
    start, end = day_bounds(day)
    return CompletedToday(
        settled_debts=list(
            await session.scalars(
                sa.select(Debt).where(
                    Debt.settled_at.isnot(None),
                    Debt.settled_at >= start,
                    Debt.settled_at < end,
                )
            )
        ),
        done_promises=list(
            await session.scalars(
                sa.select(Promise).where(
                    Promise.status == PromiseStatus.done,
                    Promise.completed_at >= start,
                    Promise.completed_at < end,
                )
            )
        ),
        done_tasks=list(
            await session.scalars(
                sa.select(Task).where(
                    Task.status == TaskStatus.done,
                    Task.completed_at >= start,
                    Task.completed_at < end,
                )
            )
        ),
    )


async def recent_interactions(
    session: AsyncSession,
    *,
    person_id: int | None = None,
    days: int = 7,
    limit: int = 20,
) -> list[Interaction]:
    """Recent interaction summaries, newest first (RAG context)."""
    since = datetime.now(settings.tz) - timedelta(days=days)
    stmt = (
        sa.select(Interaction)
        .where(Interaction.occurred_at >= since)
        .order_by(Interaction.occurred_at.desc())
        .limit(limit)
    )
    if person_id is not None:
        stmt = stmt.where(Interaction.person_id == person_id)
    return list(await session.scalars(stmt))


@dataclass(slots=True)
class UsageRow:
    provider: str
    model: str | None
    operation: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    audio_seconds: Decimal
    cost_usd: Decimal


@dataclass(slots=True)
class UsageSummary:
    date_from: date
    date_to: date  # inclusive
    rows: list[UsageRow] = field(default_factory=list)
    total_usd: Decimal = Decimal("0")
    today_usd: Decimal = Decimal("0")

    @property
    def cached_share(self) -> float:
        """Fraction of input tokens served from the prompt cache (spec §9)."""
        fresh = sum(r.input_tokens for r in self.rows)
        cached = sum(r.cache_read_tokens for r in self.rows)
        total = fresh + cached
        return cached / total if total else 0.0


async def usage_summary(
    session: AsyncSession, date_from: date, date_to: date
) -> UsageSummary:
    """What MIYA itself cost over a date range (`/xarajat`)."""
    start, _ = day_bounds(date_from)
    _, end = day_bounds(date_to)
    summary = UsageSummary(date_from=date_from, date_to=date_to)

    rows = await session.execute(
        sa.select(
            UsageLog.provider,
            UsageLog.model,
            UsageLog.operation,
            sa.func.count(UsageLog.id),
            sa.func.coalesce(sa.func.sum(UsageLog.input_tokens), 0),
            sa.func.coalesce(sa.func.sum(UsageLog.output_tokens), 0),
            sa.func.coalesce(sa.func.sum(UsageLog.cache_read_tokens), 0),
            sa.func.coalesce(sa.func.sum(UsageLog.audio_seconds), 0),
            sa.func.coalesce(sa.func.sum(UsageLog.cost_usd), 0).label("cost"),
        )
        .where(UsageLog.created_at >= start, UsageLog.created_at < end)
        .group_by(UsageLog.provider, UsageLog.model, UsageLog.operation)
        .order_by(sa.desc("cost"))
    )
    for row in rows.all():
        summary.rows.append(
            UsageRow(
                provider=row[0],
                model=row[1],
                operation=row[2],
                calls=row[3],
                input_tokens=row[4],
                output_tokens=row[5],
                cache_read_tokens=row[6],
                audio_seconds=Decimal(row[7]),
                cost_usd=Decimal(row[8]),
            )
        )
    summary.total_usd = sum((r.cost_usd for r in summary.rows), Decimal("0"))

    today_start, today_end = day_bounds(datetime.now(settings.tz).date())
    summary.today_usd = Decimal(
        await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(UsageLog.cost_usd), 0)).where(
                UsageLog.created_at >= today_start, UsageLog.created_at < today_end
            )
        )
        or 0
    )
    return summary


async def flagged_interactions(
    session: AsyncSession, *, limit: int = 10
) -> tuple[list[Interaction], int]:
    """Interactions whose processing failed (`/tekshir`): newest first, plus count."""
    stmt = sa.select(Interaction).where(Interaction.needs_review.is_(True))
    total = await session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
    rows = list(
        await session.scalars(stmt.order_by(Interaction.occurred_at.desc()).limit(limit))
    )
    return rows, total or 0
