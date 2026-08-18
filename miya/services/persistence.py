"""Turn an ExtractionResult into database rows (spec §5, steps 1–4)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    EventSource,
    PromiseMadeBy,
    TaskPriority,
    TransactionType,
)
from miya.db.models import (
    Debt,
    DebtPayment,
    Event,
    Interaction,
    Memory,
    Person,
    Promise,
    Task,
    Transaction,
)
from miya.services.extraction import ExtractionResult, to_money
from miya.services.people import resolve_person

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Applied:
    """What actually landed, so the bot can confirm it in Uzbek."""

    debts: list[Debt] = field(default_factory=list)
    settlements: list[tuple[Person, DebtPayment]] = field(default_factory=list)
    unmatched_settlements: list[tuple[str, Decimal, Currency]] = field(
        default_factory=list
    )
    # Repayments that could have gone either way; the owner has to say which.
    ambiguous_settlements: list[tuple[str, Decimal, Currency]] = field(
        default_factory=list
    )
    promises: list[Promise] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    facts: int = 0

    def is_empty(self) -> bool:
        return not any(
            (
                self.debts,
                self.settlements,
                self.unmatched_settlements,
                self.ambiguous_settlements,
                self.promises,
                self.transactions,
                self.events,
                self.tasks,
            )
        )


async def _apply_settlement(
    session: AsyncSession,
    person: Person,
    amount: Decimal,
    currency: Currency,
    note: str,
    applied: Applied,
    direction: DebtDirection | None = None,
) -> None:
    """Pay down this person's open debts in that currency, oldest due date first.

    A repayment larger than the outstanding balance is recorded in full against
    the last debt rather than dropped — the owner said it happened.

    Direction matters: the owner routinely has open debts in *both* directions
    with the same supplier, and paying the wrong side inverts his books. When
    the extraction did not say which side was repaid and both sides are open,
    nothing is paid and the owner is asked — a wrong number is worse than a
    missing one.
    """
    # The session runs with autoflush=False, so without this flush a second
    # settlement in the same extraction would not see the first one's payment
    # rows or status changes and would pay the same debt again.
    await session.flush()
    debts = list(
        await session.scalars(
            sa.select(Debt)
            .where(Debt.person_id == person.id)
            .where(Debt.currency == currency)
            .where(Debt.status != DebtStatus.settled)
            .order_by(Debt.due_date.nulls_last(), Debt.created_at)
        )
    )
    if direction is not None:
        debts = [d for d in debts if d.direction is direction]
    elif len({d.direction for d in debts}) > 1:
        applied.ambiguous_settlements.append((person.display_name, amount, currency))
        log.warning(
            "settlement of %s %s for %s is ambiguous: open debts run both ways",
            amount,
            currency.value,
            person.display_name,
        )
        return

    if not debts:
        applied.unmatched_settlements.append((person.display_name, amount, currency))
        return

    remaining = amount
    for debt in debts:
        if remaining <= 0:
            break
        paid = await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(DebtPayment.amount), 0)).where(
                DebtPayment.debt_id == debt.id
            )
        )
        outstanding = debt.amount - Decimal(paid or 0)
        if outstanding <= 0:
            debt.status = DebtStatus.settled
            continue

        is_last = debt is debts[-1]
        chunk = remaining if (remaining <= outstanding or is_last) else outstanding
        payment = DebtPayment(
            debt_id=debt.id, amount=chunk, currency=currency, note=note or None
        )
        session.add(payment)
        applied.settlements.append((person, payment))
        remaining -= chunk

        if chunk >= outstanding:
            debt.status = DebtStatus.settled
            debt.settled_at = datetime.now(settings.tz)
        else:
            debt.status = DebtStatus.partially_paid

    if remaining > 0:
        applied.unmatched_settlements.append((person.display_name, remaining, currency))


async def apply_extraction(
    session: AsyncSession, interaction: Interaction, result: ExtractionResult
) -> Applied:
    """Persist everything the extraction found, linked to `interaction`."""
    applied = Applied()
    tz = settings.tz
    occurred = interaction.occurred_at or datetime.now(tz)

    if result.summary:
        interaction.summary = result.summary

    for item in result.debts:
        amount = to_money(item.amount)
        if amount is None:
            log.warning("dropping debt with non-positive amount: %r", item.amount)
            continue
        person = await resolve_person(session, item.person)
        if person is None:
            continue
        debt = Debt(
            direction=DebtDirection(item.direction),
            person_id=person.id,
            amount=amount,
            currency=Currency(item.currency),
            reason=item.reason or None,
            due_date=item.due,
            source_interaction_id=interaction.id,
        )
        session.add(debt)
        applied.debts.append(debt)

    await session.flush()

    for item in result.debt_settlements:
        amount = to_money(item.amount)
        if amount is None:
            continue
        person = await resolve_person(session, item.person)
        if person is None:
            continue
        await _apply_settlement(
            session,
            person,
            amount,
            Currency(item.currency),
            item.note,
            applied,
            DebtDirection(item.direction) if item.direction else None,
        )

    for item in result.promises:
        person = await resolve_person(session, item.person)
        if person is None or not item.description.strip():
            continue
        promise = Promise(
            made_by=PromiseMadeBy(item.made_by),
            person_id=person.id,
            description=item.description.strip(),
            due_date=item.due,
            source_interaction_id=interaction.id,
        )
        session.add(promise)
        applied.promises.append(promise)

    for item in result.transactions:
        amount = to_money(item.amount)
        if amount is None:
            continue
        counterparty = (
            await resolve_person(session, item.counterparty)
            if item.counterparty
            else None
        )
        txn = Transaction(
            type=TransactionType(item.type),
            amount=amount,
            currency=Currency(item.currency),
            category=item.category or "other",
            description=item.description or None,
            counterparty_person_id=counterparty.id if counterparty else None,
            occurred_at=occurred,
            source_interaction_id=interaction.id,
        )
        session.add(txn)
        applied.transactions.append(txn)

    for item in result.events:
        start = item.start(tz)
        if start is None or not item.title.strip():
            continue
        event = Event(
            title=item.title.strip(),
            start_at=start,
            location=item.location or None,
            attendees=item.attendees or None,
            source=EventSource.extracted,
            source_interaction_id=interaction.id,
        )
        session.add(event)
        applied.events.append(event)

    for item in result.tasks:
        if not item.description.strip():
            continue
        task = Task(
            description=item.description.strip(),
            due_date=item.due,
            priority=TaskPriority(item.priority),
            source_interaction_id=interaction.id,
        )
        session.add(task)
        applied.tasks.append(task)

    # Facts land without an embedding; Phase 3 backfills them with bge-m3.
    for fact in result.facts:
        text = fact.strip()
        if not text:
            continue
        session.add(
            Memory(
                content=text,
                embedding=None,
                occurred_at=occurred,
                tags=result.tags or [],
                source_interaction_id=interaction.id,
            )
        )
        applied.facts += 1

    interaction.processed = True
    await session.flush()
    return applied
