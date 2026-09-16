"""Build step 3: what a counterparty asserts is asked about, never written.

Covers the gate (which extracted items become claims), the claim's life
(created by the extraction, accepted, declined or edited by the owner), and
the promise that accepting writes *exactly* the row the extraction would have
written had the owner said it himself.
"""

from __future__ import annotations

import inspect as py_inspect
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
)
from miya.db.session import engine
from miya.services import claims, records
from miya.services import extraction as ex
from miya.services.persistence import Applied, apply_extraction
from tests import conftest

TZ = settings.tz
NOW = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)


async def _interaction(session, text="test", person_id=None) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(TZ),
        raw_text=text,
        person_id=person_id,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _count(session, model) -> int:
    return await session.scalar(sa.select(sa.func.count()).select_from(model))


async def _person(session, name="Akmal") -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


def _debt(**kw) -> ex.ExtractedDebt:
    base = {
        "direction": "i_owe_them",
        "person": "Akmal",
        "amount": 5_000_000,
        "currency": "UZS",
        "reason": "yuk haqi",
        "due_date": "2026-09-20",
        "asserted_by": "them",
    }
    return ex.ExtractedDebt(**{**base, **kw})


def _settlement(**kw) -> ex.ExtractedSettlement:
    base = {
        "person": "Akmal",
        "amount": 2_000_000,
        "currency": "UZS",
        "note": "qaytardi",
        "direction": "they_owe_me",
        "asserted_by": "them",
    }
    return ex.ExtractedSettlement(**{**base, **kw})


def _transaction(**kw) -> ex.ExtractedTransaction:
    base = {
        "type": "expense",
        "amount": 300,
        "currency": "USD",
        "category": "logistics",
        "description": "konteyner",
        "counterparty": "Akmal",
        "asserted_by": "them",
    }
    return ex.ExtractedTransaction(**{**base, **kw})


def _promise(**kw) -> ex.ExtractedPromise:
    base = {
        "made_by": "me",
        "person": "Akmal",
        "description": "hujjat yuborish",
        "due_date": "2026-09-19",
        "asserted_by": "them",
    }
    return ex.ExtractedPromise(**{**base, **kw})


def _fulfilment(**kw) -> ex.ExtractedFulfilment:
    base = {
        "made_by": "them",
        "person": "Akmal",
        "description": "hujjatlarni yubordi",
        "asserted_by": "them",
    }
    return ex.ExtractedFulfilment(**{**base, **kw})


# --- the gate ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "item", "expected"),
    [
        ("debt", _debt(asserted_by="me"), False),
        ("debt", _debt(asserted_by="them"), True),
        ("debt", _debt(direction="they_owe_me", asserted_by="them"), True),
        ("settlement", _settlement(asserted_by="me"), False),
        ("settlement", _settlement(asserted_by="them"), True),
        ("settlement", _settlement(direction=None, asserted_by="them"), True),
        ("transaction", _transaction(asserted_by="me"), False),
        ("transaction", _transaction(asserted_by="them"), True),
        ("transaction", _transaction(counterparty=None, asserted_by="them"), True),
        # A promise: only the owner's commitment on a counterparty's word.
        ("promise", _promise(made_by="me", asserted_by="me"), False),
        ("promise", _promise(made_by="them", asserted_by="me"), False),
        ("promise", _promise(made_by="me", asserted_by="them"), True),
        ("promise", _promise(made_by="them", asserted_by="them"), False),
        # A fulfilment: only "they did their part", said by them.
        ("fulfilment", _fulfilment(made_by="me", asserted_by="me"), False),
        ("fulfilment", _fulfilment(made_by="them", asserted_by="me"), False),
        ("fulfilment", _fulfilment(made_by="me", asserted_by="them"), False),
        ("fulfilment", _fulfilment(made_by="them", asserted_by="them"), True),
    ],
)
def test_the_gate_table(kind, item, expected):
    assert claims.is_claim(kind, item) is expected


