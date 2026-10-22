"""Receipts read from images (JV-04).

A payment screenshot or a terminal check — sent to the bot, or into a chat
the userbot reads — is read once by ``vision.read_image``; this module turns
the model's answer into a checked ``receipts`` row and mirrors it onto the
interaction (``meta.receipt``).

What a receipt says is the image's word, never the bank's record, so nothing
here writes the ledger. Money reaches the books the way it always has: the
owner's own words through extraction, a counterparty's words through the
claims gate (``claims.create`` carries the receipt's printed time as
``receipt_at``), and the bank's SMS or app push through ``money_events``.

The bank cross-check is ``verdict`` (JV-05): ``match_bank`` links a receipt
to the bank's own income record of the same amount and currency near the
printed time (``confirmed``); ``check_waiting`` gives a receipt the bank has
not confirmed after RECEIPT_BANK_WAIT_MINUTES ``unconfirmed`` — or
``suspicious`` for a concrete reason, never "fake". The owner's ✅ Pul keldi /
🚩 Soxta (``set_owner_verdict``) is final. No verdict writes money: a
confirmed receipt points at the bank's row, it never makes one.

A read that failed, or a receipt whose amount could not be read, is kept as
``needs_review`` with the interaction flagged — never dropped. The owner's
receipt card goes out with the bot's reply for a photo sent to the bot, and
from the worker's ``receipt_notices`` job for a receipt someone sent into a
chat, once (``notified_at``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from miya.config import settings
from miya.db.enums import (
    ChatType,
    Currency,
    Direction,
    InteractionSource,
    TransactionType,
)
from miya.db.models import (
    ChatMonitor,
    Heartbeat,
    Interaction,
    Person,
    Receipt,
    Transaction,
)
from miya.services import money_events, queries
from miya.services.extraction import to_money
from miya.services.text import alias_pattern, fold_apostrophes
from miya.services.vision import ReceiptFields, VisionReading

log = logging.getLogger(__name__)

READING_VERSION = 1
# interactions.meta.receipt: the structured reading, mirrored (§4.3).
META_KEY = "receipt"

STATE_READ = "read"
STATE_NEEDS_REVIEW = "needs_review"
STATE_REJECTED = "rejected"
VERDICT_WAITING = "waiting"
IN = "in"
OUT = "out"
UNKNOWN = "unknown"

BY_VISION = "vision"
# A chat receipt older than this is history, not news: no card is sent.
NOTICE_MAX_AGE = timedelta(days=2)
NOTICE_BATCH = 20
# A printed name longer than this is not a name.
MAX_NAME_CHARS = 120
MAX_REFERENCE_CHARS = 64
# receipts.amount is NUMERIC(14,2): a larger figure is a card or reference
# number misread as the total, so the amount counts as unread.
MAX_AMOUNT = Decimal("1000000000000")

# The printed date and time as the prompt asks for it ("2026-10-20T14:05"); a
# space for the T, seconds and a UTC offset or "Z" are tolerated.
_PAID_AT = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})[T ](?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.\d+)?)?(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)
# ASCII only: ck_receipts_*_card_last4 accepts nothing else.
_DIGITS = re.compile(r"[0-9]")
_SPACES = re.compile(r"\s+")


def parse_paid_at(value: str | None) -> datetime | None:
    """The printed moment, on the owner's clock; None without a time of day.

    A receipt printed with a date and no time keeps its date in ``reading``
    only: a bank match needs the minute, and midnight would be a guess.
    """
    if not value:
        return None
    match = _PAID_AT.match(value.strip())
    if match is None:
        return None
    text = (
        f"{match.group('day')}T{int(match.group('hour')):02d}:{match.group('minute')}"
        f":{match.group('second') or '00'}"
    )
    offset = match.group("tz")
    try:
        if offset is None:
            return datetime.fromisoformat(text).replace(tzinfo=settings.tz)
        if offset == "Z":
            offset = "+00:00"
        elif ":" not in offset:
            offset = f"{offset[:3]}:{offset[3:]}"
        return datetime.fromisoformat(text + offset).astimezone(settings.tz)
    except (ValueError, OverflowError):
        # OverflowError: a date at the calendar's edge ("9999-12-31T23:59Z")
        # that cannot move to the owner's clock.
        return None


def card_last4(value: str | None) -> str | None:
    """The last four digits of a card as printed, or None when fewer show."""
    digits = "".join(_DIGITS.findall(value or ""))
    return digits[-4:] if len(digits) >= 4 else None


def clean_text(value: str | None, *, limit: int = MAX_NAME_CHARS) -> str | None:
    """A printed name or number: one line, trimmed, None when empty."""
    text = _SPACES.sub(" ", value or "").strip()
    return text[:limit] if text else None


@dataclass(slots=True)
class ParsedReceipt:
    """The columns a reading gives, before the direction is worked out."""

    amount: Decimal | None
    currency: Currency | None
    paid_at: datetime | None
    payer_name: str | None
    payee_name: str | None
    payer_card_last4: str | None
    payee_card_last4: str | None
    issuer: str | None
    reference: str | None

    @property
    def readable(self) -> bool:
        """Enough to be a receipt at all: an amount and its currency."""
        return self.amount is not None and self.currency is not None


def parse_fields(fields: ReceiptFields | None) -> ParsedReceipt:
    if fields is None:
        return ParsedReceipt(None, None, None, None, None, None, None, None, None)
    amount = to_money(fields.amount) if fields.amount is not None else None
    if amount is not None and amount >= MAX_AMOUNT:
        amount = None
    return ParsedReceipt(
        amount=amount,
        currency=Currency(fields.currency) if amount is not None else None,
        paid_at=parse_paid_at(fields.paid_at),
        payer_name=clean_text(fields.payer_name),
        payee_name=clean_text(fields.payee_name),
        payer_card_last4=card_last4(fields.payer_card_last4),
        payee_card_last4=card_last4(fields.payee_card_last4),
        issuer=clean_text(fields.issuer),
        reference=clean_text(fields.reference, limit=MAX_REFERENCE_CHARS),
    )


def _is_owner_name(name: str | None, pattern: re.Pattern[str] | None) -> bool:
    return bool(name and pattern and pattern.search(fold_apostrophes(name)))


async def direction_of(session: AsyncSession, parsed: ParsedReceipt) -> str:
    """Money to the owner or from them, by their cards and their names.

    Only a side that is plainly the owner's decides: one of their cards
    (``money_events.own_cards``) or one of their names (OWNER_ALIASES). Both
    sides theirs, or neither, is ``unknown`` — never a guess from who sent it.
    """
    mine = await money_events.own_cards(session)
    pattern = alias_pattern(settings.owner_aliases_parsed)
    payee_mine = parsed.payee_card_last4 in mine or _is_owner_name(
        parsed.payee_name, pattern
    )
    payer_mine = parsed.payer_card_last4 in mine or _is_owner_name(
        parsed.payer_name, pattern
    )
    if payee_mine and not payer_mine:
        return IN
    if payer_mine and not payee_mine:
        return OUT
    return UNKNOWN


def _reading_json(reading: VisionReading) -> dict[str, Any]:
    """``receipts.reading``: the model's answer, for audit only.

    The card fields are masked here too: a model that copied a whole card
    number must not leave it in the audit copy.
    """
    receipt = reading.receipt.model_dump(mode="json") if reading.receipt else None
    if receipt is not None:
        for key in ("payer_card_last4", "payee_card_last4"):
            receipt[key] = card_last4(receipt.get(key))
    return {
        "v": READING_VERSION,
        "model": reading.model,
        "is_receipt": reading.is_receipt,
        "fields": receipt,
        "error": reading.error,
    }


def _mirror(interaction: Interaction, receipt: Receipt) -> None:
    """``meta.receipt``: the checked reading, mirrored onto the interaction."""
    interaction.meta = {
        **(interaction.meta or {}),
        META_KEY: {
            "v": READING_VERSION,
            "id": receipt.id,
            "state": receipt.state,
            "direction": receipt.direction,
            "amount": str(receipt.amount) if receipt.amount is not None else None,
            "currency": receipt.currency.value if receipt.currency else None,
            "paid_at": receipt.paid_at.isoformat() if receipt.paid_at else None,
            "payer_name": receipt.payer_name,
            "payee_name": receipt.payee_name,
            "payer_card_last4": receipt.payer_card_last4,
            "payee_card_last4": receipt.payee_card_last4,
            "issuer": receipt.issuer,
            "reference": receipt.reference,
        },
    }


async def for_interaction(
    session: AsyncSession, interaction_id: int, *, for_update: bool = False
) -> Receipt | None:
    stmt = sa.select(Receipt).where(Receipt.interaction_id == interaction_id)
    if for_update:
        stmt = stmt.with_for_update()
    receipt: Receipt | None = await session.scalar(stmt)
    return receipt


def _apply(receipt: Receipt, parsed: ParsedReceipt, direction: str) -> None:
    receipt.direction = direction
    receipt.amount = parsed.amount
    receipt.currency = parsed.currency
    receipt.paid_at = parsed.paid_at
    receipt.payer_name = parsed.payer_name
    receipt.payee_name = parsed.payee_name
    receipt.payer_card_last4 = parsed.payer_card_last4
    receipt.payee_card_last4 = parsed.payee_card_last4
    receipt.issuer = parsed.issuer
    receipt.reference = parsed.reference


async def record(
    session: AsyncSession,
    interaction: Interaction,
    reading: VisionReading,
    *,
    presume_receipt: bool,
    now: datetime | None = None,
) -> Receipt | None:
    """Write the receipt one image reading found, or None when it is none.

    ``presume_receipt``: the owner sent this image to the bot, where images
    are receipts — a read that failed outright is still a receipt to look at
    in /tekshir. In a chat a failed read only flags the interaction, as it
    always has; a picture is not a receipt until the model says so.

    One row per interaction: a later read (``/process``) replaces a row that
    needed review and leaves one that was read alone.
    """
    if reading.ok and not reading.is_receipt:
        return None
    if not reading.ok and not presume_receipt:
        return None
    now = now or datetime.now(settings.tz)
    parsed = parse_fields(reading.receipt if reading.ok else None)
    state = STATE_READ if reading.ok and parsed.readable else STATE_NEEDS_REVIEW
    direction = await direction_of(session, parsed)

    receipt = await for_interaction(session, interaction.id, for_update=True)
    if receipt is not None and receipt.state == STATE_READ:
        return receipt
    entry = {"at": now.isoformat(), "field": "state", "by": BY_VISION}
    if receipt is None:
        receipt = Receipt(
            interaction_id=interaction.id,
            person_id=interaction.person_id,
            state=state,
            reading=_reading_json(reading),
            image_sha256=reading.image_sha256,
            history=[{**entry, "old": None, "new": state}],
        )
        session.add(receipt)
    else:
        receipt.history = [
            *(receipt.history or []),
            {**entry, "old": receipt.state, "new": state},
        ]
        receipt.state = state
        receipt.reading = _reading_json(reading)
        receipt.image_sha256 = reading.image_sha256 or receipt.image_sha256
    _apply(receipt, parsed, direction)
    await session.flush()
    if state == STATE_NEEDS_REVIEW:
        # Never dropped: the owner finds it in /tekshir.
        interaction.needs_review = True
    _mirror(interaction, receipt)
    log.info(
        "receipt %s read from interaction %s: %s",
        receipt.id,
        interaction.id,
        state,
        extra={"receipt_id": receipt.id, "interaction_id": interaction.id},
    )
    if state == STATE_READ:
        # The bank's SMS may already be here (JV-05): the card then says so.
        # A savepoint: a failed match must never cost the receipt just read;
        # the receipt_check sweep tries again.
        receipt_id = receipt.id
        try:
            async with session.begin_nested():
                await match_bank(session, now=now, receipt=receipt)
        except Exception as exc:
            log.error(
                "receipt %s not matched on arrival: %s",
                receipt_id,
                type(exc).__name__,
                extra={"receipt_id": receipt_id},
            )
    return receipt


def mark_notified(
    receipt: Receipt,
    *,
    now: datetime,
    message_id: int | None = None,
    verdict: str | None = None,
) -> None:
    """The card went out (or was edited). ``verdict`` is the one it showed:
    once that is the bank's or the owner's word, the card is current
    (``verdict_notified_at``) until the verdict changes again."""
    receipt.notified_at = receipt.notified_at or now
    if message_id is not None:
        receipt.notice_tg_message_id = message_id
    if verdict is not None and verdict == receipt.verdict and verdict != VERDICT_WAITING:
        receipt.verdict_notified_at = now


async def stamp_card(
    session: AsyncSession,
    receipt_id: int,
    *,
    now: datetime,
    verdict: str | None = None,
    message_id: int | None = None,
) -> None:
    """``mark_notified`` against the row as it is now, not as it was loaded:
    the card is current only while the database still holds the verdict it
    showed. A verdict another process changed in the meantime (a button
    tap, an SMS) keeps ``verdict_notified_at`` NULL, so the card is edited
    again on the next sweep."""
    moment = sa.literal(now, sa.DateTime(timezone=True))
    current = sa.and_(Receipt.verdict == verdict, Receipt.verdict != VERDICT_WAITING)
    values: dict[str, Any] = {
        "notified_at": sa.func.coalesce(Receipt.notified_at, moment)
    }
    if verdict is not None:
        values["verdict_notified_at"] = sa.case(
            (current, moment), else_=Receipt.verdict_notified_at
        )
    if message_id is not None:
        values["notice_tg_message_id"] = message_id
    await session.execute(
        sa.update(Receipt)
        .where(Receipt.id == receipt_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )


async def pending_notices(
    session: AsyncSession, *, now: datetime
) -> list[tuple[Receipt, Interaction]]:
    """Chat receipts whose card has not gone out, oldest first.

    Only what someone else sent, live: the owner's own outgoing receipts, an
    archive import and old history get no card. A photo sent to the bot got
    its card in the reply.
    """
    rows = await session.execute(
        sa.select(Receipt, Interaction)
        .join(Interaction, Interaction.id == Receipt.interaction_id)
        .where(
            Receipt.notified_at.is_(None),
            Interaction.source == InteractionSource.telegram_userbot,
            Interaction.direction == Direction.in_,
            Interaction.occurred_at >= now - NOTICE_MAX_AGE,
            sa.not_(sa.func.coalesce(Interaction.meta.has_key("archive"), False)),
        )
        .order_by(Receipt.created_at, Receipt.id)
        .limit(NOTICE_BATCH)
    )
    return [(receipt, interaction) for receipt, interaction in rows.tuples()]


# --- claims: the receipt's printed time (behaviour 4) ------------------------------


async def _candidates(session: AsyncSession, interaction: Interaction) -> list[Receipt]:
    """The receipts behind what this interaction's extraction says.

    A window's synthetic interaction speaks for its member messages; any
    other interaction for itself.
    """
    is_window = (interaction.meta or {}).get("kind") == "window"
    stmt = sa.select(Receipt).where(
        Receipt.state == STATE_READ, Receipt.paid_at.is_not(None)
    )
    if is_window and interaction.window_id is not None:
        stmt = stmt.join(Interaction, Interaction.id == Receipt.interaction_id).where(
            Interaction.window_id == interaction.window_id
        )
    else:
        stmt = stmt.where(Receipt.interaction_id == interaction.id)
    return list(await session.scalars(stmt.order_by(Receipt.id)))


async def backing(
    session: AsyncSession, interaction: Interaction, item: BaseModel
) -> Receipt | None:
    """The one receipt an extracted money item speaks of: the same amount in
    the same currency, read from this interaction or its window. Two such
    receipts, or none, is no answer — never a guess."""
    raw_amount = getattr(item, "amount", None)
    if raw_amount is None:
        return None
    amount = to_money(raw_amount)
    currency = getattr(item, "currency", None)
    if amount is None or currency is None:
        return None
    found = [
        r
        for r in await _candidates(session, interaction)
        if r.amount == amount and r.currency is not None and r.currency.value == currency
    ]
    return found[0] if len(found) == 1 else None


# --- the bank cross-check (JV-05) ---------------------------------------------------

VERDICT_CONFIRMED = "confirmed"
VERDICT_UNCONFIRMED = "unconfirmed"
VERDICT_SUSPICIOUS = "suspicious"
VERDICT_OWNER_OK = "owner_ok"
VERDICT_OWNER_FAKE = "owner_fake"
# What the bank may still overturn: an SMS that arrives late confirms an
# unconfirmed or a suspicious receipt. The owner's own verdict is final.
OPEN_VERDICTS = (VERDICT_WAITING, VERDICT_UNCONFIRMED, VERDICT_SUSPICIOUS)
OWNER_VERDICTS = (VERDICT_OWNER_OK, VERDICT_OWNER_FAKE)

DUPLICATE_IMAGE = "duplicate_image"
DUPLICATE_REFERENCE = "duplicate_reference"
STALE_DATE = "stale_date"
FUTURE_DATE = "future_date"
OWN_CARD_NO_BANK = "own_card_no_bank"
# verdict_reasons are stored in this order, whatever order they were found in.
_REASON_ORDER = (
    DUPLICATE_IMAGE,
    DUPLICATE_REFERENCE,
    STALE_DATE,
    FUTURE_DATE,
    OWN_CARD_NO_BANK,
)

# history ``by`` values (String(16)).
BY_BANK = "bank"
BY_CHECK = "check"
BY_BUTTON = "button"

# Images the owner sent the bot themselves: a client's receipt they forward
# there is the same receipt, not the client sending it again.
BOT_SOURCES = (InteractionSource.receipt_photo, InteractionSource.assistant_bot)
# A printed reference shorter than this ("1", "07") is too common to prove
# that a receipt was sent before.
MIN_REFERENCE_CHARS = 6
CHECK_BATCH = 50
CARD_BATCH = 20
# The phone app's heartbeats (the api writes them): any is the phone reporting.
PHONE_BEATS = ("phone", "phone_seen", "phone_app")
# interactions.meta key: the owner's own words in this bot message booked the
# payment, so the card leaves out the "booked only from the bank" footer.
BOOKED_KEY = "receipt_booked"
# interactions.meta key: this bot message's receipt card is a message of its
# own, so the bank verdict edits it in place. A card sent before JV-05 shared
# its message with the bot's reply and that reply's buttons: it is never
# edited, a fresh card goes out instead.
CARD_ALONE_KEY = "receipt_card_alone"


def _window() -> timedelta:
    return timedelta(minutes=settings.receipt_match_minutes)


def _stale() -> timedelta:
    return timedelta(hours=settings.receipt_stale_hours)


def date_reasons(paid_at: datetime | None, sent_at: datetime) -> list[str]:
    """``stale_date`` / ``future_date``: the printed time against the moment
    the receipt was sent. A clock a little ahead (within the match window)
    is a phone's clock, not a receipt from the future."""
    if paid_at is None:
        return []
    if paid_at < sent_at - _stale():
        return [STALE_DATE]
    if paid_at > sent_at + _window():
        return [FUTURE_DATE]
    return []


