"""Receipts read from images (JV-04).

A payment screenshot or a terminal check — sent to the bot, or into a chat
the userbot reads — is read once by ``vision.read_image``; this module turns
the model's answer into a checked ``receipts`` row and mirrors it onto the
interaction (``meta.receipt``).

What a receipt says is the image's word, never the bank's record, so nothing
here writes the ledger. Money reaches the books the way it always has: the
owner's own words through extraction, a counterparty's words through the
claims gate (``claims.create`` carries the receipt's printed time as
``receipt_at``), and the bank's SMS or app push through ``money_events``. The
bank cross-check of a receipt is ``verdict`` (JV-05); until then it waits.

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
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Currency, Direction, InteractionSource
from miya.db.models import Interaction, Receipt
from miya.services import money_events
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
    return receipt


def mark_notified(
    receipt: Receipt, *, now: datetime, message_id: int | None = None
) -> None:
    receipt.notified_at = now
    if message_id is not None:
        receipt.notice_tg_message_id = message_id


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
