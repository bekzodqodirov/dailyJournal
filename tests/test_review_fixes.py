"""Regression tests for the adversarial-review findings.

Each test here reproduces a confirmed production bug from the Phase 1+2 review
and pins the fix.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from miya.bot import replies
from miya.bot.formatting import TELEGRAM_LIMIT, clip
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection, DebtStatus
from miya.db.session import SessionLocal
from miya.services import extraction as ex
from miya.services import reminders
from miya.services.people import resolve_person
from miya.services.persistence import apply_extraction
from miya.services.transcription import ElevenLabsScribe, TranscriptionError
from tests.test_pipeline import _interaction, _open_debt

TZ = settings.tz


# --- settlement double-pay (autoflush=False made payments invisible) ---------


async def test_two_settlements_in_one_extraction_do_not_double_pay(session):
    """Repro from the review: debts A=40k, B=100k; settlements 70k then 50k.

    Before the fix, debt A collected 80k in payments against a 40k principal
    and B's balance came out wrong. Payments must land oldest-due-first with
    each settlement seeing the previous one's effect.
    """
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    debt_a = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("40000"),
        currency=Currency.UZS,
        due_date=date(2026, 8, 1),
        source_interaction_id=interaction.id,
    )
    debt_b = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("100000"),
        currency=Currency.UZS,
        due_date=date(2026, 9, 1),
        source_interaction_id=interaction.id,
    )
    session.add_all([debt_a, debt_b])
    await session.flush()

    await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=70_000, currency="UZS"),
                ex.ExtractedSettlement(person="Akmal", amount=50_000, currency="UZS"),
            ]
        ),
    )
    await session.commit()

    paid_a = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(m.DebtPayment.amount), 0)).where(
            m.DebtPayment.debt_id == debt_a.id
        )
    )
    paid_b = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(m.DebtPayment.amount), 0)).where(
            m.DebtPayment.debt_id == debt_b.id
        )
    )
    # 120k total: 40k settles A, the remaining 80k goes to B.
    assert paid_a == Decimal("40000.00")
    assert paid_b == Decimal("80000.00")
    assert debt_a.status is DebtStatus.settled
    assert debt_b.status is DebtStatus.partially_paid


# --- resolve_person race (bot and worker are separate processes) -------------


async def test_concurrent_resolution_of_the_same_name_yields_one_person(session):
    """Two transactions resolving the same new name must not both insert."""

    async def resolve_in_new_session(delay: float) -> int:
        await asyncio.sleep(delay)
        async with SessionLocal() as s:
            person = await resolve_person(s, "Karim")
            await s.commit()
            return person.id

    ids = await asyncio.gather(resolve_in_new_session(0), resolve_in_new_session(0.01))

    assert ids[0] == ids[1]
    total = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.Person)
        .where(m.Person.display_name == "Karim")
    )
    assert total == 1


# --- reminder dedupe key must include direction ------------------------------


async def test_opposite_direction_debts_get_separate_reminders(session):
    person, _ = await _open_debt(session, due=date(2026, 1, 1))
    first = await reminders.collect_due(session)
    assert first.debts
    await reminders.mark_sent(session, first)
    await session.flush()

    # A debt in the opposite direction, same person and currency, now due.
    session.add(
        m.Debt(
            direction=DebtDirection.i_owe_them,
            person_id=person.id,
            amount=Decimal("1000000"),
            currency=Currency.UZS,
            due_date=date(2026, 1, 1),
        )
    )
    await session.flush()

    second = await reminders.collect_due(session)
    directions = {b.direction for b in second.debts}
    assert DebtDirection.i_owe_them in directions


# --- oversized replies are clipped, never rejected by Telegram ---------------


def test_clip_caps_a_message_under_the_telegram_limit():
    body = "\n".join(f"qator {i}: " + "x" * 80 for i in range(200))
    clipped = clip(body)
    assert len(clipped) <= TELEGRAM_LIMIT + 100  # marker fits in the margin
    assert clipped.endswith("<i>(qisqartirildi)</i>")


def test_clip_leaves_short_messages_alone():
    assert clip("salom") == "salom"


def test_a_huge_reminder_body_is_clipped():
    balances = [
        replies.DebtBalance(
            person=m.Person(display_name=f"Odam {i} " + "x" * 50, aliases=[]),
            direction=DebtDirection.they_owe_me,
            currency=Currency.UZS,
            outstanding=Decimal("1000000"),
            earliest_due=date(2026, 1, 1),
            count=1,
        )
        for i in range(200)
    ]
    body = replies.reminder(balances, [], [], [])
    assert len(body) <= TELEGRAM_LIMIT + 100


# --- /kim with HTML in the argument ------------------------------------------


def test_person_not_found_escapes_the_queried_name():
    body = replies.person_not_found("Ali <ukam>")
    assert "<ukam>" not in body
    assert "&lt;ukam&gt;" in body


# --- /tekshir surfaces flagged rows ------------------------------------------


async def test_review_report_lists_flagged_interactions(session):
    interaction = await _interaction(session, text="o'qilmagan xabar")
    interaction.needs_review = True
    await session.flush()

    from miya.services.queries import flagged_interactions

    flagged, total = await flagged_interactions(session)
    body = replies.review_report(flagged, total)

    assert total == 1
    assert "1 ta yozuv qayta ishlanmagan" in body
    assert "o'qilmagan xabar" in body


def test_review_report_when_everything_is_processed():
    assert "yo'q" in replies.review_report([], 0)


# --- Scribe returning a non-JSON 200 must not re-bill every sweep ------------


async def test_scribe_garbage_body_raises_transcription_error(monkeypatch, tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"xx")

    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, text="<html>gateway error</html>")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    scribe = ElevenLabsScribe(api_key="k")

    with pytest.raises(TranscriptionError):
        await scribe.transcribe(audio)


# --- settlement direction (a repayment has a side) ---------------------------


async def _two_sided_debts(session):
    """The owner and Akmal owe each other — routine with a regular supplier."""
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    i_owe = m.Debt(
        direction=DebtDirection.i_owe_them,
        person_id=person.id,
        amount=Decimal("3000000"),
        currency=Currency.UZS,
        due_date=date(2026, 8, 20),  # due sooner, so it sorts first
        source_interaction_id=interaction.id,
    )
    they_owe = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
        due_date=date(2026, 9, 25),
        source_interaction_id=interaction.id,
    )
    session.add_all([i_owe, they_owe])
    await session.flush()
    return person, i_owe, they_owe


async def test_a_repayment_settles_the_side_it_was_made_on(session):
    """Repro: "Akmal 5 mln qaytardi" used to settle the owner's OWN debt."""
    _, i_owe, they_owe = await _two_sided_debts(session)

    await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal",
                    amount=5_000_000,
                    currency="UZS",
                    direction="they_owe_me",
                )
            ]
        ),
    )
    await session.flush()

    assert they_owe.status is DebtStatus.settled
    assert i_owe.status is DebtStatus.open  # the owner's own debt is untouched