def _ordered(reasons: set[str]) -> list[str]:
    return [reason for reason in _REASON_ORDER if reason in reasons]


def _set_verdict(
    receipt: Receipt,
    verdict: str,
    reasons: list[str],
    *,
    by: str,
    now: datetime,
    **extra: Any,
) -> bool:
    """Change the verdict with a history entry; False when nothing changed.

    A new verdict makes the card out of date: ``verdict_notified_at`` is
    cleared, so the card is edited to show it."""
    if receipt.verdict == verdict and list(receipt.verdict_reasons or []) == reasons:
        return False
    receipt.history = [
        *(receipt.history or []),
        {
            "at": now.isoformat(),
            "field": "verdict",
            "old": receipt.verdict,
            "new": verdict,
            "by": by,
            **({"reasons": reasons} if reasons else {}),
            **extra,
        },
    ]
    receipt.verdict = verdict
    receipt.verdict_reasons = reasons
    receipt.verdict_notified_at = None
    return True


def _nearest(column: Any, moment: Any) -> Any:
    return sa.func.abs(sa.extract("epoch", column - moment))


def _bank_income(txn: Transaction) -> bool:
    """A bank's own record of money in: an SMS or app push (``channel``),
    neither voided nor an own-card transfer (queries.ACTIVE_TXN)."""
    return (
        txn.type is TransactionType.income
        and txn.channel is not None
        and txn.voided_at is None
        and not txn.is_internal
    )