def test_the_gate_never_touches_other_kinds():
    assert not claims.is_claim("event", SimpleNamespace(asserted_by="them"))
    assert not claims.is_claim("task", SimpleNamespace(asserted_by="them"))
    # An item without the field is the owner's, as the pydantic default says.
    assert not claims.is_claim("debt", SimpleNamespace())


def test_refs_round_trip():
    assert claims.ref(12) == "c12"
    assert claims.parse_ref("c12") == 12
    assert claims.parse_ref("C12") == 12
    assert claims.parse_ref("#c12") == 12
    assert claims.parse_ref(" c7 ") == 7
    for bad in ("d12", "c", "c-1", "12", "", "cx", "c12x", None):
        assert claims.parse_ref(bad) is None


# --- the extraction parks a claim instead of writing -------------------------


async def test_a_counterparty_debt_becomes_a_claim_and_no_row(session):
    interaction = await _interaction(session)
    result = ex.ExtractionResult(debts=[_debt()])

    applied = await apply_extraction(session, interaction, result)

    assert applied.debts == []
    assert await _count(session, m.Debt) == 0
    assert await _count(session, m.Person) == 0  # nobody is created on a claim
    assert len(applied.claims) == 1
    claim = applied.claims[0]
    assert claim.id is not None
    assert claim.kind == claims.KIND_DEBT
    assert claim.state == claims.PENDING
    assert claim.person_name == "Akmal"
    assert claim.person_id is None
    assert claim.interaction_id == interaction.id
    assert claim.payload == _debt().model_dump(mode="json")
    assert claim.payload["asserted_by"] == "them"
    assert claim.asked_at is None and claim.answered_at is None
    assert claim.history == []
    assert interaction.processed is True


async def test_every_gated_kind_lands_as_a_claim_in_extraction_order(session):
    interaction = await _interaction(session)
    result = ex.ExtractionResult(
        debts=[_debt()],
        debt_settlements=[_settlement()],
        promises=[_promise(), _promise(made_by="them", description="to'laydi")],
        transactions=[_transaction(), _transaction(counterparty=None)],
        fulfilments=[_fulfilment(), _fulfilment(made_by="me", description="pul berdim")],
        events=[ex.ExtractedEvent(title="Uchrashuv", start_at="2026-09-20T15:00")],
        tasks=[ex.ExtractedTask(description="Konteynerni tekshirish")],
        facts=["Akmal Guangzhou'da"],
    )

    applied = await apply_extraction(session, interaction, result)

    kinds = [c.kind for c in applied.claims]
    # Ordering follows the writers: debts, settlements, fulfilments,
    # promises, transactions.
    assert kinds == [
        "debt",
        "settlement",
        "fulfilment",
        "promise",
        "transaction",
        "transaction",
    ]
    assert [c.person_name for c in applied.claims] == ["Akmal"] * 5 + [""]
    # The counterparty's own promise is written; the owner's is asked.
    assert [p.description for p in applied.promises] == ["to'laydi"]
    assert applied.promises[0].made_by is PromiseMadeBy.them
    # A "them"-asserted fulfilment of the *owner's* promise goes the usual way.
    assert applied.unmatched_fulfilments == [("Akmal", "pul berdim")]
    # A transaction with no counterparty still becomes a claim, nameless.
    assert applied.transactions == []
    # The unaffected kinds land as always.
    assert len(applied.events) == 1 and len(applied.tasks) == 1
    assert applied.facts == 1
    assert await _count(session, m.Claim) == 6


async def test_a_nameless_transaction_claim_has_an_empty_person_name(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(transactions=[_transaction(counterparty=None)]),
    )
    assert applied.claims[0].person_name == ""
    assert applied.claims[0].payload["counterparty"] is None


async def test_an_applied_with_only_claims_is_not_empty(session):
    assert Applied().is_empty()
    assert not Applied(claims=[SimpleNamespace()]).is_empty()
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt()])
    )
    assert not applied.is_empty()


async def test_the_owners_own_words_are_written_as_before(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debts=[_debt(asserted_by="me")],
            promises=[_promise(asserted_by="me")],
            transactions=[_transaction(asserted_by="me")],
        ),
    )
    assert applied.claims == []
    assert len(applied.debts) == len(applied.promises) == len(applied.transactions) == 1
    assert applied.debts[0].history == []  # no claim entry on a plain write


