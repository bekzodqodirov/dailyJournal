"""WP-44: a claim the bank already proves costs no tap and is never booked twice."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.bot import replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import claims, phone_events
from miya.services import extraction as ex
from miya.services.persistence import apply_extraction

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0) - timedelta(hours=1)
INCOME_SMS = "Karta *1234 hisobiga 5 000 000 so'm tushdi"
_ids = itertools.count(1)


async def _window(session, at: datetime = NOW) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        occurred_at=at,
        raw_text="Akmal: 5 mln o'tkazdim",
    )
    session.add(interaction)
    await session.flush()
    return interaction


def _txn_claim(kind: str = "income", amount: int = 5_000_000):
    return ex.ExtractedTransaction(
        type=kind, amount=amount, currency="UZS", counterparty="Akmal", asserted_by="them"
    )


def _settlement_claim():
    return ex.ExtractedSettlement(
        person="Akmal",
        amount=5_000_000,
        currency="UZS",
        direction="they_owe_me",
        asserted_by="them",
    )


async def _claimed(session, item, *, at: datetime = NOW):
    field = (
        "transactions"
        if isinstance(item, ex.ExtractedTransaction)
        else "debt_settlements"
    )
    applied = await apply_extraction(
        session, await _window(session, at), ex.ExtractionResult(**{field: [item]})
    )
    [claim] = applied.claims
    return claim, applied


async def _sms(session, at: datetime, body: str = INCOME_SMS):
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


async def _push(session, at: datetime, text: str = INCOME_SMS):
    await phone_events.ingest_notifications(
        session,
        "dev-1",
        [{"package": "uz.dida.payme", "posted_at": at.isoformat(), "text": text}],
    )
    await session.flush()


async def _txns(session) -> list[m.Transaction]:
    return list(
        await session.scalars(sa.select(m.Transaction).order_by(m.Transaction.id))
    )


async def test_bank_sms_closes_a_transaction_claim_without_a_second_row(session):
    await _sms(session, NOW - timedelta(minutes=10))
    claim, applied = await _claimed(session, _txn_claim())
    [bank] = await _txns(session)
    assert claim.evidence_txn_id == bank.id
    assert (claim.state, claim.answered_by) == (claims.AUTO, claims.BY_AUTO_BANK)
    assert (claim.result_kind, claim.result_id) == ("transaction", bank.id)
    assert replies.confirmation_claim_ids(applied) == []
    assert "💳 Bank: +" in replies.confirmation(applied)


async def test_bank_push_closes_it_too(session):
    await _push(session, NOW - timedelta(minutes=10))
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.state == claims.AUTO
    assert len(await _txns(session)) == 1


async def test_manual_ha_on_a_transaction_claim_links_the_bank_row(session, monkeypatch):
    monkeypatch.setattr(settings, "claim_bank_autoclose_transactions", False)
    await _sms(session, NOW - timedelta(minutes=10))
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.state == claims.PENDING and claim.evidence_txn_id is not None
    await claims.accept(session, claim.id, by=claims.BY_BUTTON, now=NOW)
    [bank] = await _txns(session)
    assert (claim.result_kind, claim.result_id) == ("transaction", bank.id)


async def test_settlement_with_bank_evidence_waits_for_a_tap_and_shows_the_bank_line(
    session,
):
    await _sms(session, NOW - timedelta(minutes=10))
    claim, _ = await _claimed(session, _settlement_claim())
    assert claim.state == claims.PENDING and claim.evidence_txn_id is not None
    await claims.load_evidence(session, [claim])
    line = replies.claim_line(claims.view(claim))
    assert "\n    💳 Bank: +" in line


async def test_autoaccept_settlements_flag_writes_one_payment(session, monkeypatch):
    monkeypatch.setattr(settings, "claim_bank_autoaccept_settlements", True)
    akmal = m.Person(display_name="Akmal", aliases=[])
    session.add(akmal)
    await session.flush()
    session.add(
        m.Debt(
            person_id=akmal.id, direction="they_owe_me", amount=5_000_000, currency="UZS"
        )
    )
    await session.flush()
    await _sms(session, NOW - timedelta(minutes=10))
    claim, _ = await _claimed(session, _settlement_claim())
    assert claim.state == claims.ACCEPTED and claim.answered_by == claims.BY_AUTO_BANK
    payments = list(await session.scalars(sa.select(m.DebtPayment)))
    assert len(payments) == 1


async def test_two_equal_claims_or_two_equal_sms_match_nothing(session):
    await _sms(session, NOW - timedelta(minutes=10))
    await _sms(session, NOW - timedelta(minutes=20), INCOME_SMS + " ")
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.evidence_txn_id is None and claim.state == claims.PENDING


async def test_voided_other_currency_or_outside_48h_is_not_evidence(session):
    await _sms(session, NOW - timedelta(hours=49))
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.evidence_txn_id is None
    [old] = await _txns(session)
    old.occurred_at = NOW
    old.voided_at = NOW
    await session.flush()
    assert await claims.match_bank_evidence(session, now=NOW, claim=claim) == []


async def test_an_expense_never_proves_an_income_claim(session):
    await _sms(session, NOW - timedelta(minutes=10), "Oplata 5 000 000 sum. Karta *1234")
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.evidence_txn_id is None


async def test_sms_arriving_after_the_claim_resolves_it(session):
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.state == claims.PENDING
    await _sms(session, NOW + timedelta(minutes=10))
    assert claim.state == claims.AUTO
    assert len(await _txns(session)) == 1


async def test_decline_frees_the_bank_row(session, monkeypatch):
    monkeypatch.setattr(settings, "claim_bank_autoclose_transactions", False)
    await _sms(session, NOW - timedelta(minutes=10))
    claim, _ = await _claimed(session, _txn_claim())
    await claims.decline(session, claim.id, by=claims.BY_BUTTON, now=NOW)
    assert claim.evidence_txn_id is None