async def _bank_row(
    session: AsyncSession, receipt: Receipt, *, only: Transaction | None = None
) -> Transaction | None:
    """The bank income that confirms this receipt: the exact amount in the
    same currency (Decimal against NUMERIC, never converted), within
    RECEIPT_MATCH_MINUTES of the printed time — not of the message that
    carried it — on the printed payee card when both name one, and not
    already confirming another receipt."""
    assert receipt.paid_at is not None and receipt.amount is not None
    other = aliased(Receipt)
    card = receipt.payee_card_last4
    stmt = (
        sa.select(Transaction)
        .where(
            queries.ACTIVE_TXN,
            Transaction.type == TransactionType.income,
            Transaction.channel.is_not(None),
            Transaction.currency == receipt.currency,
            Transaction.amount == receipt.amount,
            Transaction.occurred_at.between(
                receipt.paid_at - _window(), receipt.paid_at + _window()
            ),
            sa.or_(
                Transaction.card_last4.is_(None),
                sa.true() if card is None else Transaction.card_last4 == card,
            ),
            ~sa.exists().where(
                other.transaction_id == Transaction.id, other.id != receipt.id
            ),
        )
        .order_by(_nearest(Transaction.occurred_at, receipt.paid_at), Transaction.id)
        .limit(1)
    )
    if only is not None:
        stmt = stmt.where(Transaction.id == only.id)
    txn: Transaction | None = await session.scalar(stmt)
    return txn


