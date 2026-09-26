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
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
    TaskPriority,
    TransactionType,
)
from miya.db.models import (
    Claim,
    Debt,
    DebtPayment,
    Event,
    Interaction,
    Person,
    Promise,
    Task,
    Transaction,
)
from miya.services import claims, codes, memories, records
from miya.services.extraction import (
    ExtractedDebt,
    ExtractedFulfilment,
    ExtractedPromise,
    ExtractedSettlement,
    ExtractedTransaction,
    ExtractionResult,
    to_money,
)
from miya.services.people import (
    MATCH_THRESHOLD,
    IdentityConflict,
    OwnerNamed,
    UnknownCode,
    best_match,
    normalise,
    resolve_person,
)

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
    # What a counterparty asserted: parked as a question, not written (build
    # step 3, docs/owner-decisions.md "Counterparty claims").
    claims: list[Claim] = field(default_factory=list)
    # WP-31: rows not written because of who they named — a code nobody
    # holds, a code held by someone other than the named person, the owner.
    unknown_codes: list[str] = field(default_factory=list)
    identity_conflicts: list[tuple[str, str, str]] = field(default_factory=list)
    owner_named: list[str] = field(default_factory=list)
    # WP-35: (code, person name) the owner's own note linked outright.
    codes_learned: list[tuple[str, str]] = field(default_factory=list)

    def refusals(self) -> int:
        """How many rows were refused for who they named (WP-31)."""
        return (
            len(self.unknown_codes) + len(self.identity_conflicts) + len(self.owner_named)
        )

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
                self.claims,
                self.unknown_codes,
                self.identity_conflicts,
                self.owner_named,
                self.codes_learned,
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


# --- one writer per kind -----------------------------------------------------
#
# Each writer does for one item exactly what the extraction loop always did:
# validates it, resolves the person, writes the row and reports it in
# ``applied``. ``apply_extraction`` calls them for what the owner asserted;
# ``claims.accept`` calls the same writer once the owner has said "Ha" to
# what a counterparty asserted, so an accepted claim lands as the very row
# the extraction would have written — plus one history entry naming the
# claim, and the claim learns which row it became.


def _claim_note(record: Debt | Promise, claim: Claim, now: datetime) -> None:
    """The history entry an accepted claim leaves on the row it produced."""
    # A fresh list, not .append(): the ORM only sees JSONB reassignment.
    record.history = [
        *(record.history or []),
        {
            "at": now.isoformat(),
            "field": "claim",
            "old": None,
            "new": claims.ref(claim.id),
            "by": claim.answered_by,
            "asserted_by": "them",
        },
    ]


def _claim_result(claim: Claim | None, kind: str, row_id: int | None) -> None:
    if claim is not None:
        claim.result_kind = kind
        claim.result_id = row_id


def _code_policy(interaction: Interaction, item) -> str:
    """The owner's own message may give a person a code outright; anything
    else only suggests it."""
    if (
        getattr(item, "asserted_by", "me") == "me"
        and interaction.source is InteractionSource.assistant_bot
    ):
        return "attach"
    return "suggest"


def _flag_identity(interaction: Interaction, detail: dict) -> None:
    interaction.needs_review = True
    interaction.meta = {**(interaction.meta or {}), "identity": detail}


async def _person_for(
    session: AsyncSession,
    interaction: Interaction,
    item,
    name: str,
    applied: Applied,
    *,
    create: bool = True,
    hint: Person | None = None,
) -> Person | None:
    """resolve_person for a writer: the identity refusals become a review
    flag and a receipt line instead of a row about the wrong person.

    ``hint`` is the person an accepted claim already resolved (WP-32): the
    one the question showed, so the row lands there, not on a namesake.
    """
    if hint is not None:
        return hint
    try:
        return await resolve_person(
            session,
            name,
            create=create,
            code_policy=_code_policy(interaction, item),
            source_interaction_id=interaction.id,
            source="extraction",
            strict=True,
        )
    except UnknownCode as exc:
        _flag_identity(interaction, {"unknown_code": exc.code})
        applied.unknown_codes.append(exc.code)
    except IdentityConflict as exc:
        _flag_identity(
            interaction,
            {"conflict": exc.code, "holder": exc.holder.id, "named": exc.named},
        )
        applied.identity_conflicts.append((exc.code, exc.holder.display_name, exc.named))
    except OwnerNamed as exc:
        _flag_identity(interaction, {"owner_named": exc.name})
        applied.owner_named.append(exc.name)
    return None


