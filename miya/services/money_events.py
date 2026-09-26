"""One booking service for every money event (WP-12).

A bank SMS and a payment-app notification both end here, so there is one
writer of phone-sourced transactions. The reader (sms_money) gives a verdict;
this module applies it:

* IGNORE — stored, never asks;
* REVIEW — stored, waits in /tekshir, nothing booked;
* BOOK — under one advisory lock, either attached to the transaction this
  payment already made through another channel (the bank SMS and the Payme
  push, the bank sender and the processor sender) or booked as a new one.

Every reading lands on ``interaction.media["money"]`` so /tekshir and the
receipts can show what was read. Money is Decimal; currencies are never
mixed — a candidate must match currency, direction and amount exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    InteractionSource,
    TransactionType,
)
from miya.db.models import Debt, Interaction, Person, Transaction, TransactionEvidence
from miya.services import codes, money_notices, sms_money
from miya.services.extraction import ExtractedSettlement
from miya.services.sms_money import ParsedPayment, Verdict

MONEY_VERSION = 1
BOOK_LOCK = "miya:money-book"
AUTOBOOK_OFF = "autobook_off"
REPEAT = "repeat"


@dataclass(slots=True)
class MoneyOutcome:
    verdict: str
    transaction: Transaction | None
    merged: bool
    duplicate: bool


def channel_for_sms(sender: str) -> str:
    return "sms:" + sms_money.normalise_sender(sender)


def channel_for_app(package: str) -> str:
    return "app:" + package.strip().lower()


def _norm_text(text: str | None) -> str:
    return " ".join((text or "").split())


def _money(interaction: Interaction) -> dict:
    return dict((interaction.media or {}).get("money") or {})


def _set_money(interaction: Interaction, **changes) -> None:
    """Reassign, never mutate: JSONB change tracking sees only assignment."""
    interaction.media = {
        **(interaction.media or {}),
        "money": {**_money(interaction), **changes},
    }


def _label(interaction: Interaction, channel: str) -> str:
    media = interaction.media or {}
    return media.get("sender") or media.get("app_label") or channel.partition(":")[2]


# --- hooks filled in by later packages ------------------------------------------


TYPED_SOURCES = (InteractionSource.assistant_bot, InteractionSource.manual)


def _local_day(column):
    return sa.func.date(sa.func.timezone(settings.timezone, column))


def _same_payment(txn_type, amount, currency, occurred_at):
    """Filters shared by both directions of the typed/bank match (WP-42)."""
    window = timedelta(hours=settings.money_typed_match_hours)
    return (
        Transaction.voided_at.is_(None),
        Transaction.type == txn_type,
        Transaction.currency == currency,
        Transaction.amount == amount,
        Transaction.occurred_at.between(occurred_at - window, occurred_at + window),
        _local_day(Transaction.occurred_at) == occurred_at.astimezone(settings.tz).date(),
    )


def _nearest(occurred_at):
    return sa.func.abs(sa.extract("epoch", Transaction.occurred_at - occurred_at))


async def find_typed_match(
    session: AsyncSession, txn_type, amount, currency, occurred_at: datetime
) -> Transaction | None:
    """A payment the owner typed that this bank record confirms."""
    if settings.money_typed_match_hours <= 0:
        return None
    return await session.scalar(
        sa.select(Transaction)
        .join(Interaction, Interaction.id == Transaction.source_interaction_id)
        .where(
            *_same_payment(txn_type, amount, currency, occurred_at),
            Transaction.channel.is_(None),
            Interaction.source.in_(TYPED_SOURCES),
            ~sa.exists().where(TransactionEvidence.transaction_id == Transaction.id),
        )
        .order_by(_nearest(occurred_at))
        .limit(1)
        .with_for_update(of=Transaction)
    )


async def find_bank_match(
    session: AsyncSession, txn_type, amount, currency, occurred_at: datetime
) -> Transaction | None:
    """The bank record of a payment the owner is typing now."""
    if settings.money_typed_match_hours <= 0:
        return None
    return await session.scalar(
        sa.select(Transaction)
        .where(
            *_same_payment(txn_type, amount, currency, occurred_at),
            Transaction.channel.is_not(None),
            ~Transaction.history.contains([{"matched": "typed"}]),
        )
        .order_by(_nearest(occurred_at))
        .limit(1)
        .with_for_update()
    )


async def _typed_match(session, interaction, reading, *, channel):
    """The owner typed this payment before the bank reported it: the bank
    record becomes the typed row's evidence, not a second row (WP-42)."""
    typed = await find_typed_match(
        session, reading.type, reading.amount, reading.currency, interaction.occurred_at
    )
    if typed is None:
        return None
    session.add(
        TransactionEvidence(
            transaction_id=typed.id,
            interaction_id=interaction.id,
            channel=channel,
            card_last4=reading.card_last4,
        )
    )
    typed.channel = channel
    if typed.card_last4 is None:
        typed.card_last4 = reading.card_last4
    typed.history = [
        *(typed.history or []),
        {
            "at": datetime.now(settings.tz).isoformat(),
            "field": "evidence",
            "old": None,
            "new": channel,
            "by": "dedupe",
            "matched": "typed",
            "interaction_id": interaction.id,
        },
    ]
    await session.flush()
    if settings.money_receipt_on_typed_match:
        money_notices.note_typed_match(interaction, typed)
    return typed, True


