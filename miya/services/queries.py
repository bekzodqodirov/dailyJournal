"""Deterministic read queries (spec §8).

**Money and debt answers always come from SQL.** These functions are the only
source of financial numbers in the system; the reasoning model may phrase their
output in Uzbek but never produces the figures itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    Direction,
    InteractionSource,
    PromiseStatus,
    TaskStatus,
)
from miya.db.models import (
    ChatMonitor,
    Debt,
    DebtPayment,
    Event,
    Interaction,
    Memory,
    Person,
    Promise,
    Task,
    Transaction,
    UsageLog,
)
from miya.services import codes, memories


@dataclass(slots=True)
class DebtBalance:
    person: Person
    direction: DebtDirection
    currency: Currency
    outstanding: Decimal
    earliest_due: date | None
    count: int
    # The debt rows behind this balance, so a line can carry their d-refs.
    ids: list[int] = field(default_factory=list)


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
class TimelineEntry:
    """One row of a person's history, as a surface shows it (build step 4)."""

    interaction: Interaction
    when: datetime
    source: InteractionSource
    direction: Direction
    # The summary when extraction wrote one, else the words themselves.
    text: str


@dataclass(slots=True)
class PersonSummary:
    person: Person
    balances: list[DebtBalance] = field(default_factory=list)
    open_promises: list[Promise] = field(default_factory=list)
    last_interactions: list[Interaction] = field(default_factory=list)
    total_interactions: int = 0
    # Per-person memory (build step 4). ``profile`` is MIYA's own prose
    # (people.notes) and never a source of figures; the figures are above.
    last_contact_at: datetime | None = None
    facts: list[Memory] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    profile: str | None = None
    profile_updated_at: datetime | None = None
    # WP-40: the person's client codes and where others mentioned them.
    codes: list[str] = field(default_factory=list)
    code_mentions: list[Any] = field(default_factory=list)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a local Tashkent day, as timezone-aware datetimes."""
    tz = settings.tz
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


# The rows that count as money (WP-11). Every total over transactions MUST
# carry this filter: a voided row (a correction, a reinstall duplicate) and
# an internal transfer between the owner's own accounts are not income or
# expense. A test fails when a sum over Transaction.amount lacks it.
ACTIVE_TXN = sa.and_(Transaction.voided_at.is_(None), Transaction.is_internal.is_(False))


# `debts.amount` minus everything paid against it — the outstanding balance.
# Public: loops.py ranks open loops by this same figure, so there is one
# definition of "what is still owed" in the codebase.
OUTSTANDING = Debt.amount - sa.func.coalesce(
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
            sa.func.sum(OUTSTANDING).label("outstanding"),
            sa.func.min(Debt.due_date).label("earliest_due"),
            sa.func.count(Debt.id).label("count"),
            sa.func.array_agg(sa.distinct(Debt.id)).label("ids"),
        )
        .join(Person, Person.id == Debt.person_id)
        .where(Debt.status != DebtStatus.settled)
        .group_by(Person.id, Debt.direction, Debt.currency)
        .having(sa.func.sum(OUTSTANDING) > 0)
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
            ids=sorted(row[6] or []),
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
        .where(ACTIVE_TXN)
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
        .where(ACTIVE_TXN)
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
        .where(ACTIVE_TXN)
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

    # People are loaded eagerly: `/bugun` lists each new debt and promise by
    # name and ref, and the relationships are lazy="raise".
    summary.new_debts = list(
        await session.scalars(
            sa.select(Debt)
            .options(selectinload(Debt.person))
            .where(Debt.created_at >= start, Debt.created_at < end)
            .order_by(Debt.id)
        )
    )
    summary.new_promises = list(
        await session.scalars(
            sa.select(Promise)
            .options(selectinload(Promise.person))
            .where(Promise.created_at >= start, Promise.created_at < end)
            .order_by(Promise.id)
        )
    )
    summary.interactions = await session.scalar(
        sa.select(sa.func.count(Interaction.id)).where(
            Interaction.occurred_at >= start, Interaction.occurred_at < end
        )
    )
    summary.biggest = await _top_expenses(session, start, end)
    return summary


# --- one person's history (build step 4) ------------------------------------

# Sources whose every row is one contact worth a timeline line: a call, an
# SMS (a money SMS must appear in /tarix), a note the owner typed or spoke
# into the bot, a receipt. The userbot is different — its member messages
# are one-liners, and the row worth showing is the window's own synthetic
# interaction (meta.kind == "window"), which carries the summary of the
# whole conversation.
TIMELINE_SOURCES: tuple[InteractionSource, ...] = (
    InteractionSource.phone_call,
    InteractionSource.phone_sms,
    InteractionSource.assistant_bot,
    InteractionSource.manual,
    InteractionSource.receipt_photo,
)


def _is_window_row():
    # ``.astext`` on the nested key: ``metadata`` holds JSON null for rows
    # written without one, and a missing key must simply not match.
    return sa.and_(
        Interaction.source == InteractionSource.telegram_userbot,
        Interaction.meta["kind"].astext == "window",
    )


def timeline_filter(direction: Direction | None = None):
    """Which interaction rows a timeline shows.

    Without a direction: the window rows and every row of TIMELINE_SOURCES;
    raw userbot member lines stay out. With one: the member lines *are* the
    point — "what did I say to him" is the owner's own DM lines (direction
    out), "what did he say" his — so only userbot rows with that direction
    count. The owner's own notes to the bot are stored as direction ``in``
    too, and a call transcript holds both voices; neither is "his lines".
    The window row itself has direction ``na`` and drops out on its own.
    """
    if direction is not None:
        return sa.and_(
            Interaction.source == InteractionSource.telegram_userbot,
            Interaction.direction == direction,
        )
    return sa.or_(_is_window_row(), Interaction.source.in_(TIMELINE_SOURCES))


def timeline_text(interaction: Interaction, *, limit: int = 300) -> str:
    if interaction.summary:
        return interaction.summary.strip()
    body = interaction.transcript or interaction.raw_text or ""
    return body.strip()[:limit]


def timeline_entry(interaction: Interaction) -> TimelineEntry:
    return TimelineEntry(
        interaction=interaction,
        when=interaction.occurred_at,
        source=interaction.source,
        direction=interaction.direction,
        text=timeline_text(interaction),
    )


async def timeline(
    session: AsyncSession,
    person_id: int,
    *,
    limit: int = 20,
    before: datetime | None = None,
    since: datetime | None = None,
    direction: Direction | None = None,
) -> list[TimelineEntry]:
    """A person's contact history, newest first, in one query.

    ``before`` pages backwards (strictly earlier rows), ``since`` bounds the
    window (rows at or after it); the query walks
    ``ix_interactions_person_occurred``.
    """
    stmt = (
        sa.select(Interaction)
        .where(Interaction.person_id == person_id)
        .where(timeline_filter(direction))
        .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(Interaction.occurred_at < before)
    if since is not None:
        stmt = stmt.where(Interaction.occurred_at >= since)
    return [timeline_entry(row) for row in await session.scalars(stmt)]


def _last_of(column, *conditions) -> sa.ScalarSelect:
    # Correlated on Person so the same expression serves a per-person scan
    # (profiles.stale_people) and a single lookup by id.
    return (
        sa.select(sa.func.max(column))
        .where(*conditions)
        .correlate(Person)
        .scalar_subquery()
    )


def last_contact_expr(person_id):
    """GREATEST of every timestamp that proves contact with ``person_id``.

    The same definition as loops.quiet_counterparties: an interaction either
    way, a transaction with them, a debt or promise recorded about them, or a
    payment on one of their debts — the owner types debts and repayments into
    the bot with no interaction row of their own, and the row's timestamp is
    still proof of contact. GREATEST skips NULLs in PostgreSQL and is NULL
    only when nothing at all is recorded.
    """
    return sa.func.greatest(
        _last_of(Interaction.occurred_at, Interaction.person_id == person_id),
        # A voided row proves nothing; an internal transfer is still contact.
        _last_of(
            Transaction.occurred_at,
            Transaction.counterparty_person_id == person_id,
            Transaction.voided_at.is_(None),
        ),
        _last_of(Debt.created_at, Debt.person_id == person_id),
        _last_of(Promise.created_at, Promise.person_id == person_id),
        _last_of(
            DebtPayment.paid_at,
            DebtPayment.debt_id == Debt.id,
            Debt.person_id == person_id,
        ),
    )


async def last_contact_at(session: AsyncSession, person_id: int) -> datetime | None:
    """When the owner last had anything to do with this person, or None."""
    return await session.scalar(sa.select(last_contact_expr(person_id)))


PERSON_CODE_MENTIONS = 5


async def person_summary(
    session: AsyncSession,
    person: Person,
    *,
    recent: int = 5,
    timeline_limit: int = 10,
    facts_limit: int = 8,
) -> PersonSummary:
    """Everything held about one person (`/kim`, the API, the RAG tool).

    Seven queries, whatever the person's history: balances, open promises,
    the last interactions and their count, last contact, facts, timeline.
    """
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
    summary.last_contact_at = await last_contact_at(session, person.id)
    summary.facts = await memories.facts_for(session, person.id, limit=facts_limit)
    summary.timeline = await timeline(session, person.id, limit=timeline_limit)
    summary.profile = person.notes
    summary.profile_updated_at = person.profile_updated_at
    summary.codes = await codes.codes_of(session, person.id)
    found = []
    for code in summary.codes:
        found += await codes.mentions(
            session, code, limit=PERSON_CODE_MENTIONS, exclude_person_id=person.id
        )
    found.sort(key=lambda line: line.when, reverse=True)
    summary.code_mentions = found[:PERSON_CODE_MENTIONS]
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


async def transactions_on(
    session: AsyncSession, day: date, *, include_voided: bool = False
) -> list[Transaction]:
    """One local day's money rows, oldest first (`/pul`). Voided rows are
    listed only on request — they are shown, never counted."""
    start, end = day_bounds(day)
    stmt = (
        sa.select(Transaction)
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end)
        .options(selectinload(Transaction.counterparty))
        .order_by(Transaction.occurred_at, Transaction.id)
    )
    if not include_voided:
        stmt = stmt.where(Transaction.voided_at.is_(None))
    return list(await session.scalars(stmt))


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
        .where(ACTIVE_TXN)
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
        .where(ACTIVE_TXN)
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
                sa.select(Promise)
                .options(selectinload(Promise.person))
                .where(
                    Promise.status == PromiseStatus.done,
                    Promise.completed_at >= start,
                    Promise.completed_at < end,
                )
                .order_by(Promise.completed_at)
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
    # Anthropic calls with no price (a model missing from the table): their
    # cost is unknown, not zero (WP-25).
    unpriced_calls: int = 0

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
    summary.unpriced_calls = int(
        await session.scalar(
            sa.select(sa.func.count(UsageLog.id))
            .where(UsageLog.created_at >= start, UsageLog.created_at < end)
            .where(UsageLog.provider == "anthropic")
            .where(UsageLog.cost_usd.is_(None))
        )
        or 0
    )

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


# Phone money texts (WP-14): read deterministically, never re-extracted,
# reviewed in /tekshir's own money block.
MONEY_SOURCES: tuple[InteractionSource, ...] = (
    InteractionSource.phone_sms,
    InteractionSource.phone_notification,
)
# Ignored readings that are not worth showing even in /tekshir hammasi.
IGNORED_HIDDEN_REASONS = ("repeat", "not_payment_app")


def _is_money_row():
    return sa.and_(
        Interaction.source.in_(MONEY_SOURCES),
        Interaction.media.has_key("money"),
    )


async def flagged_interactions(
    session: AsyncSession, *, limit: int = 10
) -> tuple[list[Interaction], int]:
    """Interactions whose processing failed (`/tekshir`): newest first, plus
    count. Money texts have their own block (flagged_money), never both."""
    stmt = (
        sa.select(Interaction)
        .where(Interaction.needs_review.is_(True))
        .where(sa.not_(_is_money_row()))
    )
    total = await session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
    rows = list(
        await session.scalars(stmt.order_by(Interaction.occurred_at.desc()).limit(limit))
    )
    return rows, total or 0


async def flagged_money(
    session: AsyncSession, *, limit: int = 15
) -> tuple[list[Interaction], int]:
    """Money texts waiting for the owner's one tap: newest first, plus count."""
    stmt = (
        sa.select(Interaction)
        .where(Interaction.needs_review.is_(True))
        .where(_is_money_row())
    )
    total = await session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
    rows = list(
        await session.scalars(stmt.order_by(Interaction.occurred_at.desc()).limit(limit))
    )
    return rows, total or 0


