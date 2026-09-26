"""WP-70: money rows for questions, straight from SQL."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, Direction, InteractionSource, TransactionType
from miya.services import codes, rag

TZ = settings.tz


async def _txn(session, amount, *, at, person=None, gs=None, voided=False):
    source = m.Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        occurred_at=at,
        raw_text="x",
        processed=True,
        media={"type": "sms", "money": {"gs_codes": [gs] if gs else []}},
    )
    session.add(source)
    await session.flush()
    txn = m.Transaction(
        type=TransactionType.income,
        amount=Decimal(amount),
        currency=Currency.UZS,
        description="Payme: to'lov",
        occurred_at=at,
        source_interaction_id=source.id,
        counterparty_person_id=person.id if person else None,
        card_last4="1234",
        channel="sms:payme",
        voided_at=at if voided else None,
    )
    session.add(txn)
    await session.flush()
    return txn


async def _tool(session, **args) -> dict:
    return json.loads(await rag._run_tool(session, None, "list_transactions", args))


async def test_a_client_code_lists_only_that_clients_active_rows(session):
    akmal = m.Person(display_name="Akmal", aliases=[])
    vali = m.Person(display_name="Vali", aliases=[])
    session.add_all([akmal, vali])
    await session.flush()
    await codes.attach(session, akmal, "GS367", source="command", by="command")
    now = datetime.now(TZ).replace(microsecond=0)
    linked = await _txn(session, "1500000", at=now, person=akmal)
    coded = await _txn(session, "700000", at=now - timedelta(hours=1), gs="GS367")
    await _txn(session, "900000", at=now, person=vali)
    await _txn(session, "300000", at=now, person=akmal, voided=True)

    out = await _tool(session, person="GS367")

    assert out["client_code"] == "GS367" and out["count"] == 2
    assert out["transactions"][0].startswith(f"x{linked.id} · ")
    assert out["transactions"][1].startswith(f"x{coded.id} · ")
    assert " · +1.5 mln so'm · Akmal · " in out["transactions"][0]
    assert all("900" not in line and "300" not in line for line in out["transactions"])


async def test_voided_rows_never_appear(session):
    now = datetime.now(TZ).replace(microsecond=0)
    await _txn(session, "300000", at=now, voided=True)

    assert (await _tool(session))["count"] == 0


async def test_dates_are_inclusive_in_tashkent_time(session):
    day = datetime(2026, 9, 20, tzinfo=TZ)
    early = await _txn(session, "100000", at=day.replace(hour=0, minute=5))
    late = await _txn(session, "200000", at=day.replace(hour=23, minute=55))
    await _txn(session, "300000", at=day + timedelta(days=1, minutes=5))

    out = await _tool(session, date_from="2026-09-20", date_to="2026-09-20")

    refs = [line.split(" · ")[0] for line in out["transactions"]]
    assert refs == [f"x{late.id}", f"x{early.id}"]


async def test_an_unknown_name_is_said(session):
    out = await _tool(session, person="Hech kim")
    assert "transactions" not in out