async def _link_gs_code(
    session: AsyncSession,
    txn: Transaction,
    reading: ParsedPayment,
    interaction: Interaction,
) -> None:
    """A payment carrying exactly one GS code held by a client is theirs
    (WP-68): the transaction and the interaction name them, so /tarix,
    /kim and the assistant see it. Several codes, or an unheld one, stay in
    media.money.gs_codes only — never a fuzzy guess."""
    gs = list(reading.gs_codes)
    if len(gs) != 1 or txn.counterparty_person_id is not None:
        return
    code = gs[0]
    person = await codes.holder(session, code)
    if person is None:
        return
    txn.counterparty_person_id = person.id
    txn.history = [
        *(txn.history or []),
        {
            "at": datetime.now(settings.tz).isoformat(),
            "field": "person",
            "old": None,
            "new": person.id,
            "by": "gs_code",
            "code": code,
        },
    ]
    interaction.person_id = person.id
    _set_money(interaction, gs_person_id=person.id)
    await session.flush()
    if settings.money_gs_settle_ask and txn.type is TransactionType.income:
        await _ask_settlement(session, txn, interaction, person, code)


async def _ask_settlement(
    session: AsyncSession,
    txn: Transaction,
    interaction: Interaction,
    person: Person,
    code: str,
) -> None:
    """The client owes, and a coded payment came in: ask whether it pays the
    debt down. Only ever a question — a DebtPayment is written by "Ha"."""
    from miya.services import claims  # claims → persistence → money_events

    owes = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Debt)
        .where(
            Debt.person_id == person.id,
            Debt.direction == DebtDirection.they_owe_me,
            Debt.currency == txn.currency,
            Debt.status != DebtStatus.settled,
        )
    )
    if not owes:
        return
    item = ExtractedSettlement(
        person=person.display_name,
        amount=float(txn.amount),
        currency=txn.currency.value,
        note=f"bank: {code} {(interaction.raw_text or '')[:80]}".strip(),
        direction="they_owe_me",
        asserted_by="them",
    )
    claim = await claims.create(
        session, interaction, claims.KIND_SETTLEMENT, item, person_id=person.id
    )
    claim.payload = {**claim.payload, "origin": "bank_gs", "code": code}
    claim.evidence_txn_id = txn.id
    await session.flush()


async def own_cards(session: AsyncSession) -> set[str]:
    """The owner's cards: every last-4 on an active phone transaction whose
    text reported a balance (only the account holder is told a balance),
    plus PAYMENT_OWN_CARDS."""
    rows = await session.scalars(
        sa.select(sa.distinct(Transaction.card_last4))
        .join(Interaction, Interaction.id == Transaction.source_interaction_id)
        .where(
            Transaction.card_last4.is_not(None),
            Transaction.voided_at.is_(None),
            Transaction.channel.is_not(None),
            Interaction.media["money"]["balance_after"].astext.is_not(None),
        )
    )
    return {c for c in rows if c} | set(settings.payment_own_cards_list)


async def _mark_internal_pair(session: AsyncSession, txn: Transaction) -> None:
    """Money moved from one of the owner's cards to another is one expense
    and one income that cancel out: both are marked internal (WP-69), so the
    totals (queries.ACTIVE_TXN) leave them out. Either side may arrive first."""
    if txn.card_last4 is None or txn.is_internal or txn.voided_at is not None:
        return
    mine = await own_cards(session)
    if txn.card_last4 not in mine:
        return
    other_type = (
        TransactionType.expense
        if txn.type is TransactionType.income
        else TransactionType.income
    )
    window = timedelta(minutes=settings.payment_dedupe_window_minutes)
    pair = await session.scalar(
        sa.select(Transaction)
        .where(
            Transaction.id != txn.id,
            Transaction.type == other_type,
            Transaction.amount == txn.amount,
            Transaction.currency == txn.currency,
            Transaction.card_last4.in_(mine - {txn.card_last4}),
            Transaction.is_internal.is_(False),
            Transaction.voided_at.is_(None),
            Transaction.occurred_at.between(
                txn.occurred_at - window, txn.occurred_at + window
            ),
        )
        .order_by(
            sa.func.abs(sa.extract("epoch", Transaction.occurred_at - txn.occurred_at))
        )
        .limit(1)
        .with_for_update()
    )
    if pair is None:
        return
    at = datetime.now(settings.tz).isoformat()
    for row, other in ((txn, pair), (pair, txn)):
        row.is_internal = True
        row.history = [
            *(row.history or []),
            {
                "at": at,
                "field": "is_internal",
                "old": False,
                "new": True,
                "by": "dedupe",
                "pair": other.id,
            },
        ]
    await session.flush()