async def test_the_windows_person_is_kept_when_the_name_is_theirs(session):
    akmal = await _person(session, "Akmal")
    interaction = await _interaction(session, person_id=akmal.id)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debts=[_debt(person="Akmal aka"), _debt(person="Sardor")],
            transactions=[_transaction(counterparty=None)],
        ),
    )
    by_name = {c.person_name: c.person_id for c in applied.claims}
    assert by_name == {"Akmal aka": akmal.id, "Sardor": None, "": None}


# --- accepting writes the row the extraction would have --------------------------


def _debt_shape(debt: m.Debt) -> tuple:
    return (
        debt.direction,
        debt.person_id,
        debt.amount,
        debt.currency,
        debt.reason,
        debt.due_date,
        debt.status,
    )


async def test_accepting_a_debt_writes_the_same_row_the_owner_would_have(session):
    # The owner's own words, for comparison.
    own = await _interaction(session, "own")
    own_applied = await apply_extraction(
        session, own, ex.ExtractionResult(debts=[_debt(asserted_by="me")])
    )
    # The same words from the counterparty: a claim, then "Ha".
    theirs = await _interaction(session, "theirs")
    applied = await apply_extraction(
        session, theirs, ex.ExtractionResult(debts=[_debt()])
    )
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    assert accepted is not None and accepted.claim is claim
    assert len(accepted.applied.debts) == 1
    debt = accepted.applied.debts[0]
    assert _debt_shape(debt) == _debt_shape(own_applied.debts[0])
    assert debt.source_interaction_id == theirs.id
    assert debt.history == [
        {
            "at": NOW.isoformat(),
            "field": "claim",
            "old": None,
            "new": f"c{claim.id}",
            "by": "button",
            "asserted_by": "them",
        }
    ]
    assert (claim.state, claim.answered_by, claim.answered_at) == (
        claims.ACCEPTED,
        "button",
        NOW,
    )
    assert (claim.result_kind, claim.result_id) == ("debt", debt.id)
    assert claim.person_id == debt.person_id
    assert await _count(session, m.Debt) == 2
    assert await _count(session, m.Person) == 1  # the same Akmal


async def test_accepting_a_promise_writes_the_owners_commitment(session):
    own = await _interaction(session, "own")
    own_applied = await apply_extraction(
        session, own, ex.ExtractionResult(promises=[_promise(asserted_by="me")])
    )
    theirs = await _interaction(session, "theirs")
    applied = await apply_extraction(
        session, theirs, ex.ExtractionResult(promises=[_promise()])
    )
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_COMMAND, now=NOW)

    promise = accepted.applied.promises[0]
    reference = own_applied.promises[0]
    assert (
        promise.made_by,
        promise.person_id,
        promise.description,
        promise.due_date,
    ) == (
        reference.made_by,
        reference.person_id,
        reference.description,
        reference.due_date,
    )
    assert promise.made_by is PromiseMadeBy.me
    assert promise.status is PromiseStatus.open
    assert promise.history[-1]["field"] == "claim"
    assert promise.history[-1]["by"] == "command"
    assert (claim.result_kind, claim.result_id) == ("promise", promise.id)
    assert claim.person_id == promise.person_id


async def test_accepting_a_transaction_writes_the_transaction(session):
    own = await _interaction(session, "own")
    own_applied = await apply_extraction(
        session, own, ex.ExtractionResult(transactions=[_transaction(asserted_by="me")])
    )
    theirs = await _interaction(session, "theirs")
    applied = await apply_extraction(
        session, theirs, ex.ExtractionResult(transactions=[_transaction()])
    )
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    txn = accepted.applied.transactions[0]
    reference = own_applied.transactions[0]
    for name in ("type", "amount", "currency", "category", "description"):
        assert getattr(txn, name) == getattr(reference, name), name
    assert txn.counterparty_person_id == reference.counterparty_person_id
    assert txn.occurred_at == theirs.occurred_at
    assert txn.source_interaction_id == theirs.id
    assert (claim.result_kind, claim.result_id) == ("transaction", txn.id)
    assert claim.person_id == txn.counterparty_person_id
    assert await _count(session, m.Transaction) == 2