def _ignored_money():
    money = Interaction.media["money"]
    return sa.and_(
        _is_money_row(),
        money["verdict"].astext == "ignore",
        sa.func.coalesce(money["reason"].astext, "").notin_(IGNORED_HIDDEN_REASONS),
        sa.not_(money.has_key("resolved")),
    )


async def ignored_money(
    session: AsyncSession, since: datetime, *, limit: int = 15
) -> tuple[list[Interaction], int]:
    """Money texts the reader ignored (codes, adverts) since ``since``: never
    out of the owner's reach (`/tekshir hammasi`)."""
    stmt = (
        sa.select(Interaction)
        .where(_ignored_money())
        .where(Interaction.occurred_at >= since)
    )
    total = await session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
    rows = list(
        await session.scalars(stmt.order_by(Interaction.occurred_at.desc()).limit(limit))
    )
    return rows, total or 0


async def ignored_money_count(
    session: AsyncSession, start: datetime, end: datetime
) -> int:
    return int(
        await session.scalar(
            sa.select(sa.func.count(Interaction.id))
            .where(_ignored_money())
            .where(Interaction.occurred_at >= start, Interaction.occurred_at < end)
        )
        or 0
    )


async def money_review_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            sa.select(sa.func.count(Interaction.id))
            .where(Interaction.needs_review.is_(True))
            .where(_is_money_row())
        )
        or 0
    )