async def write_debt(
    session: AsyncSession,
    interaction: Interaction,
    item: ExtractedDebt,
    applied: Applied,
    *,
    now: datetime,
    claim: Claim | None = None,
    person_hint: Person | None = None,
) -> Debt | None:
    amount = to_money(item.amount)
    if amount is None:
        log.warning("dropping debt with non-positive amount: %r", item.amount)
        return None
    person = await _person_for(
        session, interaction, item, item.person, applied, hint=person_hint
    )
    if person is None:
        return None
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
    if claim is not None:
        await session.flush()
        _claim_note(debt, claim, now)
        claim.person_id = person.id
        _claim_result(claim, "debt", debt.id)
    applied.debts.append(debt)
    return debt


async def write_settlement(
    session: AsyncSession,
    interaction: Interaction,
    item: ExtractedSettlement,
    applied: Applied,
    *,
    now: datetime,
    claim: Claim | None = None,
    person_hint: Person | None = None,
) -> None:
    amount = to_money(item.amount)
    if amount is None:
        return
    person = await _person_for(
        session, interaction, item, item.person, applied, hint=person_hint
    )
    if person is None:
        return
    before = len(applied.settlements)
    await _apply_settlement(
        session,
        person,
        amount,
        Currency(item.currency),
        item.note,
        applied,
        DebtDirection(item.direction) if item.direction else None,
    )
    if claim is not None:
        claim.person_id = person.id
        # The first payment this settlement wrote; an unmatched or ambiguous
        # repayment wrote none and stays a question in ``applied``.
        if len(applied.settlements) > before:
            await session.flush()
            _claim_result(claim, "payment", applied.settlements[before][1].id)


async def write_transaction(
    session: AsyncSession,
    interaction: Interaction,
    item: ExtractedTransaction,
    applied: Applied,
    *,
    now: datetime,
    claim: Claim | None = None,
    person_hint: Person | None = None,
) -> Transaction | None:
    amount = to_money(item.amount)
    if amount is None:
        return None
    counterparty = None
    if item.counterparty or person_hint is not None:
        refused = applied.refusals()
        counterparty = await _person_for(
            session, interaction, item, item.counterparty, applied, hint=person_hint
        )
        if applied.refusals() > refused:
            return None
    txn = Transaction(
        type=TransactionType(item.type),
        amount=amount,
        currency=Currency(item.currency),
        category=item.category or "other",
        description=item.description or None,
        counterparty_person_id=counterparty.id if counterparty else None,
        occurred_at=interaction.occurred_at or now,
        source_interaction_id=interaction.id,
    )
    session.add(txn)
    if claim is not None:
        await session.flush()
        claim.person_id = counterparty.id if counterparty else None
        _claim_result(claim, "transaction", txn.id)
    applied.transactions.append(txn)
    return txn


async def write_promise(
    session: AsyncSession,
    interaction: Interaction,
    item: ExtractedPromise,
    applied: Applied,
    *,
    now: datetime,
    claim: Claim | None = None,
    person_hint: Person | None = None,
) -> Promise | None:
    person = await _person_for(
        session, interaction, item, item.person, applied, hint=person_hint
    )
    if person is None or not item.description.strip():
        return None
    promise = Promise(
        made_by=PromiseMadeBy(item.made_by),
        person_id=person.id,
        description=item.description.strip(),
        due_date=item.due,
        source_interaction_id=interaction.id,
    )
    session.add(promise)
    if claim is not None:
        await session.flush()
        _claim_note(promise, claim, now)
        claim.person_id = person.id
        _claim_result(claim, "promise", promise.id)
    applied.promises.append(promise)
    return promise


