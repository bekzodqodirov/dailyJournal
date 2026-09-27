"""WP-82: the owner's labelled payment texts, anonymised, for parser tests."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, Direction, InteractionSource, TransactionType
from miya.services import records
from miya.tools import payments_corpus

NOW = datetime.now(settings.tz).replace(microsecond=0)


def test_card_digits_and_phones_are_masked_amounts_are_not():
    text = "UZCARD 8600 **** **** 1234, karta *5678, +998 90 123 45 67; 1 500 000 so'm"
    out = payments_corpus.anonymise(text)

    assert "1234" not in out and "5678" not in out and "8600" not in out
    assert "123 45 67" not in out
    assert "1 500 000 so'm" in out


async def _sms(session, text: str, money: dict) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text=text,
        processed=True,
        media={"type": "sms", "money": {"channel": "sms:bank", **money}},
    )
    session.add(row)
    await session.flush()
    return row


async def test_only_labelled_rows_are_exported_by_default(session, tmp_path):
    await _sms(
        session, "karta *1234 50 000", {"verdict": "book", "owner_label": "expense"}
    )
    await _sms(session, "karta *9999 70 000", {"verdict": "book"})
    await session.commit()

    out = tmp_path / "p.jsonl"
    assert await payments_corpus.export(out) == 1
    [row] = [json.loads(line) for line in out.read_text().splitlines()]
    assert row == {
        "text": "karta *0000 50 000",
        "channel": "sms:bank",
        "verdict": "book",
        "reason": None,
        "owner_label": "expense",
    }
    assert await payments_corpus.export(out, include_all=True) == 2


async def test_voiding_a_phone_row_labels_its_text(session):
    source = await _sms(session, "karta *1234 50 000", {"verdict": "book"})
    txn = m.Transaction(
        type=TransactionType.expense,
        amount=Decimal("50000"),
        currency=Currency.UZS,
        occurred_at=NOW,
        source_interaction_id=source.id,
        channel="sms:bank",
    )
    session.add(txn)
    await session.flush()

    await records.void(session, txn, by="button")

    await session.refresh(source)
    assert source.media["money"]["owner_label"] == "voided"
