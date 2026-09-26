"""WP-71: "who paid whom" and "which promise" are real questions, not
receipt text."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from miya.bot import formatting, keyboards
from miya.bot.notices import overflow_summary
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
)
from miya.services import claims, persistence, questions
from miya.services import extraction as ex

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0)


async def _akmal(session) -> m.Person:
    person = m.Person(display_name="Akmal", aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _note(session, text: str) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text=text,
    )
    session.add(row)
    await session.flush()
    return row


async def _both_ways(session, person):
    for direction in (DebtDirection.they_owe_me, DebtDirection.i_owe_them):
        session.add(
            m.Debt(
                direction=direction,
                person_id=person.id,
                amount=Decimal("10000000"),
                currency=Currency.UZS,
            )
        )
    await session.flush()


async def _repaid(session) -> m.Claim:
    row = await _note(session, "Akmal 5 mln qaytardi")
    await persistence.apply_extraction(
        session,
        row,
        ex.ExtractionResult(
            debt_settlements=[ex.ExtractedSettlement(person="Akmal", amount=5_000_000)]
        ),
    )
    [claim] = list(await session.scalars(sa.select(m.Claim)))
    return claim


async def test_ambiguous_repayment_is_parked_and_answered_by_direction(session):
    akmal = await _akmal(session)
    await _both_ways(session, akmal)

    claim = await _repaid(session)

    assert claim.state == claims.PENDING and claim.payload["origin"] == "ambiguous"
    line = formatting.claim_line(claims.view(claim))
    assert line == (
        f"❓ <code>c{claim.id}</code> <b>Akmal</b> bilan 5 mln so'm to'lov — "
        "kim kimga to'ladi?"
    )
    accepted = await claims.accept_with_direction(
        session, claim.id, DebtDirection.they_owe_me, by=claims.BY_BUTTON
    )
    assert accepted.written and claim.state == claims.ACCEPTED
    [payment] = list(await session.scalars(sa.select(m.DebtPayment)))
    debt = await session.get(m.Debt, payment.debt_id)
    assert debt.direction is DebtDirection.they_owe_me
    assert payment.amount == Decimal("5000000")


async def test_owner_origin_question_is_listed_in_savollar_after_overflow_folding(
    session,
):
    akmal = await _akmal(session)
    await _both_ways(session, akmal)
    claim = await _repaid(session)

    pending = await questions.collect(session, now=NOW, for_push=False)

    assert any(p.subject is not None and p.subject.id == claim.id for p in pending)
    folded = overflow_summary([("Guruh", {"debts": 3})])
    assert "/savollar" in folded


async def _promises(session, person) -> list[m.Promise]:
    rows = [
        m.Promise(
            made_by=PromiseMadeBy.them,
            person_id=person.id,
            description=text,
            status=PromiseStatus.open,
        )
        for text in ("hujjatlarni yuboradi", "hujjatlarni yuboradi (nusxa)")
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def _kept(session) -> m.Claim:
    row = await _note(session, "Akmal hujjatlar yubordi")
    await persistence.apply_extraction(
        session,
        row,
        ex.ExtractionResult(
            fulfilments=[
                ex.ExtractedFulfilment(
                    made_by="them", person="Akmal", description="hujjatlar yubordi"
                )
            ]
        ),
    )
    [claim] = list(await session.scalars(sa.select(m.Claim)))
    return claim


async def test_unclear_fulfilment_offers_candidates_and_closes_the_chosen_promise(
    session,
):
    akmal = await _akmal(session)
    first, second = await _promises(session, akmal)

    claim = await _kept(session)
    view = claims.view(claim)

    assert set(view.candidates) == {first.id, second.id}
    assert formatting.claim_line(view).endswith(
        "<b>Akmal</b>: «hujjatlar yubordi» — qaysi va'da bajarildi?"
    )
    chosen, promise = await claims.choose_promise(
        session, claim.id, second.id, by=claims.BY_BUTTON
    )
    assert promise.status is PromiseStatus.done and chosen.state == claims.ACCEPTED
    await session.refresh(first)
    assert first.status is PromiseStatus.open


async def test_hech_biri_closes_nothing(session):
    akmal = await _akmal(session)
    first, second = await _promises(session, akmal)
    claim = await _kept(session)

    await claims.decline(session, claim.id, by=claims.BY_BUTTON)

    for promise in (first, second):
        await session.refresh(promise)
        assert promise.status is PromiseStatus.open


def test_payloads_fit_64_bytes():
    big = 2_147_483_647
    markups = [
        keyboards.claim_direction(big),
        keyboards.claim_candidates(big, [big, big, big]),
    ]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