async def retryable_interactions(
    session: AsyncSession, *, limit: int = 50
) -> list[Interaction]:
    """Flagged interactions that another extraction attempt could still rescue.

    Only rows that already hold text: a voice note whose transcription failed
    has nothing to re-extract from, and re-running it would spend money to
    fail again in exactly the same way.

    Oldest first, so a backlog is rebuilt in the order the owner lived it.
    """
    stmt = (
        sa.select(Interaction)
        .where(Interaction.needs_review.is_(True))
        # Only rows never applied: re-extracting an applied row doubles its
        # debts (WP-14), and a money text is read by rules, never a model.
        .where(Interaction.processed.is_(False))
        .where(Interaction.source.notin_(MONEY_SOURCES))
        .where(
            sa.or_(
                sa.func.length(sa.func.coalesce(Interaction.raw_text, "")) > 0,
                sa.func.length(sa.func.coalesce(Interaction.transcript, "")) > 0,
            )
        )
        .order_by(Interaction.occurred_at)
        .limit(limit)
    )
    return list(await session.scalars(stmt))


# --- what happened in the groups (spec §7B) ----------------------------------


@dataclass(slots=True)
class ChatDigest:
    """One chat's day: what it was about, and what was aimed at the owner."""

    tg_chat_id: int
    title: str
    messages: int
    summaries: list[str] = field(default_factory=list)
    to_me: list[Interaction] = field(default_factory=list)


