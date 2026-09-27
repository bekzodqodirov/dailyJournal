"""WP-42: a payment the owner typed and the bank's record of it are one row."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import extraction as ex
from miya.services import phone_events
from miya.services.persistence import apply_extraction
from tests.test_close_and_correct import _Callback, _Message, bound  # noqa: F401

TZ = settings.tz
NOON = datetime(2026, 9, 20, 12, 0, tzinfo=TZ)
SMS = "Платёж успешно проведён\nKORZINKA\n250 000 сум"
_ids = iter(range(1, 10_000))


async def _typed(session, at: datetime, amount: int = 250_000):
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=at,
        raw_text="Korzinkaga 250 ming to'ladim",
    )
    session.add(interaction)
    await session.flush()
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            transactions=[
                ex.ExtractedTransaction(
                    type="expense",
                    amount=amount,
                    category="oziq-ovqat",
                    description="Korzinka",
                )
            ]
        ),
    )
    return interaction, applied


async def _sms(session, at: datetime, body: str = SMS):
    await phone_events.ingest_sms(
        session,
        "dev-1",
        [
            {
                "sms_id": next(_ids),
                "sender": "Payme",
                "received_at": at.isoformat(),
                "body": body,
                "sim_slot": 0,
            }
        ],
    )
    await session.flush()


class _Editable(_Message):
    edited_text: str | None = None

    async def edit_text(self, text, reply_markup=None):
        self.edited_text = text


async def _txns(session) -> list[m.Transaction]:
    return list(
        await session.scalars(sa.select(m.Transaction).order_by(m.Transaction.id))
    )


async def _evidence(session) -> list[m.TransactionEvidence]:
    return list(await session.scalars(sa.select(m.TransactionEvidence)))


async def test_typed_then_bank_is_one_transaction(session):
    await _typed(session, NOON)
    await _sms(session, NOON + timedelta(minutes=5))
    [txn] = await _txns(session)
    assert txn.channel == "sms:payme"
    assert len(await _evidence(session)) == 1
    assert txn.history[-1]["matched"] == "typed"


async def test_bank_then_typed_matches_and_can_be_split(bound):  # noqa: F811
    await _sms(bound, NOON - timedelta(minutes=5))
    interaction, applied = await _typed(bound, NOON)
    [bank] = await _txns(bound)
    assert applied.transactions == []
    assert [t.id for t, _, _ in applied.matched_transactions] == [bank.id]
    assert bank.category == "oziq-ovqat" and bank.description == "Korzinka"
    text = replies.confirmation(applied)
    assert "bir xil to'lov" in text

    _, keyboard = handlers._receipt(type("R", (), {"ok": True, "applied": applied})())
    split = [
        b for row in keyboard.inline_keyboard for b in row if b.text.startswith("➕")
    ]
    assert split[0].callback_data == f"mx:split:{interaction.id}:0"

    message = _Editable(reply_markup=keyboard)
    await handlers.on_money_split(_Callback(split[0].callback_data, message))
    txns = await _txns(bound)
    assert len(txns) == 2
    shown = message.sent[-1][0] if message.sent else message.edited_text
    assert shown == replies.money_split_done(txns[-1])
    # A second tap writes nothing more.
    await handlers.on_money_split(_Callback(split[0].callback_data, _Message()))
    assert len(await _txns(bound)) == 2


async def test_another_day_is_another_payment(session):
    await _typed(session, NOON - timedelta(days=1))
    await _sms(session, NOON)
    assert len(await _txns(session)) == 2


async def test_eight_hours_apart_is_another_payment(session):
    await _typed(session, datetime(2026, 9, 20, 9, 0, tzinfo=TZ))
    await _sms(session, datetime(2026, 9, 20, 17, 0, tzinfo=TZ))
    assert len(await _txns(session)) == 2


async def test_a_typed_row_matches_one_bank_record_only(session):
    await _typed(session, NOON)
    await _sms(session, NOON + timedelta(minutes=5))
    await _sms(
        session,
        NOON + timedelta(minutes=30),
        "Платёж успешно проведён\nMAKRO\n250 000 сум",
    )
    assert len(await _txns(session)) == 2


@pytest.mark.parametrize("hours", [0])
async def test_zero_hours_disables_matching(session, monkeypatch, hours):
    monkeypatch.setattr(settings, "money_typed_match_hours", hours)
    await _typed(session, NOON)
    await _sms(session, NOON + timedelta(minutes=5))
    assert len(await _txns(session)) == 2
