"""WP-13: a money row can be corrected, voided and restored — by command, by
button and through the owner API — and every change lands in its history."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from miya.api.main import app
from miya.bot import formatting as f
from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, TransactionType
from miya.db.session import SessionLocal
from miya.services import queries, records
from miya.services.persistence import Applied
from tests.test_close_and_correct import _Callback, _command, _Message

TZ = settings.tz
TOKEN = "owner-token-" + "x" * 52


@pytest.fixture
def bound(session, monkeypatch):
    """Handlers run inside the test session instead of opening their own."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def _txn(session, amount="250000", **over) -> m.Transaction:
    fields = {
        "type": TransactionType.expense,
        "amount": Decimal(amount),
        "currency": Currency.UZS,
        "category": "oziq-ovqat",
        "description": "KORZINKA.UZ",
        # Midday today, so /pul and "sana kecha" never straddle midnight.
        "occurred_at": datetime.now(TZ).replace(
            hour=12, minute=0, second=0, microsecond=0
        ),
    }
    fields.update(over)
    txn = m.Transaction(**fields)
    session.add(txn)
    await session.commit()
    return txn


async def _fresh(session, txn) -> m.Transaction:
    await session.refresh(txn)
    return txn


def test_the_x_ref_round_trips():
    assert f.parse_ref("x12") == ("transaction", 12)
    assert f.ref("transaction", 5) == "x5"


async def _tuzat(handle: str, args: str) -> str:
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"{handle} {args}"))
    [(text, _)] = message.sent
    return text


async def test_tuzat_amount_writes_the_history(bound):
    txn = await _txn(bound)
    text = await _tuzat(f"x{txn.id}", "300 ming")
    assert "Tuzatildi" in text and "300 ming so'm" in text
    fresh = await _fresh(bound, txn)
    assert fresh.amount == Decimal("300000.00")
    entry = fresh.history[-1]
    assert {k: entry[k] for k in ("field", "old", "new", "by")} == {
        "field": "amount",
        "old": "250000.00",
        "new": "300000.00",
        "by": "command",
    }


async def test_tuzat_direction_currency_note_and_date(bound):
    txn = await _txn(bound)
    handle = f"x{txn.id}"
    await _tuzat(handle, "teskari")
    assert (await _fresh(bound, txn)).type is TransactionType.income
    await _tuzat(handle, "chiqim")
    assert (await _fresh(bound, txn)).type is TransactionType.expense
    await _tuzat(handle, "kirim")
    assert (await _fresh(bound, txn)).type is TransactionType.income
    await _tuzat(handle, "valyuta $")
    assert (await _fresh(bound, txn)).currency is Currency.USD
    await _tuzat(handle, "izoh yuk uchun")
    assert (await _fresh(bound, txn)).description == "yuk uchun"
    await _tuzat(handle, "turkum Transport")
    assert (await _fresh(bound, txn)).category == "transport"

    before = txn.occurred_at.astimezone(TZ)
    await _tuzat(handle, "sana kecha")
    after = (await _fresh(bound, txn)).occurred_at.astimezone(TZ)
    assert after.date() == datetime.now(TZ).date() - timedelta(days=1)
    assert after.time() == before.time()


async def test_tuzat_an_unknown_person_asks_first(bound):
    txn = await _txn(bound)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"x{txn.id} kim Sardor"))
    [(text, markup)] = message.sent
    assert "Sardor" in text
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert payloads[0].startswith(f"rec:py:x{txn.id}:")
    fresh = await _fresh(bound, txn)
    assert fresh.counterparty_person_id is None
    assert await bound.scalar(sa.select(sa.func.count(m.Person.id))) == 0

    await handlers.on_record_button(_Callback(payloads[0], _Message()))
    fresh = await _fresh(bound, txn)
    assert fresh.counterparty_person_id is not None


async def test_ochir_voids_and_qaytar_restores(bound):
    txn = await _txn(bound)
    message = _Message()
    await handlers.cmd_void(message, _command("ochir", f"x{txn.id}"))
    [(text, markup)] = message.sent
    assert "o'chirildi" in text and "hisobotlarga kirmaydi" in text
    assert markup.inline_keyboard[0][0].callback_data == f"rec:r:x{txn.id}"
    fresh = await _fresh(bound, txn)
    assert fresh.voided_at is not None
    assert fresh.history[-1]["new"] == "void"
    assert (await queries.day_summary(bound, fresh.occurred_at.date())).expense == {}

    again = _Message()
    await handlers.cmd_void(again, _command("ochir", f"x{txn.id}"))
    assert again.sent[0][0] == replies.RECORD_ALREADY_CLOSED
    fresh = await _fresh(bound, txn)
    assert [h["field"] for h in fresh.history] == ["status"]

    back = _Message()
    await handlers.cmd_reopen(back, _command("qaytar", f"x{txn.id}"))
    assert "qaytarildi" in back.sent[0][0]
    fresh = await _fresh(bound, txn)
    assert fresh.voided_at is None
    assert [h["new"] for h in fresh.history] == ["void", "active"]