async def write_fulfilment(
    session: AsyncSession,
    interaction: Interaction,
    item: ExtractedFulfilment,
    applied: Applied,
    *,
    now: datetime,
    claim: Claim | None = None,
    person_hint: Person | None = None,
) -> None:
    if not item.description.strip():
        return
    # Never creates a person: a fulfilment names someone who already has
    # a promise on the books, or it matches nothing either way.
    refused = applied.refusals()
    person = await _person_for(
        session, interaction, item, item.person, applied, create=False, hint=person_hint
    )
    if person is None:
        if applied.refusals() == refused:
            applied.unmatched_fulfilments.append((item.person, item.description))
        return
    before = len(applied.fulfilled)
    await _apply_fulfilment(
        session,
        person,
        item.description.strip(),
        PromiseMadeBy(item.made_by),
        applied,
        now=now,
    )
    if claim is not None:
        claim.person_id = person.id
        if len(applied.fulfilled) > before:
            promise = applied.fulfilled[before][0]
            _claim_note(promise, claim, now)
            _claim_result(claim, "fulfilment", promise.id)


# --- per-person memory (build step 4) ---------------------------------------


async def _link_codes(
    session: AsyncSession,
    interaction: Interaction,
    result: ExtractionResult,
    applied: Applied,
) -> None:
    """people[] items the text tied to a GS code (WP-35).

    The owner's own note gives the code outright; anything else only
    suggests it. Never creates a person; a code held by someone else is a
    conflict for review, never a move. Safe to run twice.
    """
    own_note = (
        interaction.source is InteractionSource.assistant_bot
        and (interaction.meta or {}).get("kind") is None
    )
    policy = "attach" if own_note else "suggest"
    for item in result.people:
        code = codes.canonical_client_code(item.client_code or "")
        if code is None or not item.name.strip():
            continue
        held_before = await codes.holder(session, code)
        try:
            person = await resolve_person(
                session,
                f"{item.name} {code}",
                create=False,
                code_policy=policy,
                source="extraction",
                source_interaction_id=interaction.id,
                strict=True,
            )
        except UnknownCode:
            continue
        except IdentityConflict as exc:
            conflict = (exc.code, exc.holder.display_name, exc.named)
            if conflict not in applied.identity_conflicts:
                _flag_identity(
                    interaction,
                    {"conflict": exc.code, "holder": exc.holder.id, "named": exc.named},
                )
                applied.identity_conflicts.append(conflict)
            continue
        except OwnerNamed:
            continue
        if person is None or held_before is not None:
            continue
        if (await codes.holder(session, code)) is person:
            applied.codes_learned.append((code, person.display_name))


async def _persist_people(
    session: AsyncSession,
    interaction: Interaction,
    result: ExtractionResult,
    applied: Applied,
    *,
    occurred: datetime,
) -> set[int]:
    """Keep what the extractor learned about each person named, as facts.

    Only for people already on the books: a mention is not a contact, and
    the owner decided a slip of the extractor must not create people. The
    ids of the people written are returned for the fact scoping below.
    """
    written: set[int] = set()
    for item in result.people:
        context = (item.context or "").strip()
        if not context:
            continue
        person = await resolve_person(session, item.name, create=False)
        if person is None:
            log.info("skipping context for unknown person %r", item.name)
            continue
        await memories.remember(
            session,
            context,
            person_id=person.id,
            occurred_at=occurred,
            source_interaction_id=interaction.id,
            tags=["person"],
        )
        applied.facts += 1
        written.add(person.id)
    return written


def _code_tags(tags, text: str) -> list[str]:
    """The extractor's tags plus every GS code and waybill the text holds,
    so ix_memories_tags filters on them exactly (WP-39)."""
    out = list(tags or [])
    for code in codes.find_client_codes(text) + codes.find_waybills(text):
        if code not in out:
            out.append(code)
    return out


def _single_person(applied: Applied, extra: set[int]) -> int | None:
    """The one person this extraction wrote about, or None when it is unclear."""
    ids: set[int] = set(extra)
    ids.update(d.person_id for d in applied.debts)
    ids.update(person.id for person, _ in applied.settlements)
    ids.update(p.person_id for p in applied.promises)
    ids.update(
        t.counterparty_person_id
        for t in applied.transactions
        if t.counterparty_person_id is not None
    )
    ids.update(person.id for _, person in applied.fulfilled)
    return next(iter(ids)) if len(ids) == 1 else None


# --- the gate ----------------------------------------------------------------


async def _known_person_id(
    session: AsyncSession, interaction: Interaction, name: str
) -> int | None:
    """The interaction's person, when the extractor's name is clearly them.

    A window from a private chat already knows who the other side is; a
    claim that names that same person can carry the id from the start, and
    the surface can show it as such. Any other name waits for the accept.
    """
    if not interaction.person_id or not name.strip():
        return None
    person = await session.get(Person, interaction.person_id)
    if person is None:
        return None
    _, score = best_match(name, [person])
    return person.id if score >= MATCH_THRESHOLD else None