def internal_pair_id(txn: Transaction) -> int | None:
    """The other side of an own-card transfer, from the history."""
    for entry in reversed(txn.history or []):
        if entry.get("field") == "is_internal" and entry.get("pair"):
            return int(entry["pair"])
    return None


async def _match_bank_evidence(session, *, now: datetime, txn: Transaction) -> None:
    """A fresh bank row may prove a pending claim (WP-44)."""
    from miya.services import claims  # claims → persistence → money_events

    await claims.match_bank_evidence(session, now=now, txn=txn)


# --- booking ------------------------------------------------------------------------


async def _is_repeat(
    session: AsyncSession, interaction: Interaction, channel: str
) -> bool:
    """The same text through the same channel moments apart: an app that
    re-posts its notification, not a second payment."""
    window = timedelta(seconds=settings.payment_repeat_seconds)
    rows = await session.scalars(
        sa.select(Interaction.raw_text).where(
            Interaction.source == interaction.source,
            Interaction.media["money"]["channel"].astext == channel,
            Interaction.id != interaction.id,
            Interaction.occurred_at.between(
                interaction.occurred_at - window, interaction.occurred_at + window
            ),
        )
    )
    mine = _norm_text(interaction.raw_text)
    return any(_norm_text(text) == mine for text in rows)


async def book(
    session: AsyncSession,
    interaction: Interaction,
    reading: ParsedPayment,
    *,
    channel: str,
    check_repeat: bool = True,
) -> tuple[Transaction | None, bool]:
    """Attach this payment to the transaction another channel already booked,
    or book it. Returns (transaction, merged); (None, False) for a repeat.
    ``check_repeat=False`` when the owner books by hand: the owner's tap is
    never a re-posted notification."""
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtext(:lock))"), {"lock": BOOK_LOCK}
    )

    if check_repeat and await _is_repeat(session, interaction, channel):
        _set_money(interaction, verdict=Verdict.IGNORE.value, reason=REPEAT)
        interaction.needs_review = False
        return None, False

    typed = await _typed_match(session, interaction, reading, channel=channel)
    if typed is not None:
        return typed

    card = reading.card_last4
    moment = interaction.occurred_at
    window = timedelta(minutes=settings.payment_dedupe_window_minutes)
    # Only phone-evidenced rows merge here (channel IS NOT NULL): an
    # owner-typed row is matched by _typed_match alone. Voided rows stay
    # candidates, so a void sticks when a late push arrives.
    candidate = await session.scalar(
        sa.select(Transaction)
        .where(
            Transaction.currency == reading.currency,
            Transaction.type == reading.type,
            Transaction.amount == reading.amount,
            Transaction.occurred_at.between(moment - window, moment + window),
            Transaction.channel.is_not(None),
            sa.or_(
                Transaction.card_last4.is_(None),
                sa.true() if card is None else Transaction.card_last4 == card,
            ),
            ~sa.exists().where(
                TransactionEvidence.transaction_id == Transaction.id,
                TransactionEvidence.channel == channel,
            ),
        )
        .order_by(sa.func.abs(sa.extract("epoch", Transaction.occurred_at - moment)))
        .limit(1)
        .with_for_update()
    )
    if candidate is not None:
        session.add(
            TransactionEvidence(
                transaction_id=candidate.id,
                interaction_id=interaction.id,
                channel=channel,
                card_last4=card,
            )
        )
        if candidate.card_last4 is None and card is not None:
            candidate.card_last4 = card
        candidate.history = [
            *(candidate.history or []),
            {
                "at": datetime.now(settings.tz).isoformat(),
                "field": "evidence",
                "old": None,
                "new": channel,
                "by": "dedupe",
                "interaction_id": interaction.id,
            },
        ]
        await session.flush()
        await _link_gs_code(session, candidate, reading, interaction)
        return candidate, True

    raw = interaction.raw_text or ""
    txn = Transaction(
        type=reading.type,
        amount=reading.amount,
        currency=reading.currency,
        category=sms_money.category_of(reading.merchant, raw),
        description=f"{_label(interaction, channel)}: {reading.merchant or raw[:80]}",
        occurred_at=moment,
        source_interaction_id=interaction.id,
        card_last4=card,
        channel=channel,
        counterparty_person_id=None,  # a bank is not a counterparty
    )
    session.add(txn)
    await session.flush()
    session.add(
        TransactionEvidence(
            transaction_id=txn.id,
            interaction_id=interaction.id,
            channel=channel,
            card_last4=card,
        )
    )
    await session.flush()
    await _link_gs_code(session, txn, reading, interaction)
    await _mark_internal_pair(session, txn)
    return txn, False