def _addressed_to_owner():
    """Rows the userbot marked as aimed at the owner.

    `.astext` on the nested key: `metadata` holds JSON null for rows written
    without one, and a missing key must simply not match.
    """
    return Interaction.meta["to_me"].astext == "true"


async def messages_to_me(
    session: AsyncSession, day: date | None = None, *, limit: int = 30
) -> list[Interaction]:
    """Group messages that mentioned the owner or replied to him, newest last."""
    start, end = day_bounds(day or datetime.now(settings.tz).date())
    return list(
        await session.scalars(
            sa.select(Interaction)
            .where(_addressed_to_owner())
            .where(Interaction.occurred_at >= start)
            .where(Interaction.occurred_at < end)
            .order_by(Interaction.occurred_at)
            .limit(limit)
        )
    )


async def messages_maybe_to_me(
    session: AsyncSession, day: date | None = None, *, limit: int = 30
) -> list[Interaction]:
    """Bare-first-name lines in a group where a namesake speaks (WP-38).

    Shown apart in /menga; never fed to windows, loops or the instant path.
    """
    start, end = day_bounds(day or datetime.now(settings.tz).date())
    return list(
        await session.scalars(
            sa.select(Interaction)
            .where(Interaction.meta["to_me_maybe"].astext == "true")
            .where(Interaction.occurred_at >= start)
            .where(Interaction.occurred_at < end)
            .order_by(Interaction.occurred_at)
            .limit(limit)
        )
    )


async def chat_digests(
    session: AsyncSession, day: date | None = None
) -> list[ChatDigest]:
    """Per-chat digest of one day, busiest first.

    Two different rows feed this. The window interactions carry the summaries
    the extractor wrote — one per closed conversation — while the member
    messages are what gets counted. Counting the windows instead would report
    "3 messages" for a chat that saw ninety.
    """
    start, end = day_bounds(day or datetime.now(settings.tz).date())
    in_day = (Interaction.occurred_at >= start, Interaction.occurred_at < end)

    titles = dict(
        (
            await session.execute(sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title))
        ).all()
    )

    counts = (
        await session.execute(
            sa.select(Interaction.tg_chat_id, sa.func.count(Interaction.id))
            .where(Interaction.tg_chat_id.isnot(None))
            .where(Interaction.window_id.is_(None))  # members, not the window row
            .where(*in_day)
            .group_by(Interaction.tg_chat_id)
        )
    ).all()

    summaries: dict[int, list[str]] = {}
    rows = await session.execute(
        sa.select(Interaction.tg_chat_id, Interaction.summary)
        .where(Interaction.window_id.isnot(None))
        .where(Interaction.summary.isnot(None))
        .where(*in_day)
        .order_by(Interaction.occurred_at)
    )
    for chat_id, summary in rows:
        if chat_id is not None and summary:
            summaries.setdefault(chat_id, []).append(summary)

    addressed: dict[int, list[Interaction]] = {}
    for interaction in await session.scalars(
        sa.select(Interaction).where(_addressed_to_owner()).where(*in_day)
    ):
        addressed.setdefault(interaction.tg_chat_id or 0, []).append(interaction)

    digests = [
        ChatDigest(
            tg_chat_id=chat_id,
            title=titles.get(chat_id) or str(chat_id),
            messages=count,
            summaries=summaries.get(chat_id, []),
            to_me=addressed.get(chat_id, []),
        )
        for chat_id, count in counts
    ]
    digests.sort(key=lambda d: d.messages, reverse=True)
    return digests