async def test_accepting_a_settlement_pays_the_open_debt(session):
    akmal = await _person(session)
    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=akmal.id,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
    )
    session.add(debt)
    await session.flush()
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debt_settlements=[_settlement()])
    )
    assert applied.settlements == [] and await _count(session, m.DebtPayment) == 0
    assert debt.status is DebtStatus.open
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    assert len(accepted.applied.settlements) == 1
    person, payment = accepted.applied.settlements[0]
    assert person.id == akmal.id
    assert payment.amount == Decimal("2000000.00")
    assert payment.note == "qaytardi"
    assert debt.status is DebtStatus.partially_paid
    assert (claim.result_kind, claim.result_id) == ("payment", payment.id)
    assert claim.person_id == akmal.id


async def test_accepting_a_settlement_with_no_debt_stays_a_question(session):
    await _person(session)
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debt_settlements=[_settlement()])
    )
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    assert accepted.applied.settlements == []
    assert accepted.applied.unmatched_settlements == [
        ("Akmal", Decimal("2000000.00"), Currency.UZS)
    ]
    assert not accepted.applied.is_empty()
    # Nothing landed, so the "Ha" is not spent: the claim stays pending for
    # after the debt itself is confirmed.
    assert accepted.written is False
    assert claim.state == claims.PENDING
    assert claim.answered_at is None
    assert (claim.result_kind, claim.result_id) == (None, None)
    assert await _count(session, m.DebtPayment) == 0


async def test_accepting_a_fulfilment_marks_the_promise_done(session):
    akmal = await _person(session)
    promise = m.Promise(
        made_by=PromiseMadeBy.them,
        person_id=akmal.id,
        description="hujjatlarni yuboradi",
    )
    session.add(promise)
    await session.flush()
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(fulfilments=[_fulfilment()])
    )
    assert applied.fulfilled == [] and promise.status is PromiseStatus.open
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    assert [p.id for p, _ in accepted.applied.fulfilled] == [promise.id]
    assert promise.status is PromiseStatus.done
    assert promise.completed_at == NOW
    fields = [entry["field"] for entry in promise.history]
    assert fields == ["status", "claim"]
    assert promise.history[-1]["new"] == f"c{claim.id}"
    assert promise.history[-1]["asserted_by"] == "them"
    assert (claim.result_kind, claim.result_id) == ("fulfilment", promise.id)


async def test_accepting_a_fulfilment_that_matches_nothing_closes_nothing(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(fulfilments=[_fulfilment()])
    )
    claim = applied.claims[0]

    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    assert accepted.applied.unmatched_fulfilments == [("Akmal", "hujjatlarni yubordi")]
    assert (claim.result_kind, claim.result_id) == (None, None)
    assert await _count(session, m.Person) == 0  # a fulfilment never creates one


async def test_accepting_twice_writes_nothing_twice(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt()])
    )
    claim = applied.claims[0]
    await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)

    with pytest.raises(claims.AlreadyAnswered):
        await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)
    with pytest.raises(claims.AlreadyAnswered):
        await claims.decline(session, claim.id, by=claims.BY_BUTTON, now=NOW)
    with pytest.raises(claims.AlreadyAnswered):
        await claims.edit(
            session, claim.id, records.Edit("person", "Sardor"), by=claims.BY_COMMAND
        )
    assert await _count(session, m.Debt) == 1


async def test_declining_writes_nothing(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(debts=[_debt()], transactions=[_transaction()]),
    )
    claim = applied.claims[0]

    declined = await claims.decline(session, claim.id, by=claims.BY_COMMAND, now=NOW)

    assert declined is claim
    assert (claim.state, claim.answered_by, claim.answered_at) == (
        claims.DECLINED,
        "command",
        NOW,
    )
    assert (claim.result_kind, claim.result_id) == (None, None)
    assert await _count(session, m.Debt) == 0
    assert await _count(session, m.Person) == 0
    with pytest.raises(claims.AlreadyAnswered):
        await claims.accept(session, claim.id, by=claims.BY_BUTTON)
    # The other claim of the same interaction is untouched.
    assert [c.id for c in await claims.pending(session)] == [applied.claims[1].id]


