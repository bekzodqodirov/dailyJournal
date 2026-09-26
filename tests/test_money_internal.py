"""WP-69: a transfer between the owner's own cards is not income and expense."""

from __future__ import annotations

from datetime import datetime, timedelta

from miya.bot import replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import money_events, queries, records, sms_money

TZ = settings.tz
AT = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
OUT = "Spisanie 500 000 sum karta *1234. Balans: 3 000 000 sum"
IN = "Popolnenie 500 000 sum karta *5678. Balans: 900 000 sum"


async def _sms(session, body, *, at=AT, sender="Bank"):
    row = m.Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        occurred_at=at,
        raw_text=body,
        processed=True,
        media={"type": "sms", "sender": sender},
    )
    session.add(row)
    await session.flush()
    outcome = await money_events.apply_reading(
        session,
        row,
        sms_money.read(body, received_at=at),
        channel=money_events.channel_for_sms(sender),
        now=at,
    )
    return outcome.transaction


async def _totals(session):
    summary = await queries.spending_summary(session, AT.date(), AT.date())
    return summary.income, summary.expense


async def test_a_transfer_between_own_cards_is_internal(session):
    out = await _sms(session, OUT)
    into = await _sms(session, IN, at=AT + timedelta(minutes=2), sender="Bank2")

    assert out.is_internal and into.is_internal
    assert money_events.internal_pair_id(into) == out.id
    assert await _totals(session) == ({}, {})
    receipt = replies.money_internal_receipt(into, out)
    assert receipt.startswith("🔁 O'z kartalaringiz orasida: *1234 → *5678, ")
    assert f"<code>x{out.id}</code>, <code>x{into.id}</code>" in receipt


async def test_different_amounts_are_two_payments(session):
    out = await _sms(session, OUT)
    into = await _sms(
        session,
        "Popolnenie 400 000 sum karta *5678. Balans: 900 000 sum",
        at=AT + timedelta(minutes=2),
        sender="Bank2",
    )

    assert not out.is_internal and not into.is_internal


async def test_a_card_never_seen_with_a_balance_is_not_own(session):
    out = await _sms(session, OUT)
    into = await _sms(
        session, "Popolnenie 500 000 sum karta *9999", at=AT + timedelta(minutes=2)
    )

    assert not out.is_internal and not into.is_internal


async def test_own_cards_from_settings_count(session, monkeypatch):
    monkeypatch.setattr(settings, "payment_own_cards", "9999")
    out = await _sms(session, OUT)
    into = await _sms(
        session, "Popolnenie 500 000 sum karta *9999", at=AT + timedelta(minutes=2)
    )

    assert out.is_internal and into.is_internal


async def test_tuzat_tashqi_puts_it_back_in_the_totals(session):
    out = await _sms(session, OUT)
    await _sms(session, IN, at=AT + timedelta(minutes=2), sender="Bank2")

    edit = records.parse_edit("tashqi")
    assert edit == records.Edit("internal", False)
    await records.set_field(session, out, edit.field, edit.value, by="command")

    assert not out.is_internal
    income, expense = await _totals(session)
    assert income == {} and sum(expense.values()) == 500000
    assert records.parse_edit("ichki") == records.Edit("internal", True)