async def test_two_sessions_racing_to_void_write_one_entry(session):
    txn = await _txn(session)
    async with SessionLocal() as first, SessionLocal() as second:
        row = await records.load(first, "transaction", txn.id)
        await records.void(first, row, by="button")

        async def late() -> str:
            other = await records.load(second, "transaction", txn.id)
            try:
                await records.void(second, other, by="button")
            except records.NotOpen:
                return "already"
            return "voided"

        racing = asyncio.create_task(late())
        await asyncio.sleep(0.2)
        await first.commit()
        assert await racing == "already"
        await second.rollback()
    fresh = await _fresh(session, txn)
    assert len(fresh.history) == 1


async def test_refusals_on_a_money_row(bound):
    txn = await _txn(bound)
    handle = f"x{txn.id}"
    done = _Message()
    await handlers.cmd_done(done, _command("bajarildi", handle))
    assert "bajarildi" in done.sent[0][0] and "/ochir" in done.sent[0][0]

    wrong = _Message()
    await handlers.cmd_void(wrong, _command("ochir", "d12"))
    assert wrong.sent[0][0] == replies.OCHIR_ONLY_MONEY

    closed = _Message()
    await handlers.cmd_close(closed, _command("yop", handle))
    assert (await _fresh(bound, txn)).voided_at is not None

    text = await _tuzat(handle, "300 ming")
    assert "o'chirilgan" in text and "/qaytar" in text
    fresh = await _fresh(bound, txn)
    assert fresh.amount == Decimal("250000.00")
    assert [h["field"] for h in fresh.history] == ["status"]


async def test_the_void_button_trims_and_offers_undo(bound):
    txn = await _txn(bound)
    handle = f"x{txn.id}"
    message = _Message(reply_markup=keyboards.record_actions([("transaction", txn.id)]))
    await handlers.on_record_button(_Callback(f"rec:v:{handle}", message))
    assert message.edited_markup is None
    [(text, markup)] = message.sent
    assert "o'chirildi" in text
    assert markup.inline_keyboard[0][0].text == "↩️ Qaytar"


async def test_receipts_and_rows_carry_the_ref(session):
    txn = await _txn(session)
    applied = Applied(transactions=[txn])
    assert f"<code>x{txn.id}</code>" in replies.confirmation(applied)
    assert ("transaction", txn.id) in replies.confirmation_refs(applied)
    row = keyboards.record_row("transaction", txn.id, labelled=False)
    assert [b.text for b in row] == ["✏️ Tuzat", "🗑 O'chir"]


async def test_pul_lists_today_including_voided_rows(bound):
    live = await _txn(bound)
    dead = await _txn(bound, "90000", voided_at=datetime.now(TZ))
    message = _Message()
    await handlers.cmd_money_day(message, _command("pul", ""))
    [(text, markup)] = message.sent
    assert f"<code>x{live.id}</code>" in text and f"<code>x{dead.id}</code>" in text
    assert "🗑 o'chirilgan" in text and "✍️" in text
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"rec:v:x{live.id}" in payloads and f"rec:v:x{dead.id}" not in payloads


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    monkeypatch.setattr(settings, "upload_tokens", "phone:" + "d" * 64)
    with TestClient(app) as client:
        yield client


async def test_the_owner_api_voids_and_restores(session, api):
    txn = await _txn(session)
    url = f"/v1/transactions/{txn.id}"
    device = {"Authorization": "Bearer " + "d" * 64}
    owner = {"Authorization": f"Bearer {TOKEN}"}
    assert api.post(f"{url}/void", headers=device).status_code == 401

    first = api.post(f"{url}/void", headers=owner, json={"reason": "noto'g'ri"})
    assert first.status_code == 200 and first.json()["void_reason"] == "noto'g'ri"
    assert api.post(f"{url}/void", headers=owner).status_code == 409
    assert api.post(f"{url}/unvoid", headers=owner).status_code == 200
    assert api.post(f"{url}/unvoid", headers=owner).status_code == 409
    assert api.post("/v1/transactions/999999/void", headers=owner).status_code == 404