async def test_a_missing_claim_is_none_everywhere(session):
    assert await claims.get(session, 424242) is None
    assert await claims.accept(session, 424242, by=claims.BY_BUTTON) is None
    assert await claims.decline(session, 424242, by=claims.BY_BUTTON) is None
    assert (
        await claims.edit(
            session, 424242, records.Edit("person", "X"), by=claims.BY_COMMAND
        )
        is None
    )


async def test_the_person_is_created_on_accept_not_on_create(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt(person="Sardor")])
    )
    assert await _count(session, m.Person) == 0
    assert applied.claims[0].person_id is None

    accepted = await claims.accept(
        session, applied.claims[0].id, by=claims.BY_BUTTON, now=NOW
    )

    person = await session.get(m.Person, accepted.applied.debts[0].person_id)
    assert person.display_name == "Sardor"
    assert applied.claims[0].person_id == person.id


async def test_a_purged_interaction_takes_its_claims_with_it(session):
    interaction = await _interaction(session)
    await apply_extraction(session, interaction, ex.ExtractionResult(debts=[_debt()]))
    await session.flush()
    await session.execute(
        sa.delete(m.Interaction).where(m.Interaction.id == interaction.id)
    )
    assert await _count(session, m.Claim) == 0


# --- editing before the answer --------------------------------------------------


async def _claim_of(session, **result) -> m.Claim:
    interaction = await _interaction(session)
    applied = await apply_extraction(session, interaction, ex.ExtractionResult(**result))
    return applied.claims[0]


async def test_editing_the_amount_rebinds_the_payload_and_writes_history(session):
    claim = await _claim_of(session, debts=[_debt()])
    before = claim.payload

    edited = await claims.edit(
        session,
        claim.id,
        records.parse_edit("6 mln"),
        by=claims.BY_COMMAND,
        now=NOW,
    )

    assert edited is claim
    assert claim.payload is not before  # rebound, never mutated in place
    assert claim.payload["amount"] == 6_000_000
    assert claim.payload["currency"] == "UZS"
    assert claim.state == claims.PENDING
    assert claim.history == [
        {
            "at": NOW.isoformat(),
            "field": "amount",
            "old": "5000000.00",
            "new": "6000000.00",
            "by": "command",
        }
    ]
    # The item rebuilt from the payload carries the correction into accept.
    assert ex.to_money(claims.item_of(claim).amount) == Decimal("6000000.00")
    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON)
    assert accepted.applied.debts[0].amount == Decimal("6000000.00")


async def test_editing_the_amount_with_a_currency_changes_both(session):
    claim = await _claim_of(session, debts=[_debt()])
    await claims.edit(
        session, claim.id, records.parse_edit("$500"), by=claims.BY_COMMAND, now=NOW
    )
    assert (claim.payload["amount"], claim.payload["currency"]) == (500, "USD")
    assert [e["field"] for e in claim.history] == ["amount", "currency"]
    assert claim.history[1] == {
        "at": NOW.isoformat(),
        "field": "currency",
        "old": "UZS",
        "new": "USD",
        "by": "command",
    }


async def test_editing_the_person_renames_and_forgets_the_resolved_id(session):
    akmal = await _person(session)
    interaction = await _interaction(session, person_id=akmal.id)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt()])
    )
    claim = applied.claims[0]
    assert claim.person_id == akmal.id

    await claims.edit(
        session, claim.id, records.parse_edit("Sardor"), by=claims.BY_COMMAND, now=NOW
    )

    assert claim.payload["person"] == "Sardor"
    assert claim.person_name == "Sardor"
    assert claim.person_id is None
    assert claim.history[-1]["old"] == "Akmal" and claim.history[-1]["new"] == "Sardor"
    accepted = await claims.accept(session, claim.id, by=claims.BY_BUTTON)
    sardor = await session.get(m.Person, accepted.applied.debts[0].person_id)
    assert sardor.display_name == "Sardor"