async def test_the_owner_paying_back_settles_his_own_debt(session):
    _, i_owe, they_owe = await _two_sided_debts(session)

    await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal",
                    amount=3_000_000,
                    currency="UZS",
                    direction="i_owe_them",
                )
            ]
        ),
    )
    await session.flush()

    assert i_owe.status is DebtStatus.settled
    assert they_owe.status is DebtStatus.open


async def test_an_ambiguous_repayment_pays_nothing_and_asks(session):
    """With both sides open and no direction, guessing would invert the books."""
    _, i_owe, they_owe = await _two_sided_debts(session)

    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=5_000_000, currency="UZS")
            ]
        ),
    )
    await session.flush()

    assert i_owe.status is DebtStatus.open
    assert they_owe.status is DebtStatus.open
    assert applied.settlements == []
    assert applied.ambiguous_settlements == [
        ("Akmal", Decimal("5000000.00"), Currency.UZS)
    ]
    body = replies.confirmation(applied)
    assert "kim to'laganini yozing" in body


async def test_a_one_sided_repayment_still_needs_no_direction(session):
    """Only one side open: the direction is not in doubt, so it just applies."""
    person, debt = await _open_debt(session, amount="4000000")

    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=4_000_000, currency="UZS")
            ]
        ),
    )
    await session.flush()

    assert debt.status is DebtStatus.settled
    assert applied.ambiguous_settlements == []