async def _link(
    session: AsyncSession, receipt: Receipt, txn: Transaction, *, now: datetime
) -> bool:
    """Point the receipt at its bank row. ux_receipts_transaction keeps one
    receipt per bank row: when another receipt took it first (a race), this
    one stays as it was and the next sweep looks again."""
    try:
        async with session.begin_nested():
            result = await session.execute(
                sa.update(Receipt)
                .where(Receipt.id == receipt.id, Receipt.transaction_id.is_(None))
                .values(transaction_id=txn.id)
                .execution_options(synchronize_session=False)
            )
    except IntegrityError:
        log.info(
            "bank row %s already confirms another receipt",
            txn.id,
            extra={"receipt_id": receipt.id, "transaction_id": txn.id},
        )
        return False
    if not getattr(result, "rowcount", 0):
        return False
    receipt.transaction_id = txn.id
    _set_verdict(
        receipt, VERDICT_CONFIRMED, [], by=BY_BANK, now=now, transaction_id=txn.id
    )
    await session.flush()
    log.info(
        "receipt %s confirmed by bank row %s",
        receipt.id,
        txn.id,
        extra={"receipt_id": receipt.id, "transaction_id": txn.id},
    )
    return True


async def match_bank(
    session: AsyncSession,
    *,
    now: datetime,
    txn: Transaction | None = None,
    receipt: Receipt | None = None,
) -> list[Receipt]:
    """Confirm receipts by the bank's own income records; the receipts that
    turned ``confirmed``.

    ``txn``: a bank row just booked (``money_events.apply_reading``) — the
    one receipt it confirms, if any. ``receipt``: a receipt just read. With
    neither, every receipt the bank may still confirm that arrived within
    RECEIPT_STALE_HOURS (``questions.auto_resolve`` and ``receipt_check``).

    Only a readable receipt of money to the owner (or of unknown direction)
    with a printed time is matched; an old receipt sent again, or one dated
    ahead of its message, is never confirmed by whatever bank row happens to
    sit near its printed time. Without ``txn``, a receipt whose bank row
    another read of it already holds is confirmed by that copy
    (``linked_copy``). A receipt row another transaction is working on is
    skipped, never waited for; one that fails is logged and left for the
    next sweep. Nothing here writes money.
    """
    if txn is not None and not _bank_income(txn):
        return []
    sent_at = Interaction.occurred_at
    stmt = (
        sa.select(Receipt)
        .join(Interaction, Interaction.id == Receipt.interaction_id)
        .where(
            Receipt.state == STATE_READ,
            Receipt.verdict.in_(OPEN_VERDICTS),
            Receipt.transaction_id.is_(None),
            Receipt.direction != OUT,
            Receipt.amount.is_not(None),
            Receipt.paid_at.is_not(None),
            Receipt.paid_at >= sent_at - _stale(),
            Receipt.paid_at <= sent_at + _window(),
        )
        .with_for_update(of=Receipt, skip_locked=True)
    )
    if receipt is not None:
        stmt = stmt.where(Receipt.id == receipt.id)
    if txn is not None:
        stmt = stmt.where(
            Receipt.currency == txn.currency,
            Receipt.amount == txn.amount,
            Receipt.paid_at.between(
                txn.occurred_at - _window(), txn.occurred_at + _window()
            ),
        ).order_by(_nearest(Receipt.paid_at, txn.occurred_at), Receipt.id)
    else:
        if receipt is None:
            stmt = stmt.where(Receipt.created_at >= now - _stale())
        stmt = stmt.order_by(Receipt.created_at, Receipt.id)
    confirmed: list[Receipt] = []
    for row in list(await session.scalars(stmt)):
        row_id = row.id
        try:
            async with session.begin_nested():
                bank = await _bank_row(session, row, only=txn)
                if bank is not None:
                    linked = await _link(session, row, bank, now=now)
                elif txn is None:
                    linked = await _confirm_by_copy(session, row, now=now)
                else:
                    linked = False
        except Exception as exc:
            log.error(
                "receipt %s not matched: %s",
                row_id,
                type(exc).__name__,
                extra={"receipt_id": row_id},
            )
            continue
        if not linked:
            continue
        confirmed.append(row)
        if txn is not None:
            break  # one bank row confirms one receipt
    return confirmed