async def apply_reading(
    session: AsyncSession,
    interaction: Interaction,
    reading: ParsedPayment,
    *,
    channel: str,
    now: datetime,
) -> MoneyOutcome:
    """Record the reading on the interaction and act on its verdict."""
    verdict, reason = reading.verdict, reading.reason
    if verdict is Verdict.BOOK and not settings.money_autobook:
        verdict, reason = Verdict.REVIEW, AUTOBOOK_OFF
    interaction.media = {
        **(interaction.media or {}),
        "money": {
            "v": MONEY_VERSION,
            "verdict": verdict.value,
            "reason": reason,
            "markers": list(reading.markers),
            "type": reading.type.value,
            "amount": str(reading.amount) if reading.amount is not None else None,
            "currency": reading.currency.value,
            "card_last4": reading.card_last4,
            "merchant": reading.merchant,
            "balance_after": (
                str(reading.balance_after) if reading.balance_after is not None else None
            ),
            "channel": channel,
            "gs_codes": list(reading.gs_codes),
            "waybills": list(reading.waybills),
            "transaction_id": None,
            "merged": False,
        },
    }
    txn: Transaction | None = None
    merged = duplicate = False
    if verdict is Verdict.IGNORE:
        interaction.needs_review = False
    elif verdict is Verdict.REVIEW:
        interaction.needs_review = True
    else:
        interaction.needs_review = False
        txn, merged = await book(session, interaction, reading, channel=channel)
        if txn is None:
            duplicate = True
            verdict = Verdict.IGNORE
        else:
            _set_money(interaction, transaction_id=txn.id, merged=merged)
            await _match_bank_evidence(session, now=now, txn=txn)
    await session.flush()
    money_notices.note(interaction, verdict.value, txn=txn, merged=merged, now=now)
    return MoneyOutcome(
        verdict=verdict.value, transaction=txn, merged=merged, duplicate=duplicate
    )


class NotBookable(Exception):
    """A review row with no readable amount: there is nothing to book."""


async def book_from_review(
    session: AsyncSession,
    interaction: Interaction,
    txn_type: TransactionType,
    *,
    by: str,
    now: datetime | None = None,
) -> tuple[Transaction, bool]:
    """The owner's 📉 Chiqim / 📈 Kirim in /tekshir: book what was read, with
    the owner's direction, through the same dedupe as every other event."""
    now = now or datetime.now(settings.tz)
    money = _money(interaction)
    if not money.get("amount"):
        raise NotBookable()
    reading = ParsedPayment(
        type=txn_type,
        amount=Decimal(money["amount"]),
        currency=Currency(money.get("currency") or Currency.UZS.value),
        card_last4=money.get("card_last4"),
        merchant=money.get("merchant"),
        balance_after=None,
        sender=(interaction.media or {}).get("sender") or "",
        confidence=sms_money.HIGH,
        verdict=Verdict.BOOK,
        reason="owner",
    )
    channel = money.get("channel") or _fallback_channel(interaction)
    txn, merged = await book(
        session, interaction, reading, channel=channel, check_repeat=False
    )
    assert txn is not None  # no repeat check, so book always returns a row
    _set_money(
        interaction,
        verdict=Verdict.BOOK.value,
        transaction_id=txn.id,
        merged=merged,
        resolved={"by": by, "at": now.isoformat(), "action": txn_type.value},
        owner_label=txn_type.value,
    )
    interaction.needs_review = False
    await session.flush()
    return txn, merged


def resolve_not_money(
    interaction: Interaction, *, by: str, now: datetime | None = None
) -> None:
    """✖️ Pul emas: the text is not a payment; nothing is booked."""
    now = now or datetime.now(settings.tz)
    _set_money(
        interaction,
        resolved={"by": by, "at": now.isoformat(), "action": "not_money"},
        owner_label="not_money",
    )
    interaction.needs_review = False


def _fallback_channel(interaction: Interaction) -> str:
    media = interaction.media or {}
    if media.get("sender"):
        return channel_for_sms(media["sender"])
    return channel_for_app(media.get("package") or "unknown")
