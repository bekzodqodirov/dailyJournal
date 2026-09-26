"""WP-12: one booking service. The same payment seen through two channels is
one transaction with two evidence rows; separate payments stay separate; a
void sticks; the kill switch sends everything to /tekshir."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource, TransactionType
from miya.db.session import SessionLocal
from miya.services import money_events, sms_money

TZ = settings.tz
SMS = "Оплата 25 000 сум\nUZCARD *1234\nKORZINKA.UZ"
PUSH = "Payme: Oplata 25 000 so'm muvaffaqiyatli. Karta *1234"
PAYME_APP = "uz.dida.payme"
BASE = datetime.now(TZ).replace(microsecond=0) - timedelta(hours=2)


def _at(seconds: float = 0) -> datetime:
    return BASE + timedelta(seconds=seconds)


async def _event(session, *, at, body, sms_sender=None, app=None, card=None):
    """One money text: an SMS from ``sms_sender`` or a push from ``app``."""
    if card is not None:
        body = body.replace("1234", card)
    if sms_sender is not None:
        source, media = InteractionSource.phone_sms, {"type": "sms", "sender": sms_sender}
        channel = money_events.channel_for_sms(sms_sender)
    else:
        source, media = InteractionSource.phone_notification, {"app_label": "Payme"}
        channel = money_events.channel_for_app(app or PAYME_APP)
    row = m.Interaction(
        source=source,
        direction=Direction.in_,
        occurred_at=at,
        raw_text=body,
        processed=True,
        needs_review=False,
        media=media,
    )
    session.add(row)
    await session.flush()
    outcome = await money_events.apply_reading(
        session,
        row,
        sms_money.read(body, received_at=at),
        channel=channel,
        now=at,
    )
    await session.commit()
    return row, outcome


async def _sms(session, seconds=0, sender="Payme", **kw):
    return await _event(session, at=_at(seconds), body=SMS, sms_sender=sender, **kw)


async def _push(session, seconds=0, **kw):
    return await _event(session, at=_at(seconds), body=PUSH, app=PAYME_APP, **kw)


async def _count(session, model) -> int:
    return await session.scalar(sa.select(sa.func.count(model.id)))


async def test_an_sms_then_its_push_is_one_transaction(session):
    _, first = await _sms(session)
    push, second = await _push(session, 40)

    assert await _count(session, m.Transaction) == 1
    assert await _count(session, m.TransactionEvidence) == 2
    assert second.merged and second.transaction.id == first.transaction.id
    assert push.media["money"]["merged"] is True
    assert push.media["money"]["transaction_id"] == first.transaction.id
    [txn] = list(await session.scalars(sa.select(m.Transaction)))
    assert [h["field"] for h in txn.history] == ["evidence"]


async def test_the_push_first_then_the_sms_is_one_transaction(session):
    await _push(session)
    _, sms = await _sms(session, 40)
    assert sms.merged
    assert await _count(session, m.Transaction) == 1
    assert await _count(session, m.TransactionEvidence) == 2


async def test_two_equal_payments_from_one_sender_stay_two(session):
    await _sms(session)
    await _sms(session, 180)
    assert await _count(session, m.Transaction) == 2


async def test_interleaved_pairs_pair_up(session):
    await _sms(session)
    await _push(session, 20)
    await _sms(session, 180)
    await _push(session, 200)
    assert await _count(session, m.Transaction) == 2
    counts = (
        await session.execute(
            sa.select(sa.func.count(m.TransactionEvidence.id)).group_by(
                m.TransactionEvidence.transaction_id
            )
        )
    ).scalars()
    assert sorted(counts) == [2, 2]


async def test_a_different_card_is_a_different_payment(session):
    await _sms(session)
    await _push(session, 30, card="9999")
    assert await _count(session, m.Transaction) == 2


async def test_outside_the_window_is_a_different_payment(session, monkeypatch):
    monkeypatch.setattr(settings, "payment_dedupe_window_minutes", 10)
    await _sms(session)
    await _push(session, 11 * 60)
    assert await _count(session, m.Transaction) == 2


async def test_a_void_sticks_when_a_late_push_arrives(session):
    _, first = await _sms(session)
    txn = await session.get(m.Transaction, first.transaction.id)
    txn.voided_at = datetime.now(TZ)
    txn.void_reason = "owner"
    await session.commit()

    _, late = await _push(session, 60)
    assert late.merged and late.transaction.id == txn.id
    assert await _count(session, m.Transaction) == 1
    await session.refresh(txn)
    assert txn.voided_at is not None


async def test_the_bank_and_the_processor_sender_are_one_payment(session):
    await _sms(session, sender="8600")
    await _sms(session, 15, sender="Payme")
    assert await _count(session, m.Transaction) == 1
    channels = set(await session.scalars(sa.select(m.TransactionEvidence.channel)))
    assert channels == {"sms:8600", "sms:payme"}


async def test_two_sessions_at_once_book_one_transaction(session):
    rows = []
    for seconds, source, media, channel in (
        (0, InteractionSource.phone_sms, {"sender": "Payme"}, "sms:payme"),
        (30, InteractionSource.phone_notification, {}, f"app:{PAYME_APP}"),
    ):
        row = m.Interaction(
            source=source,
            direction=Direction.in_,
            occurred_at=_at(seconds),
            raw_text=SMS if source is InteractionSource.phone_sms else PUSH,
            processed=True,
            needs_review=False,
            media=media,
        )
        session.add(row)
        rows.append((row, channel))
    await session.commit()

    async def apply(row_id: int, channel: str) -> None:
        async with SessionLocal() as own:
            row = await own.get(m.Interaction, row_id)
            await money_events.apply_reading(
                own, row, sms_money.read(row.raw_text), channel=channel, now=_at()
            )
            await own.commit()

    await asyncio.gather(*(apply(row.id, channel) for row, channel in rows))
    assert await _count(session, m.Transaction) == 1
    assert await _count(session, m.TransactionEvidence) == 2


async def test_a_reposted_app_text_is_a_repeat(session):
    await _push(session)
    row, again = await _push(session, 30)
    assert again.verdict == "ignore" and again.transaction is None
    assert (row.media["money"]["verdict"], row.media["money"]["reason"]) == (
        "ignore",
        "repeat",
    )
    assert await _count(session, m.TransactionEvidence) == 1


async def test_the_kill_switch_sends_payments_to_review(session, monkeypatch):
    monkeypatch.setattr(settings, "money_autobook", False)
    row, outcome = await _sms(session)
    assert outcome.verdict == "review" and outcome.transaction is None
    assert row.needs_review is True
    assert row.media["money"]["reason"] == "autobook_off"
    assert await _count(session, m.Transaction) == 0


async def test_an_owner_typed_payment_is_not_merged_by_the_generic_path(session):
    typed = m.Transaction(
        type=TransactionType.expense,
        amount=Decimal("25000"),
        occurred_at=_at(),
    )
    session.add(typed)
    await session.commit()

    await _sms(session, 120)
    assert await _count(session, m.Transaction) == 2
    await session.refresh(typed)
    assert typed.channel is None
    evidence = set(await session.scalars(sa.select(m.TransactionEvidence.transaction_id)))
    assert typed.id not in evidence