async def linked_copy(
    session: AsyncSession, receipt: Receipt, interaction: Interaction | None = None
) -> Receipt | None:
    """Another read of this same receipt the bank already confirmed: the
    same image bytes, or the same issuer and printed reference, of the same
    amount and currency, linked to a bank row.

    The bank row confirms one receipt only (ux_receipts_transaction), so a
    copy can never take it again — but it is the same payment. A copy counts
    when either side is the owner's own image in the bot (a client's receipt
    forwarded there, or a ``/process`` re-read); a client sending a
    confirmed receipt into a chat again is a resend, never a confirmation.
    """
    if receipt.amount is None or receipt.currency is None:
        return None
    interaction = interaction or await session.get(Interaction, receipt.interaction_id)
    if interaction is None:
        return None
    other = aliased(Receipt)
    other_message = aliased(Interaction)
    same: list[Any] = []
    if receipt.image_sha256:
        same.append(other.image_sha256 == receipt.image_sha256)
    if receipt.reference and len(receipt.reference) >= MIN_REFERENCE_CHARS:
        same.append(
            sa.and_(
                other.reference == receipt.reference,
                other.issuer.is_not_distinct_from(receipt.issuer),
            )
        )
    if not same:
        return None
    stmt = (
        sa.select(other)
        .join(other_message, other_message.id == other.interaction_id)
        .where(
            sa.or_(*same),
            other.id != receipt.id,
            other.state == STATE_READ,
            other.transaction_id.is_not(None),
            other.amount == receipt.amount,
            other.currency == receipt.currency,
        )
        .order_by(other.id)
        .limit(1)
    )
    if interaction.source not in BOT_SOURCES:
        stmt = stmt.where(other_message.source.in_(BOT_SOURCES))
    copy: Receipt | None = await session.scalar(stmt)
    return copy