async def test_editing_the_person_of_a_transaction_sets_the_counterparty(session):
    claim = await _claim_of(session, transactions=[_transaction(counterparty=None)])
    await claims.edit(
        session, claim.id, records.parse_edit("kim Akmal"), by=claims.BY_COMMAND
    )
    assert claim.payload["counterparty"] == "Akmal"
    assert "person" not in claim.payload
    assert claim.person_name == "Akmal"


async def test_editing_the_currency(session):
    claim = await _claim_of(session, debt_settlements=[_settlement()])
    await claims.edit(
        session, claim.id, records.parse_edit("usd"), by=claims.BY_BUTTON, now=NOW
    )
    assert claim.payload["currency"] == "USD"
    assert claim.history[-1]["field"] == "currency"
    assert claims.item_of(claim).currency == "USD"


async def test_editing_the_due_date_and_clearing_it(session):
    claim = await _claim_of(session, promises=[_promise()])
    await claims.edit(
        session, claim.id, records.parse_edit("2026-10-01"), by=claims.BY_COMMAND
    )
    assert claim.payload["due_date"] == "2026-10-01"
    assert claims.item_of(claim).due == date(2026, 10, 1)

    await claims.edit(
        session, claim.id, records.parse_edit("muddatsiz"), by=claims.BY_COMMAND
    )
    assert claim.payload["due_date"] is None
    assert claims.item_of(claim).due is None
    assert [e["field"] for e in claim.history] == ["due", "due"]
    assert claim.history[-1]["old"] == "2026-10-01"
    assert claim.history[-1]["new"] is None


async def test_flipping_the_direction(session):
    claim = await _claim_of(session, debts=[_debt(direction="i_owe_them")])
    await claims.edit(
        session, claim.id, records.parse_edit("teskari"), by=claims.BY_BUTTON
    )
    assert claim.payload["direction"] == "they_owe_me"
    await claims.edit(
        session, claim.id, records.parse_edit("teskari"), by=claims.BY_BUTTON
    )
    assert claim.payload["direction"] == "i_owe_them"

    settlement = await _claim_of(session, debt_settlements=[_settlement()])
    await claims.edit(
        session, settlement.id, records.parse_edit("teskari"), by=claims.BY_BUTTON
    )
    assert settlement.payload["direction"] == "i_owe_them"


@pytest.mark.parametrize(
    ("result", "text"),
    [
        # A settlement has no due date.
        ({"debt_settlements": [_settlement()]}, "juma"),
        # A promise has no amount, no currency, no direction.
        ({"promises": [_promise()]}, "6 mln"),
        ({"promises": [_promise()]}, "usd"),
        ({"promises": [_promise()]}, "teskari"),
        # A transaction has no due date and no direction.
        ({"transactions": [_transaction()]}, "ertaga"),
        ({"transactions": [_transaction()]}, "teskari"),
        # A fulfilment is a hint: only the name can be corrected.
        ({"fulfilments": [_fulfilment()]}, "6 mln"),
        ({"fulfilments": [_fulfilment()]}, "juma"),
    ],
)
async def test_editing_a_field_the_kind_has_no_use_for_is_refused(session, result, text):
    claim = await _claim_of(session, **result)
    payload, history = claim.payload, claim.history

    with pytest.raises(ValueError, match=r"^a \w+ claim has no \w+ to correct$"):
        await claims.edit(
            session, claim.id, records.parse_edit(text), by=claims.BY_COMMAND
        )

    assert claim.payload == payload and claim.history == history
    assert claim.state == claims.PENDING


async def test_flipping_a_settlement_without_a_direction_is_refused(session):
    claim = await _claim_of(session, debt_settlements=[_settlement(direction=None)])
    with pytest.raises(ValueError, match="direction"):
        await claims.edit(
            session, claim.id, records.parse_edit("teskari"), by=claims.BY_COMMAND
        )
    assert claim.payload["direction"] is None


# --- reading -----------------------------------------------------------------


