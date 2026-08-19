"""Person resolution, persistence, queries and reminders against a real database."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

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
)
from miya.services import extraction as ex
from miya.services import queries, reminders
from miya.services.people import best_match, normalise, resolve_person
from miya.services.persistence import apply_extraction

TZ = settings.tz


async def _interaction(session, text="test") -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(TZ),
        raw_text=text,
    )
    session.add(interaction)
    await session.flush()
    return interaction


# --- person resolution ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Akmal aka", "akmal"),
        ("AKMAL", "akmal"),
        ("Akmal, GZ", "akmal gz"),
        ("Dilnoza opa", "dilnoza"),
        ("To'lqin aka", "to'lqin"),
    ],
)
def test_normalise_strips_honorifics_and_case(raw, expected):
    assert normalise(raw) == expected


def test_best_match_ignores_word_order():
    people = [m.Person(display_name="Akmal GZ", aliases=[])]
    person, score = best_match("GZ Akmal", people)
    assert person is people[0] and score >= 85


def test_best_match_keeps_different_people_apart():
    people = [
        m.Person(display_name="Akmal", aliases=[]),
        m.Person(display_name="Dilnoza", aliases=[]),
    ]
    person, score = best_match("Dilnoza opa", people)
    assert person.display_name == "Dilnoza"


async def test_resolve_person_creates_then_reuses(session):
    first = await resolve_person(session, "Akmal aka")
    await session.flush()
    second = await resolve_person(session, "Akmal")

    assert second.id == first.id
    total = await session.scalar(sa.select(sa.func.count()).select_from(m.Person))
    assert total == 1


async def test_a_new_spelling_is_learned_as_an_alias(session):
    person = await resolve_person(session, "Akmal aka")
    await session.flush()
    await resolve_person(session, "Akmal GZ")

    assert "Akmal GZ" in person.aliases


async def test_a_known_telegram_id_short_circuits_the_fuzzy_match(session):
    person = m.Person(display_name="Akmal", aliases=[], telegram_id=555)
    session.add(person)
    await session.flush()

    # A completely different name still resolves via the id.
    resolved = await resolve_person(session, "Dilnoza", telegram_id=555)
    assert resolved.id == person.id


async def test_distinct_names_create_distinct_people(session):
    await resolve_person(session, "Akmal")
    await session.flush()
    await resolve_person(session, "Dilnoza")
    await session.flush()

    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Person)) == 2


# --- persistence ------------------------------------------------------------


async def test_apply_extraction_writes_every_kind_of_row(session):
    interaction = await _interaction(session)
    result = ex.ExtractionResult(
        summary="Akmalga 5 mln berildi",
        debts=[
            ex.ExtractedDebt(
                direction="they_owe_me",
                person="Akmal aka",
                amount=5_000_000,
                currency="UZS",
                reason="qarz",
                due_date="2026-08-25",
            )
        ],
        promises=[
            ex.ExtractedPromise(
                made_by="them", person="Akmal aka", description="25-avgustda qaytaradi"
            )
        ],
        transactions=[
            ex.ExtractedTransaction(
                type="expense", amount=45_000, category="food", description="tushlik"
            )
        ],
        events=[ex.ExtractedEvent(title="Uchrashuv", start_at="2026-08-20T15:00")],
        tasks=[ex.ExtractedTask(description="Konteynerni tekshirish", priority="high")],
        facts=["Akmal aka Guangzhou'da yetkazib beruvchi"],
        tags=["supplier"],
    )

    applied = await apply_extraction(session, interaction, result)

    assert len(applied.debts) == 1
    assert applied.debts[0].amount == Decimal("5000000.00")
    assert applied.debts[0].due_date == date(2026, 8, 25)
    assert len(applied.promises) == 1
    assert applied.transactions[0].category == "food"
    assert applied.events[0].start_at.hour == 15
    assert applied.tasks[0].priority.value == "high"
    assert applied.facts == 1
    assert interaction.processed is True
    assert interaction.summary == "Akmalga 5 mln berildi"

    # The same person backs the debt and the promise.
    assert applied.debts[0].person_id == applied.promises[0].person_id


async def test_a_debt_with_a_zero_amount_is_dropped_not_persisted(session):
    interaction = await _interaction(session)
    result = ex.ExtractionResult(
        debts=[ex.ExtractedDebt(direction="they_owe_me", person="X", amount=0)]
    )
    applied = await apply_extraction(session, interaction, result)

    assert applied.debts == []
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 0


async def test_facts_are_stored_without_an_embedding_for_phase_3(session):
    interaction = await _interaction(session)
    await apply_extraction(
        session, interaction, ex.ExtractionResult(facts=["Akmal — yetkazib beruvchi"])
    )

    memory = await session.scalar(sa.select(m.Memory))
    assert memory.content == "Akmal — yetkazib beruvchi"
    assert memory.embedding is None


# --- debt settlement --------------------------------------------------------


async def _open_debt(session, amount="5000000", currency=Currency.UZS, due=None):
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal(amount),
        currency=currency,
        due_date=due,
        source_interaction_id=interaction.id,
    )
    session.add(debt)
    await session.flush()
    return person, debt


async def test_a_partial_repayment_marks_the_debt_partially_paid(session):
    _, debt = await _open_debt(session)
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=2_000_000, currency="UZS")
            ]
        ),
    )

    assert len(applied.settlements) == 1
    assert debt.status is DebtStatus.partially_paid
    assert debt.settled_at is None


async def test_a_full_repayment_settles_the_debt(session):
    _, debt = await _open_debt(session)
    interaction = await _interaction(session)

    await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=5_000_000, currency="UZS")
            ]
        ),
    )

    assert debt.status is DebtStatus.settled
    assert debt.settled_at is not None


async def test_a_repayment_with_no_matching_debt_is_flagged_not_dropped(session):
    await resolve_person(session, "Akmal")
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=1_000_000, currency="UZS")
            ]
        ),
    )

    assert applied.settlements == []
    assert applied.unmatched_settlements == [
        ("Akmal", Decimal("1000000.00"), Currency.UZS)
    ]


async def test_a_repayment_only_touches_debts_in_the_same_currency(session):
    await _open_debt(session, amount="300", currency=Currency.USD)
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=300, currency="UZS")
            ]
        ),
    )

    assert applied.settlements == []
    assert applied.unmatched_settlements


async def test_a_repayment_spreads_across_debts_oldest_due_date_first(session):
    person, first = await _open_debt(session, amount="1000000", due=date(2026, 8, 1))
    second = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("2000000"),
        currency=Currency.UZS,
        due_date=date(2026, 9, 1),
    )
    session.add(second)
    await session.flush()

    interaction = await _interaction(session)
    await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=1_500_000, currency="UZS")
            ]
        ),
    )

    assert first.status is DebtStatus.settled
    assert second.status is DebtStatus.partially_paid


# --- queries ----------------------------------------------------------------


async def test_open_debts_reports_the_balance_net_of_payments(session):
    _, debt = await _open_debt(session)
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=Decimal("2000000"), currency=Currency.UZS)
    )
    await session.flush()

    balances = await queries.open_debts(session)

    assert len(balances) == 1
    assert balances[0].outstanding == Decimal("3000000.00")
    assert balances[0].currency is Currency.UZS


async def test_a_fully_paid_debt_disappears_from_open_debts(session):
    _, debt = await _open_debt(session)
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=Decimal("5000000"), currency=Currency.UZS)
    )
    debt.status = DebtStatus.settled
    await session.flush()

    assert await queries.open_debts(session) == []


async def test_balances_are_separated_by_currency(session):
    person, _ = await _open_debt(session, amount="5000000", currency=Currency.UZS)
    session.add(
        m.Debt(
            direction=DebtDirection.they_owe_me,
            person_id=person.id,
            amount=Decimal("300"),
            currency=Currency.USD,
        )
    )
    await session.flush()

    balances = await queries.open_debts(session)
    assert {b.currency for b in balances} == {Currency.UZS, Currency.USD}


async def test_day_summary_counts_only_the_requested_local_day(session):
    interaction = await _interaction(session)
    today = datetime.now(TZ)
    session.add_all(
        [
            m.Transaction(
                type="expense",
                amount=Decimal("45000"),
                currency=Currency.UZS,
                category="food",
                occurred_at=today,
                source_interaction_id=interaction.id,
            ),
            m.Transaction(
                type="expense",
                amount=Decimal("99000"),
                currency=Currency.UZS,
                category="transport",
                occurred_at=today - timedelta(days=2),
                source_interaction_id=interaction.id,
            ),
        ]
    )
    await session.flush()

    summary = await queries.day_summary(session)

    assert summary.expense[Currency.UZS] == Decimal("45000.00")
    assert [c for c, _, _ in summary.by_category] == ["food"]


async def test_person_summary_gathers_debts_promises_and_contact(session):
    person, _ = await _open_debt(session)
    session.add(
        m.Promise(
            made_by=PromiseMadeBy.them,
            person_id=person.id,
            description="qaytaradi",
        )
    )
    interaction = await _interaction(session)
    interaction.person_id = person.id
    await session.flush()

    summary = await queries.person_summary(session, person)

    assert summary.balances[0].outstanding == Decimal("5000000.00")
    assert len(summary.open_promises) == 1
    assert summary.total_interactions >= 1


# --- reminders --------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "quiet"),
    [
        ("2026-08-17T23:45", True),
        ("2026-08-17T03:00", True),
        ("2026-08-17T07:29", True),
        ("2026-08-17T07:31", False),
        ("2026-08-17T12:00", False),
        ("2026-08-17T23:29", False),
    ],
)
def test_quiet_hours_wrap_around_midnight(moment, quiet):
    assert (
        reminders.in_quiet_hours(datetime.fromisoformat(moment).replace(tzinfo=TZ))
        is quiet
    )


async def test_due_items_include_overdue_and_exclude_far_future(session):
    person, _ = await _open_debt(session, due=date(2026, 1, 1))  # long overdue
    session.add(
        m.Debt(
            direction=DebtDirection.they_owe_me,
            person_id=person.id,
            amount=Decimal("100"),
            currency=Currency.USD,
            due_date=datetime.now(TZ).date() + timedelta(days=90),
        )
    )
    await session.flush()

    due = await queries.due_items(session)
    currencies = {b.currency for b in due["debts"]}

    assert Currency.UZS in currencies
    assert Currency.USD not in currencies


async def test_a_reminder_is_not_repeated_within_24_hours(session):
    await _open_debt(session, due=date(2026, 1, 1))

    first = await reminders.collect_due(session)
    assert first.debts
    await reminders.mark_sent(session, first)
    await session.flush()

    second = await reminders.collect_due(session)
    assert second.is_empty()


async def test_a_reminder_returns_after_the_dedupe_window(session):
    person, _ = await _open_debt(session, due=date(2026, 1, 1))
    session.add(
        m.ReminderLog(
            kind="debt",
            ref=f"{person.id}:UZS",
            sent_at=datetime.now(TZ) - timedelta(hours=25),
        )
    )
    await session.flush()

    bundle = await reminders.collect_due(session)
    assert bundle.debts