async def _confirm_by_copy(
    session: AsyncSession,
    receipt: Receipt,
    *,
    now: datetime,
    interaction: Interaction | None = None,
) -> bool:
    """Confirm a receipt by its ``linked_copy``. The receipt keeps
    ``transaction_id`` NULL (one receipt per bank row); the history entry
    names the bank row and the copy."""
    copy = await linked_copy(session, receipt, interaction)
    if copy is None:
        return False
    _set_verdict(
        receipt,
        VERDICT_CONFIRMED,
        [],
        by=BY_BANK,
        now=now,
        transaction_id=copy.transaction_id,
        copy_of=copy.id,
    )
    await session.flush()
    log.info(
        "receipt %s confirmed as a copy of receipt %s",
        receipt.id,
        copy.id,
        extra={
            "receipt_id": receipt.id,
            "copy_of": copy.id,
            "transaction_id": copy.transaction_id,
        },
    )
    return True


def confirmed_by(receipt: Receipt) -> int | None:
    """The bank row behind a confirmed receipt: its own link, or the one its
    copy holds (from the verdict's history entry)."""
    if receipt.transaction_id is not None:
        return receipt.transaction_id
    for entry in reversed(receipt.history or []):
        if entry.get("field") == "verdict" and entry.get("new") == VERDICT_CONFIRMED:
            found = entry.get("transaction_id")
            return found if isinstance(found, int) else None
    return None


@dataclass(slots=True, frozen=True)
class EarlierCopy:
    """The same receipt sent before: which reason, and when it was sent."""

    reason: str
    sent_at: datetime


async def earlier_copy(session: AsyncSession, receipt: Receipt) -> EarlierCopy | None:
    """The first earlier receipt with the same image bytes, or the same
    issuer and printed reference — the same receipt sent again.

    Only copies from the same side count: a client's receipt the owner
    forwards to the bot is that receipt, not a resend, so a bot image is
    compared with bot images and a chat image with chat images. Only a copy
    that was read, from a message sent strictly earlier, counts: a failed
    read the owner retried (``/process`` carries the original message's
    time) is the same message, not the receipt sent again."""
    interaction = await session.get(Interaction, receipt.interaction_id)
    if interaction is None:
        return None
    other = aliased(Receipt)
    other_message = aliased(Interaction)
    from_bot = interaction.source in BOT_SOURCES
    side = (
        other_message.source.in_(BOT_SOURCES)
        if from_bot
        else other_message.source.not_in(BOT_SOURCES)
    )
    checks: list[tuple[str, Any]] = []
    if receipt.image_sha256:
        checks.append((DUPLICATE_IMAGE, other.image_sha256 == receipt.image_sha256))
    if receipt.reference and len(receipt.reference) >= MIN_REFERENCE_CHARS:
        checks.append(
            (
                DUPLICATE_REFERENCE,
                sa.and_(
                    other.reference == receipt.reference,
                    other.issuer.is_not_distinct_from(receipt.issuer),
                ),
            )
        )
    for reason, same in checks:
        sent_at: datetime | None = await session.scalar(
            sa.select(other_message.occurred_at)
            .select_from(other)
            .join(other_message, other_message.id == other.interaction_id)
            .where(
                same,
                side,
                other.id != receipt.id,
                other.state == STATE_READ,
                other_message.occurred_at < sa.literal(interaction.occurred_at),
            )
            .order_by(other_message.occurred_at, other.id)
            .limit(1)
        )
        if sent_at is not None:
            return EarlierCopy(reason, sent_at)
    return None


