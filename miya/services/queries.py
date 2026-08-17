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
    stmt = (
        sa.select(Promise, Person)
        .join(Person, Person.id == Promise.person_id)
        .where(Promise.status == PromiseStatus.open)
        .order_by(Promise.due_date.nulls_last(), Promise.created_at)
        .limit(limit)
    )
    if person_id is not None:
        stmt = stmt.where(Promise.person_id == person_id)
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


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