async def test_view_reads_every_kind(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debts=[_debt()],
            debt_settlements=[_settlement()],
            promises=[_promise()],
            transactions=[_transaction()],
            fulfilments=[_fulfilment()],
        ),
    )
    views = {c.kind: claims.view(c) for c in applied.claims}

    debt = views["debt"]
    assert (debt.id, debt.state, debt.person_name) == (
        applied.claims[0].id,
        "pending",
        "Akmal",
    )
    assert (debt.amount, debt.currency) == (Decimal("5000000.00"), Currency.UZS)
    assert debt.direction is DebtDirection.i_owe_them
    assert (debt.description, debt.due, debt.made_by) == (
        "yuk haqi",
        date(2026, 9, 20),
        None,
    )

    settlement = views["settlement"]
    assert settlement.amount == Decimal("2000000.00")
    assert settlement.direction is DebtDirection.they_owe_me
    assert settlement.description == "qaytardi"
    assert settlement.due is None

    transaction = views["transaction"]
    assert (transaction.amount, transaction.currency) == (Decimal("300.00"), Currency.USD)
    assert transaction.description == "konteyner"
    assert transaction.direction is None

    promise = views["promise"]
    assert (promise.description, promise.due, promise.made_by) == (
        "hujjat yuborish",
        date(2026, 9, 19),
        PromiseMadeBy.me,
    )
    assert promise.amount is None and promise.currency is None

    fulfilment = views["fulfilment"]
    assert fulfilment.description == "hujjatlarni yubordi"
    assert fulfilment.made_by is PromiseMadeBy.them
    assert fulfilment.amount is None and fulfilment.due is None


def test_view_never_raises_on_a_broken_payload():
    broken = m.Claim(
        id=9,
        kind="debt",
        person_name="",
        payload={
            "amount": "lots",
            "currency": "EUR",
            "direction": "sideways",
            "due_date": 42,
            "reason": None,
            "person": "Akmal",
        },
        state="pending",
    )
    view = claims.view(broken)
    assert (view.id, view.kind, view.person_name) == (9, "debt", "Akmal")
    assert view.amount is None and view.currency is None and view.direction is None
    assert view.due is None and view.description == "" and view.made_by is None

    empty = m.Claim(id=10, kind="promise", person_name="X", payload={}, state="pending")
    view = claims.view(empty)
    assert (view.person_name, view.description, view.due, view.made_by) == (
        "X",
        "",
        None,
        None,
    )

    null = m.Claim(id=11, kind="transaction", person_name="", payload=None, state=None)
    view = claims.view(null)
    assert view.state == "pending" and view.amount is None
    # A nonsensical amount (NaN, a bool) is no amount.
    nan = m.Claim(id=12, kind="debt", person_name="", payload={"amount": "nan"})
    assert claims.view(nan).amount is None
    flag = m.Claim(id=13, kind="debt", person_name="", payload={"amount": True})
    assert claims.view(flag).amount is None


async def test_item_of_rebuilds_the_extracted_item(session):
    claim = await _claim_of(session, promises=[_promise()])
    item = claims.item_of(claim)
    assert isinstance(item, ex.ExtractedPromise)
    assert item == _promise()


# --- finding ---------------------------------------------------------------------


async def test_pending_lists_oldest_first_and_only_pending(session):
    first = await _interaction(session, "first")
    second = await _interaction(session, "second")
    older = await claims.create(
        session, first, claims.KIND_DEBT, _debt(), now=NOW - timedelta(hours=2)
    )
    newer = await claims.create(
        session, second, claims.KIND_DEBT, _debt(), now=NOW - timedelta(hours=1)
    )
    answered = await claims.create(
        session, second, claims.KIND_TRANSACTION, _transaction(), now=NOW
    )
    await claims.decline(session, answered.id, by=claims.BY_BUTTON)

    assert [c.id for c in await claims.pending(session)] == [older.id, newer.id]
    assert [c.id for c in await claims.pending(session, limit=1)] == [older.id]
    assert [c.id for c in await claims.pending_for(session, second.id)] == [newer.id]
    assert await claims.pending_for(session, 424242) == []


