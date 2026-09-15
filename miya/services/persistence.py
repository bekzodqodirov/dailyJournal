"""Turn an ExtractionResult into database rows (spec §5, steps 1–4)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from rapidfuzz import fuzz
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    EventSource,
    PromiseMadeBy,
    PromiseStatus,
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
from miya.services import records
from miya.services.extraction import ExtractionResult, to_money
from miya.services.people import normalise, resolve_person

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
    # Promises closed because the message said the thing happened, and the
    # hints that matched nothing clearly enough to close on their own.
    fulfilled: list[tuple[Promise, Person]] = field(default_factory=list)
    unmatched_fulfilments: list[tuple[str, str]] = field(default_factory=list)
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
                self.fulfilled,
                self.unmatched_fulfilments,
            )
        )


# A fulfilment closes a promise only when one open promise of that person,
# made by the same side, clearly reads like it. "Unambiguous" means all of:
#
#   * the hint has at least MIN_HINT_TOKENS distinct words — a one-word hint
#     ("invoice", or "invoice invoice") is a topic, not a description, and
#     would score 100 against any promise that mentions the word;
#   * the best score is at least FULFIL_MATCH;
#   * the runner-up is at least FULFIL_MARGIN below it.
#
# Anything less and the owner is shown the hint and closes the right one
# himself — a wrongly closed promise is a silently broken one. The score is
# token_set_ratio alone: partial_ratio let a short hint score 100 against
# any longer promise containing it, which is exactly the over-match this
# guards against.
FULFIL_MATCH = 70
FULFIL_MARGIN = 15
MIN_HINT_TOKENS = 2


def _fulfilment_score(description: str, promise_text: str) -> float:
    a, b = normalise(description), normalise(promise_text)
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


async def _apply_fulfilment(
    session: AsyncSession,
    person: Person,
    description: str,
    made_by: PromiseMadeBy,
    applied: Applied,
    *,
    now: datetime,
) -> None:
    # Distinct tokens: "invoice invoice" is still the one-word topic "invoice".
    if len(set(normalise(description).split())) < MIN_HINT_TOKENS:
        applied.unmatched_fulfilments.append((person.display_name, description))
        return
    # Only promises from the same side: "Akmal invoice yubordi" ends what
    # Akmal promised, never what the owner promised Akmal.
    open_promises = list(
        await session.scalars(
            sa.select(Promise)
            .where(Promise.person_id == person.id)
            .where(Promise.made_by == made_by)
            .where(Promise.status == PromiseStatus.open)
            .order_by(Promise.created_at)
        )
    )
    scored = sorted(
        ((_fulfilment_score(description, p.description), p) for p in open_promises),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored or scored[0][0] < FULFIL_MATCH:
        applied.unmatched_fulfilments.append((person.display_name, description))
        return
    if len(scored) > 1 and scored[0][0] - scored[1][0] < FULFIL_MARGIN:
        log.info(
            "fulfilment %r for %s is ambiguous between promises %s and %s",
            description,
            person.display_name,
            scored[0][1].id,
            scored[1][1].id,
        )
        applied.unmatched_fulfilments.append((person.display_name, description))
        return

    promise = scored[0][1]
    promise.person = person
    await records.mark_done(session, promise, by=records.BY_EXTRACTION, now=now)
    applied.fulfilled.append((promise, person))


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
            # Already covered by earlier payments: settled, and dated — a
            # settled row without settled_at never reaches "Bajarilganlar".
            debt.status = DebtStatus.settled
            debt.settled_at = debt.settled_at or datetime.now(settings.tz)
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

    # Before the new promises land, so a message that both closes one promise
    # and makes the next cannot close the one it just made.
    for item in result.fulfilments:
        if not item.description.strip():
            continue
        # Never creates a person: a fulfilment names someone who already has
        # a promise on the books, or it matches nothing either way.
        person = await resolve_person(session, item.person, create=False)
        if person is None:
            applied.unmatched_fulfilments.append((item.person, item.description))
            continue
        await _apply_fulfilment(
            session,
            person,
            item.description.strip(),
            PromiseMadeBy(item.made_by),
            applied,
            now=datetime.now(tz),
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

    # Spec §5: facts *and* the summary go to memories. Without this, a
    # conversation that produced no discrete facts is unreachable by /qidir
    # once it ages out of the recent-interactions window.
    summary_text = (result.summary or "").strip()
    if summary_text and summary_text not in {f.strip() for f in result.facts}:
        session.add(
            Memory(
                content=summary_text,
                embedding=None,
                occurred_at=occurred,
                tags=result.tags or [],
                source_interaction_id=interaction.id,
            )
        )

    interaction.processed = True
    await session.flush()
    return applied
