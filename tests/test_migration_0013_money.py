"""Migration 0013 (WP-11): the frozen key functions and the backfills, run on
seeded rows; the Alembic round trip runs only on MIYA_MIGRATION_DATABASE_URL."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource, TransactionType
from miya.db.session import engine
from miya.services import phone_events, sms_money

ROOT = Path(__file__).resolve().parents[1]
TZ = settings.tz


def _load():
    path = ROOT / "alembic" / "versions" / "0013_money_ledger.py"
    spec = importlib.util.spec_from_file_location("migration_0013", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load()
AT = datetime(2026, 9, 20, 14, 30, 5, tzinfo=TZ)


@pytest.mark.parametrize(
    ("sender", "body"),
    [
        ("Payme", "Oplata 25 000 sum. Karta *1234"),
        ("Kapital Bank", "Оплата 25 000 сум\nОстаток: 1 250 000 сум"),
        ("Click", "Oplata 1 250 000 sum"),
        ("Uzum", "To'lov 25 000 so'm 🎉"),
        ("PAYME", "Oplata 25 000 sum   "),
        ("8600", "Oplata 25 000 sum\r\nKarta *1234"),
    ],
)
def test_the_frozen_sms_key_equals_the_app(sender, body):
    assert mig._frozen_sms_content_key(sender, AT, body) == phone_events.sms_content_key(
        sender, AT, body
    )
    assert mig._frozen_sms_content_key(
        sender, AT.isoformat(), body
    ) == phone_events.sms_content_key(sender, AT.astimezone(UTC), body)


def test_the_frozen_call_key_equals_the_app():
    for number in ("+998 90 123-45-67", None, "12"):
        assert mig._frozen_call_content_key(
            number, AT, 42, "incoming"
        ) == phone_events.call_content_key(number, AT, 42, "incoming")


def test_the_frozen_sender_normalisation():
    assert mig._frozen_normalise_sender("Kapital Bank") == "kapitalbank"
    for sender in ("Капитал Банк", "O'zbank", "PAY-ME."):
        assert mig._frozen_normalise_sender(sender) == sms_money.normalise_sender(sender)


def _legacy_sms(device: str, sms_id: int, body: str, at=AT) -> m.Interaction:
    """An SMS row as 0012 wrote it: an event key, no content key."""
    return m.Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        occurred_at=at,
        raw_text=body,
        processed=True,
        needs_review=False,
        media={
            "type": "sms",
            "event_key": f"{device}:sms:{sms_id}:0000",
            "sender": "Payme",
            "device_id": device,
            "payment": {
                "amount": "25000.00",
                "currency": "UZS",
                "card_last4": "1234",
                "merchant": None,
                "balance_after": None,
            },
        },
    )


def _txn(interaction: m.Interaction) -> m.Transaction:
    return m.Transaction(
        type=TransactionType.expense,
        amount=Decimal("25000"),
        occurred_at=interaction.occurred_at,
        source_interaction_id=interaction.id,
    )


async def test_backfill_content_keys_marks_a_past_reinstall(session):
    body = "Oplata 25 000 sum. Karta *1234"
    first = _legacy_sms("device-a", 5, body)
    again = _legacy_sms("device-b", 1, body)
    other = _legacy_sms("device-b", 2, body, at=AT + timedelta(seconds=1))
    session.add_all([first, again, other])
    await session.flush()
    session.add_all([_txn(first), _txn(again)])
    await session.commit()

    async with engine.begin() as conn:
        counts = await conn.run_sync(mig.backfill_content_keys)
    assert counts == {"keyed": 2, "duplicates": 1, "voided": 1}

    for row in (first, again, other):
        await session.refresh(row)
    assert first.media["content_key"] == phone_events.sms_content_key("Payme", AT, body)
    assert "content_key" not in again.media
    assert again.media["content_key_duplicate_of"] == first.id
    assert other.media["content_key"] != first.media["content_key"]

    live, dead = list(
        await session.scalars(
            sa.select(m.Transaction).order_by(m.Transaction.source_interaction_id)
        )
    )
    assert live.voided_at is None
    assert dead.void_reason == "reinstall_duplicate"
    [entry] = dead.history
    assert (entry["old"], entry["new"], entry["by"]) == ("active", "void", "migration")


async def test_backfill_evidence_links_each_sms_transaction(session):
    row = _legacy_sms("device-a", 1, "Oplata 25 000 sum. Karta *1234")
    session.add(row)
    await session.flush()
    txn = _txn(row)
    session.add(txn)
    await session.commit()

    async with engine.begin() as conn:
        assert await conn.run_sync(mig.backfill_evidence) == 1
        assert await conn.run_sync(mig.backfill_evidence) == 0  # idempotent

    [evidence] = list(await session.scalars(sa.select(m.TransactionEvidence)))
    assert (evidence.channel, evidence.card_last4) == ("sms:payme", "1234")
    assert evidence.interaction_id == row.id
    await session.refresh(txn)
    assert (txn.channel, txn.card_last4) == ("sms:payme", "1234")


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


def test_the_round_trip():
    url = os.environ.get("MIYA_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("MIYA_MIGRATION_DATABASE_URL is not set")
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "-1")
    _alembic(url, "upgrade", "head")