async def _gate(
    session: AsyncSession,
    interaction: Interaction,
    kind: str,
    item,
    applied: Applied,
    *,
    now: datetime,
) -> bool:
    """Park a counterparty's assertion as a claim. True when it was one."""
    if not claims.is_claim(kind, item):
        return False
    name = getattr(item, "person", None) or getattr(item, "counterparty", None) or ""
    person_id = await _known_person_id(session, interaction, name)
    claim = await claims.create(
        session, interaction, kind, item, person_id=person_id, now=now
    )
    applied.claims.append(claim)
    return True


async def apply_extraction(
    session: AsyncSession, interaction: Interaction, result: ExtractionResult
) -> Applied:
    """Persist everything the extraction found, linked to `interaction`.

    What the owner asserted is written. What a counterparty asserted — a
    debt, a repayment, money, a promise of the owner's, the counterparty's
    own fulfilment — is parked as a claim and asked about (see
    ``claims.is_claim``): opening a promise on their word is safe, closing
    one or moving money is not.
    """
    applied = Applied()
    tz = settings.tz
    occurred = interaction.occurred_at or datetime.now(tz)

    if result.summary:
        interaction.summary = result.summary

    # Codes first, so a debt naming "GS367" finds the Akmal the same note
    # tied it to; again after the writers, for the people they created.
    await _link_codes(session, interaction, result, applied)

    for item in result.debts:
        now = datetime.now(tz)
        if not await _gate(
            session, interaction, claims.KIND_DEBT, item, applied, now=now
        ):
            await write_debt(session, interaction, item, applied, now=now)

    await session.flush()

    for item in result.debt_settlements:
        now = datetime.now(tz)
        if not await _gate(
            session, interaction, claims.KIND_SETTLEMENT, item, applied, now=now
        ):
            await write_settlement(session, interaction, item, applied, now=now)

    # Before the new promises land, so a message that both closes one promise
    # and makes the next cannot close the one it just made.
    for item in result.fulfilments:
        now = datetime.now(tz)
        if not await _gate(
            session, interaction, claims.KIND_FULFILMENT, item, applied, now=now
        ):
            await write_fulfilment(session, interaction, item, applied, now=now)

    for item in result.promises:
        now = datetime.now(tz)
        if not await _gate(
            session, interaction, claims.KIND_PROMISE, item, applied, now=now
        ):
            await write_promise(session, interaction, item, applied, now=now)

    for item in result.transactions:
        now = datetime.now(tz)
        if not await _gate(
            session, interaction, claims.KIND_TRANSACTION, item, applied, now=now
        ):
            await write_transaction(session, interaction, item, applied, now=now)

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

    await _link_codes(session, interaction, result, applied)
    # After the money and promise rows, so the people they name exist.
    people_written = await _persist_people(
        session, interaction, result, applied, occurred=occurred
    )
    # A fact belongs to the person on the interaction (a private chat, a
    # call), else to the one person this extraction wrote about; a fact from
    # a conversation involving two people is nobody's in particular and stays
    # reachable by similarity alone.
    fact_person_id = interaction.person_id or _single_person(applied, people_written)

    # Facts land without an embedding; Phase 3 backfills them with bge-m3.
    for fact in result.facts:
        text = fact.strip()
        if not text:
            continue
        await memories.remember(
            session,
            text,
            person_id=fact_person_id,
            occurred_at=occurred,
            source_interaction_id=interaction.id,
            tags=_code_tags(result.tags, text),
        )
        applied.facts += 1

    # Spec §5: facts *and* the summary go to memories. Without this, a
    # conversation that produced no discrete facts is unreachable by /qidir
    # once it ages out of the recent-interactions window.
    summary_text = (result.summary or "").strip()
    if summary_text and summary_text not in {f.strip() for f in result.facts}:
        await memories.remember(
            session,
            summary_text,
            person_id=fact_person_id,
            occurred_at=occurred,
            source_interaction_id=interaction.id,
            tags=_code_tags(result.tags, summary_text),
        )

    interaction.processed = True
    await session.flush()
    return applied
