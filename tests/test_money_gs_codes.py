"""WP-68: a GS-coded payment belongs to its client; settling always asks."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.bot import formatting, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection, Direction, InteractionSource
from miya.services import claims, codes, money_events, sms_money

TZ = settings.tz
AT = datetime.now(TZ).replace(microsecond=0) - timedelta(hours=1)
PAID = "GS367 uchun 1 500 000 so'm tushdi, karta *1234"


async def _akmal(session, code="GS367") -> m.Person:
    person = m.Person(display_name="Akmal", aliases=[])
    session.add(person)
    await session.flush()
    await codes.attach(session, person, code, source="command", by="command")
    return person


async def _pay(session, body=PAID):
    row = m.Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        occurred_at=AT,
        raw_text=body,
        processed=True,
        media={"type": "sms", "sender": "Payme"},
    )
    session.add(row)
    await session.flush()
    outcome = await money_events.apply_reading(
        session,
        row,
        sms_money.read(body, received_at=AT),
        channel=money_events.channel_for_sms("Payme"),
        now=AT,
    )
    return row, outcome.transaction


async def test_a_coded_payment_is_linked_to_its_client(session):
    akmal = await _akmal(session)

    row, txn = await _pay(session)

    assert txn.counterparty_person_id == akmal.id
    assert row.person_id == akmal.id
    [entry] = [h for h in txn.history if h.get("field") == "person"]
    assert entry["by"] == "gs_code" and entry["code"] == "GS367"
    assert entry["new"] == akmal.id
    receipt = replies.money_receipt(txn, row, akmal)
    assert "👤 Akmal (GS367)" in receipt


async def test_two_codes_link_nobody(session):
    await _akmal(session)

    row, txn = await _pay(session, "GS367 GS368 uchun 1 500 000 so'm tushdi, karta *1234")

    assert txn.counterparty_person_id is None and row.person_id is None
    assert row.media["money"]["gs_codes"] == ["GS367", "GS368"]


async def test_an_unknown_code_is_kept_but_not_linked(session):
    row, txn = await _pay(session, "GS999 uchun 1 500 000 so'm tushdi, karta *1234")

    assert txn.counterparty_person_id is None
    assert row.media["money"]["gs_codes"] == ["GS999"]
    receipt = replies.money_receipt(txn, row, None)
    assert "🏷 GS999 — bu kod hali hech kimga bog'lanmagan" in receipt


async def test_the_waybill_is_never_the_amount(session):
    row, txn = await _pay(
        session, "Popolnenie 1 500 000 sum YW26-004715 GS999 karta *1234"
    )

    assert txn.amount == Decimal("1500000.00")
    assert row.media["money"]["waybills"] == ["YW26-004715"]


async def test_settling_asks_with_evidence_and_writes_no_payment(session, monkeypatch):
    monkeypatch.setattr(settings, "money_gs_settle_ask", True)
    akmal = await _akmal(session)
    session.add(
        m.Debt(
            direction=DebtDirection.they_owe_me,
            person_id=akmal.id,
            amount=Decimal("5000000"),
            currency=Currency.UZS,
        )
    )
    await session.flush()

    row, txn = await _pay(session)

    [claim] = list(await session.scalars(sa.select(m.Claim)))
    assert claim.state == claims.PENDING and claim.kind == claims.KIND_SETTLEMENT
    assert claim.evidence_txn_id == txn.id and claim.person_id == akmal.id
    assert claim.payload["origin"] == "bank_gs"
    assert await session.scalar(sa.select(sa.func.count(m.DebtPayment.id))) == 0
    line = formatting.claim_line(claims.view(claim))
    assert line.startswith(f"❓ <code>c{claim.id}</code> Bank: <b>Akmal</b> (GS367) ")
    assert line.endswith("yubordi — qarzidan ayirilsinmi?")


async def test_no_question_without_the_switch_or_a_debt(session, monkeypatch):
    await _akmal(session)
    await _pay(session)
    monkeypatch.setattr(settings, "money_gs_settle_ask", True)
    await _pay(session, "GS367 uchun 2 000 000 so'm tushdi, karta *1234")

    assert await session.scalar(sa.select(sa.func.count(m.Claim.id))) == 0