async def phone_reporting(session: AsyncSession, *, since: datetime) -> bool:
    """Has the phone app talked to the server since ``since``? Then a bank
    SMS or Payme push from before that moment would be here by now."""
    last: datetime | None = await session.scalar(
        sa.select(sa.func.max(Heartbeat.last_seen_at)).where(
            Heartbeat.component.in_(PHONE_BEATS)
        )
    )
    return last is not None and last >= since


def arrived_live(interaction: Interaction, receipt: Receipt) -> bool:
    """Did the receipt reach MIYA when it was sent? An archive import or a
    message read days late is history: the phone could never have delivered
    the bank's SMS of back then since the receipt came."""
    if (interaction.meta or {}).get("archive"):
        return False
    return interaction.occurred_at >= receipt.created_at - NOTICE_MAX_AGE


async def suspicion_reasons(
    session: AsyncSession,
    receipt: Receipt,
    *,
    interaction: Interaction,
    now: datetime,
) -> list[str]:
    """Why an unconfirmed receipt is suspicious — empty when it is merely
    unconfirmed.

    Sent before (the same image or reference), printed long before its
    message or after it, or paid to one of the owner's cards
    (``money_events.own_cards``) while the phone kept reporting and no bank
    record came since the receipt arrived live. An account whose SMS never
    reaches the phone, a silent phone or an imported receipt is never a
    reason: that receipt is only unconfirmed. Neither is a receipt of the
    owner's own payment (``out``): the flags guard money said to have reached
    the owner, and the bank's income never confirms one."""
    if receipt.direction == OUT:
        return []
    reasons: set[str] = set()
    copy = await earlier_copy(session, receipt)
    if copy is not None:
        reasons.add(copy.reason)
    reasons.update(date_reasons(receipt.paid_at, interaction.occurred_at))
    card = receipt.payee_card_last4
    if (
        not reasons
        and card
        and arrived_live(interaction, receipt)
        and card in await money_events.own_cards(session)
    ):
        since = receipt.created_at
        if receipt.paid_at is not None:
            since = max(since, receipt.paid_at + _window())
        if await phone_reporting(session, since=min(since, now)):
            reasons.add(OWN_CARD_NO_BANK)
    return _ordered(reasons)


async def check_waiting(session: AsyncSession, *, now: datetime) -> list[Receipt]:
    """The verdict for receipts the bank has not confirmed within
    RECEIPT_BANK_WAIT_MINUTES of their arrival: ``confirmed`` when another
    read of the same receipt holds the bank row (``linked_copy``), else
    ``suspicious`` for a reason (``suspicion_reasons``), otherwise
    ``unconfirmed`` — never "fake".

    Every readable receipt gets a verdict, an outgoing one too (it is only
    ever ``unconfirmed``); one that fails is logged and left for the next
    sweep."""
    cutoff = now - timedelta(minutes=settings.receipt_bank_wait_minutes)
    rows = await session.execute(
        sa.select(Receipt, Interaction)
        .join(Interaction, Interaction.id == Receipt.interaction_id)
        .where(
            Receipt.verdict == VERDICT_WAITING,
            Receipt.state == STATE_READ,
            Receipt.created_at <= cutoff,
        )
        .order_by(Receipt.created_at, Receipt.id)
        .limit(CHECK_BATCH)
        .with_for_update(of=Receipt, skip_locked=True)
    )
    decided: list[Receipt] = []
    for receipt, interaction in list(rows.tuples()):
        receipt_id = receipt.id
        try:
            async with session.begin_nested():
                if not await _confirm_by_copy(
                    session, receipt, now=now, interaction=interaction
                ):
                    reasons = await suspicion_reasons(
                        session, receipt, interaction=interaction, now=now
                    )
                    verdict = VERDICT_SUSPICIOUS if reasons else VERDICT_UNCONFIRMED
                    _set_verdict(receipt, verdict, reasons, by=BY_CHECK, now=now)
                    await session.flush()
        except Exception as exc:
            log.error(
                "receipt %s could not be judged: %s",
                receipt_id,
                type(exc).__name__,
                extra={"receipt_id": receipt_id},
            )
            continue
        decided.append(receipt)
    return decided