async def test_unasked_waits_for_age_and_skips_what_was_shown(session):
    interaction = await _interaction(session)
    old = await claims.create(
        session, interaction, claims.KIND_DEBT, _debt(), now=NOW - timedelta(minutes=30)
    )
    shown = await claims.create(
        session, interaction, claims.KIND_DEBT, _debt(), now=NOW - timedelta(minutes=30)
    )
    fresh = await claims.create(
        session, interaction, claims.KIND_DEBT, _debt(), now=NOW - timedelta(minutes=2)
    )
    claims.mark_asked(shown, now=NOW - timedelta(minutes=20))
    await session.flush()

    found = await claims.unasked(session, older_than=timedelta(minutes=10), now=NOW)
    assert [c.id for c in found] == [old.id]
    # Once old enough, the fresh one is due; the shown one never comes back.
    later = await claims.unasked(
        session, older_than=timedelta(minutes=10), now=NOW + timedelta(minutes=10)
    )
    assert [c.id for c in later] == [old.id, fresh.id]
    assert (
        await claims.unasked(
            session,
            older_than=timedelta(minutes=10),
            now=NOW + timedelta(hours=1),
            limit=1,
        )
    )[0].id == old.id


def test_mark_asked_keeps_the_first_time():
    claim = m.Claim(kind="debt", person_name="", payload={})
    claims.mark_asked(claim, now=NOW)
    claims.mark_asked(claim, now=NOW + timedelta(hours=1))
    assert claim.asked_at == NOW


async def test_get_with_a_row_lock(session):
    claim = await _claim_of(session, debts=[_debt()])
    assert (await claims.get(session, claim.id, for_update=True)) is claim
    assert (await claims.get(session, claim.id)) is claim


async def test_create_refuses_an_unknown_kind(session):
    interaction = await _interaction(session)
    with pytest.raises(ValueError):
        await claims.create(session, interaction, "event", _debt())


# --- schema and fixtures -----------------------------------------------------------


def test_the_claims_table_is_wiped_before_what_it_references():
    order = [
        line.strip()
        for line in py_inspect.getsource(conftest._truncate).splitlines()
        if line.strip().startswith("m.")
    ]
    assert order[0] == "m.Claim,"
    assert order.index("m.Claim,") < order.index("m.Interaction,")
    assert order.index("m.Claim,") < order.index("m.Person,")


async def test_truncation_clears_claims_between_tests(session):
    await _claim_of(session, debts=[_debt()])
    await session.commit()
    await conftest._truncate(session)
    assert await _count(session, m.Claim) == 0


def test_the_claim_model_matches_the_contract():
    table = m.Claim.__table__
    assert table.name == "claims"
    assert {c.name for c in table.c} >= {
        "id",
        "interaction_id",
        "person_id",
        "person_name",
        "kind",
        "payload",
        "state",
        "asked_at",
        "answered_at",
        "answered_by",
        "result_kind",
        "result_id",
        "history",
        "created_at",
    }
    fks = {fk.column.table.name: fk.ondelete for fk in table.foreign_keys}
    assert fks == {"interactions": "CASCADE", "people": "SET NULL"}
    assert {i.name for i in table.indexes} >= {
        "ix_claims_state_created",
        "ix_claims_interaction",
    }
    assert not table.c.interaction_id.nullable and table.c.person_id.nullable
    assert "Claim" in m.__all__


def _alembic(*args: str) -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        check=True,
        capture_output=True,
        timeout=120,
    )


async def _has_claims_table() -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: sa.inspect(c).has_table("claims"))


async def test_migration_0009_round_trips():
    if not await conftest.database_available():
        pytest.skip("no migrated database reachable at DATABASE_URL")
    assert await _has_claims_table()
    # Nothing may hold a lock on the table while it is dropped.
    await engine.dispose()
    try:
        _alembic("downgrade", "0008_open_loops_surface")
        assert not await _has_claims_table()
    finally:
        await engine.dispose()
        _alembic("upgrade", "head")
    assert await _has_claims_table()
    _alembic("check")