def owner_can_judge(receipt: Receipt) -> bool:
    """Does the card carry ✅ Pul keldi / 🚩 Soxta? A readable receipt of
    money to the owner (or of unknown direction) the bank has not
    confirmed and the owner has not judged."""
    return (
        receipt.state == STATE_READ
        and receipt.direction != OUT
        and receipt.verdict in OPEN_VERDICTS
    )


async def set_owner_verdict(
    session: AsyncSession,
    receipt_id: int,
    *,
    paid: bool,
    by: str = BY_BUTTON,
    now: datetime | None = None,
) -> tuple[Receipt | None, bool]:
    """The owner's ✅ Pul keldi (``paid``) or 🚩 Soxta, under a row lock.

    Returns (the receipt, whether it changed). The same tap twice changes
    nothing and writes no second history entry. Neither books money nor
    voids any: the ledger keeps only what the bank and the owner's own words
    wrote. A receipt that could not be read has no verdict to set."""
    now = now or datetime.now(settings.tz)
    receipt: Receipt | None = await session.scalar(
        sa.select(Receipt)
        .where(Receipt.id == receipt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if receipt is None or receipt.state != STATE_READ:
        return receipt, False
    verdict = VERDICT_OWNER_OK if paid else VERDICT_OWNER_FAKE
    changed = _set_verdict(receipt, verdict, [], by=by, now=now)
    await session.flush()
    return receipt, changed


# --- the card, rendered again for an edit (JV-05) ----------------------------------


@dataclass(slots=True, frozen=True)
class CardSource:
    """Who the card names as the sender, and where it came from."""

    from_bot: bool
    sender: str | None = None
    # A group's title; None for a private chat.
    chat_title: str | None = None
    # The "booked only from the bank" footer; dropped where the owner's own
    # words in the same bot message already booked the payment.
    footer: bool = True


@dataclass(slots=True, frozen=True)
class VerdictDetail:
    """What the verdict line names: the bank's channel and time for a
    confirmed receipt, when an earlier copy was sent for a resend."""

    bank_label: str | None = None
    bank_at: datetime | None = None
    earlier_at: datetime | None = None


def card_editable(interaction: Interaction) -> bool:
    """May the card's message be edited in place? A chat receipt's card was
    always a message of its own; a bot card only once ``CARD_ALONE_KEY``
    says so."""
    if interaction.source not in BOT_SOURCES:
        return True
    return bool((interaction.meta or {}).get(CARD_ALONE_KEY))


def mark_card_alone(interaction: Interaction) -> None:
    interaction.meta = {**(interaction.meta or {}), CARD_ALONE_KEY: True}


async def card_source(
    session: AsyncSession, receipt: Receipt, interaction: Interaction | None = None
) -> CardSource:
    interaction = interaction or await session.get(Interaction, receipt.interaction_id)
    if interaction is None or interaction.source in BOT_SOURCES:
        meta = (interaction.meta if interaction is not None else None) or {}
        return CardSource(from_bot=True, footer=not meta.get(BOOKED_KEY))
    person_id = receipt.person_id or interaction.person_id
    person = await session.get(Person, person_id) if person_id else None
    monitor = (
        await session.scalar(
            sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == interaction.tg_chat_id)
        )
        if interaction.tg_chat_id is not None
        else None
    )
    group = monitor is not None and monitor.chat_type is not ChatType.private
    title = monitor.title if group and monitor is not None and monitor.title else None
    if person is not None:
        sender: str | None = person.display_name
    else:
        sender = monitor.title if monitor is not None else None
    return CardSource(from_bot=False, sender=sender, chat_title=title)


async def verdict_detail(session: AsyncSession, receipt: Receipt) -> VerdictDetail:
    txn_id = confirmed_by(receipt) if receipt.verdict == VERDICT_CONFIRMED else None
    if txn_id is not None:
        txn = await session.get(Transaction, txn_id)
        if txn is None:
            return VerdictDetail()
        source = (
            await session.get(Interaction, txn.source_interaction_id)
            if txn.source_interaction_id is not None
            else None
        )
        label = (
            money_events.channel_label(source, txn.channel)
            if source is not None and txn.channel
            else (txn.channel or "").partition(":")[2] or None
        )
        return VerdictDetail(bank_label=label, bank_at=txn.occurred_at)
    reasons = set(receipt.verdict_reasons or [])
    if receipt.verdict == VERDICT_SUSPICIOUS and reasons & {
        DUPLICATE_IMAGE,
        DUPLICATE_REFERENCE,
    }:
        copy = await earlier_copy(session, receipt)
        return VerdictDetail(earlier_at=copy.sent_at if copy else None)
    return VerdictDetail()


async def cards_to_refresh(session: AsyncSession) -> list[Receipt]:
    """Receipts whose card went out showing an older verdict than they now
    have: the card is edited (or, without a message id, sent again)."""
    return list(
        await session.scalars(
            sa.select(Receipt)
            .where(
                Receipt.notified_at.is_not(None),
                Receipt.verdict != VERDICT_WAITING,
                Receipt.verdict_notified_at.is_(None),
            )
            .order_by(Receipt.updated_at, Receipt.id)
            .limit(CARD_BATCH)
        )
    )
