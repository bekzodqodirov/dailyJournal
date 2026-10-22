"""JV-05: the bank cross-check of receipts read from images.

A receipt is the image's word. ``receipts.match_bank`` confirms it by the
bank's own income record (an SMS or Payme push) of the same amount and
currency near the time printed on it; ``receipts.check_waiting`` gives a
receipt without one, after RECEIPT_BANK_WAIT_MINUTES, ``unconfirmed`` — or
``suspicious`` for a reason it names. The card is edited in place, and the
owner's ✅ Pul keldi / 🚩 Soxta are final. No verdict ever writes money.
Real PostgreSQL; Telegram replaced by stand-ins.
"""

# ruff: noqa: F811 - the shared fixtures are imported, then requested by name.

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, replies
from miya.bot.formatting import clock, day_label
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    Direction,
    InteractionSource,
    TransactionType,
)
from miya.services import health, questions, receipts
from miya.tools import doctor
from miya.worker import main as worker
from tests.test_close_and_correct import bound  # noqa: F401
from tests.test_receipts_read import (
    CHAT,
    INCOME_SMS,
    NOW,
    _Bot,
    _chat_photo,
    _fields,
    _reading,
    _sms,
)

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]


def _later(hours: float = 2) -> datetime:
    """A sweep this long after the receipts arrived (``created_at`` is the
    database's clock, so it is measured from the real now)."""
    return datetime.now(TZ) + timedelta(hours=hours)


async def _receipt(
    session,
    *,
    sent_at: datetime = NOW,
    paid_at: datetime | None = None,
    person=None,
    image: str = "a" * 64,
    **fields,
) -> m.Receipt:
    """A receipt someone sent into a chat at ``sent_at``, printed at
    ``paid_at`` (an hour earlier by default)."""
    paid_at = paid_at or sent_at - timedelta(hours=1)
    row, _ = await _chat_photo(session, person=person, at=sent_at)
    receipt = await receipts.record(
        session,
        row,
        _reading(
            receipt=_fields(paid_at=paid_at.strftime("%Y-%m-%dT%H:%M"), **fields),
            image_sha256=image,
        ),
        presume_receipt=False,
    )
    assert receipt is not None
    return receipt


async def _bank(
    session,
    at: datetime,
    *,
    amount: str = "1250000",
    currency: Currency = Currency.UZS,
    txn_type: TransactionType = TransactionType.income,
    channel: str | None = "sms:payme",
    card: str | None = "1234",
    **extra,
) -> m.Transaction:
    txn = m.Transaction(
        type=txn_type,
        amount=Decimal(amount),
        currency=currency,
        occurred_at=at,
        channel=channel,
        card_last4=card,
        **extra,
    )
    session.add(txn)
    await session.flush()
    return txn


async def _txn_count(session) -> int:
    return await session.scalar(sa.select(sa.func.count(m.Transaction.id)))


def _verdict_entries(receipt: m.Receipt) -> list[dict]:
    return [e for e in receipt.history if e.get("field") == "verdict"]


# --- matching -----------------------------------------------------------------------


async def test_match_by_paid_at_not_message_time(session):
    """The client paid five hours before they sent the screenshot: the
    bank's SMS from the moment of payment confirms it; one that came in
    when the message did is another payment."""
    paid = NOW - timedelta(hours=5)
    receipt = await _receipt(session, sent_at=NOW, paid_at=paid)

    await _sms(session, NOW - timedelta(minutes=1), INCOME_SMS)
    await session.refresh(receipt)
    assert receipt.verdict == "waiting" and receipt.transaction_id is None

    await _sms(session, paid + timedelta(minutes=3), INCOME_SMS)
    await session.refresh(receipt)
    bank = await session.scalar(
        sa.select(m.Transaction).where(
            m.Transaction.occurred_at == paid + timedelta(minutes=3)
        )
    )
    assert receipt.verdict == "confirmed"
    assert receipt.transaction_id == bank.id
    assert receipt.verdict_reasons == []
    [entry] = _verdict_entries(receipt)
    assert (entry["old"], entry["new"], entry["by"]) == ("waiting", "confirmed", "bank")
    assert entry["transaction_id"] == bank.id
    # The verdict wrote no money: only the two bank rows exist.
    assert await _txn_count(session) == 2


async def test_old_resent_receipt_not_matched(session):
    """A receipt the bank confirmed five days ago, sent again today, never
    takes that bank row — nor any other near its old printed time."""
    paid = NOW - timedelta(days=5)
    first = await _receipt(session, sent_at=paid + timedelta(minutes=5), paid_at=paid)
    await _sms(session, paid + timedelta(minutes=1), INCOME_SMS)
    await session.refresh(first)
    assert first.verdict == "confirmed"

    # Another bank row of the same amount at the old time is not linked to
    # anything: an old receipt is still not confirmed by it.
    await _bank(session, paid + timedelta(minutes=2), card=None)
    again = await _receipt(session, sent_at=NOW, paid_at=paid, person=None)
    await receipts.match_bank(session, now=_later(0))
    assert again.transaction_id is None and again.verdict == "waiting"

    [judged] = await receipts.check_waiting(session, now=_later())
    assert judged.id == again.id
    assert again.verdict == "suspicious"
    assert again.verdict_reasons == ["duplicate_image", "stale_date"]
    detail = await receipts.verdict_detail(session, again)
    line = replies.receipt_verdict_line(again, detail)
    assert line == (
        "⚠️ Chek shubhali: bu chek avval ham yuborilgan "
        f"({day_label(paid + timedelta(minutes=5))}), chekdagi sana eski "
        f"({day_label(paid)}). Pul kartangizga tushganini ilovadan tekshiring."
    )


async def test_duplicate_image_flagged(session):
    sent = NOW - timedelta(hours=3)
    first = await _receipt(session, sent_at=sent)
    second = await _receipt(session, sent_at=sent + timedelta(minutes=30))

    assert await receipts.check_waiting(session, now=_later()) == [first, second]

    assert (first.verdict, first.verdict_reasons) == ("unconfirmed", [])
    assert second.verdict == "suspicious"
    assert second.verdict_reasons == ["duplicate_image"]
    [entry] = _verdict_entries(second)
    assert entry["by"] == "check" and entry["reasons"] == ["duplicate_image"]
    detail = await receipts.verdict_detail(session, second)
    assert detail.earlier_at == sent
    assert replies.receipt_verdict_line(second, detail).startswith(
        f"⚠️ Chek shubhali: bu chek avval ham yuborilgan ({day_label(sent)})."
    )


async def test_duplicate_reference_flagged_and_short_ones_ignored(session):
    sent = NOW - timedelta(hours=3)
    await _receipt(session, sent_at=sent, image="b" * 64)
    resent = await _receipt(session, sent_at=sent + timedelta(minutes=5), image="c" * 64)
    await _receipt(session, sent_at=sent, image="d" * 64, reference="77")
    short = await _receipt(
        session, sent_at=sent + timedelta(minutes=5), image="e" * 64, reference="77"
    )
    await receipts.check_waiting(session, now=_later())
    assert resent.verdict_reasons == ["duplicate_reference"]
    assert (short.verdict, short.verdict_reasons) == ("unconfirmed", [])


async def test_owner_forward_to_the_bot_is_not_a_resend(session):
    """The owner forwarding a client's receipt to the bot is that receipt."""
    sent = NOW - timedelta(hours=3)
    await _receipt(session, sent_at=sent)
    forward = m.Interaction(
        source=InteractionSource.receipt_photo,
        direction=Direction.in_,
        occurred_at=sent + timedelta(minutes=10),
    )
    session.add(forward)
    await session.flush()
    copy = await receipts.record(
        session,
        forward,
        _reading(
            receipt=_fields(
                paid_at=(sent - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
            )
        ),
        presume_receipt=True,
    )
    await receipts.check_waiting(session, now=_later())
    assert (copy.verdict, copy.verdict_reasons) == ("unconfirmed", [])


@pytest.mark.parametrize(
    ("paid_offset", "reason"),
    [(timedelta(hours=-80), "stale_date"), (timedelta(hours=3), "future_date")],
)
async def test_a_printed_date_far_from_the_message_is_suspicious(
    session, paid_offset, reason
):
    sent = NOW - timedelta(hours=4)
    receipt = await _receipt(session, sent_at=sent, paid_at=sent + paid_offset)
    await receipts.check_waiting(session, now=_later())
    assert (receipt.verdict, receipt.verdict_reasons) == ("suspicious", [reason])
    if reason == "future_date":
        assert replies.receipt_verdict_line(receipt) == (
            "⚠️ Chek shubhali: chekdagi sana kelajakda. Pul kartangizga tushganini "
            "ilovadan tekshiring."
        )


async def test_only_an_active_bank_income_in_the_same_currency_confirms(session):
    """Exact Decimal amount and currency; never a typed row, a voided one,
    an own-card transfer, an expense, another card or another currency."""
    receipt = await _receipt(session, sent_at=NOW, paid_at=NOW - timedelta(hours=1))
    at = NOW - timedelta(minutes=50)
    await _bank(session, at, currency=Currency.USD)
    await _bank(session, at, amount="1250000.01")
    await _bank(session, at, channel=None)
    await _bank(session, at, voided_at=NOW, void_reason="test")
    await _bank(session, at, is_internal=True)
    await _bank(session, at, txn_type=TransactionType.expense)
    await _bank(session, at, card="9999")
    await _bank(session, NOW - timedelta(minutes=20))  # 40 min off the print
    assert await receipts.match_bank(session, now=_later(0)) == []
    assert receipt.transaction_id is None

    good = await _bank(session, at)
    assert await receipts.match_bank(session, now=_later(0)) == [receipt]
    assert receipt.transaction_id == good.id
    # A bank row confirms one receipt only.
    twin = await _receipt(
        session, sent_at=NOW, paid_at=NOW - timedelta(hours=1), image="f" * 64
    )
    assert await receipts.match_bank(session, now=_later(0)) == []
    assert twin.transaction_id is None
    assert await _txn_count(session) == 9


async def test_a_bank_record_already_here_confirms_the_receipt_on_arrival(session):
    bank = await _bank(session, NOW - timedelta(minutes=58))
    receipt = await _receipt(session, sent_at=NOW, paid_at=NOW - timedelta(hours=1))
    assert (receipt.verdict, receipt.transaction_id) == ("confirmed", bank.id)


async def test_a_late_sms_still_confirms_but_never_overrules_the_owner(session):
    late = await _receipt(session, sent_at=NOW - timedelta(hours=3))
    judged = await _receipt(
        session, sent_at=NOW - timedelta(hours=3), image="g" * 64, amount=900000
    )
    await receipts.check_waiting(session, now=_later())
    assert late.verdict == "unconfirmed"
    await receipts.set_owner_verdict(session, judged.id, paid=False)

    await _bank(session, NOW - timedelta(hours=4, minutes=-2))
    await _bank(session, NOW - timedelta(hours=4, minutes=-2), amount="900000")
    resolved = await questions.auto_resolve(session, now=_later())
    assert resolved["receipts_bank"] == 1
    assert late.verdict == "confirmed"
    assert judged.verdict == "owner_fake" and judged.transaction_id is None


# --- the verdict after the wait ---------------------------------------------------


async def test_unmatched_without_own_card_is_unconfirmed(session):
    await health.beat(session, "phone_seen", now=_later(0) + timedelta(minutes=1))
    receipt = await _receipt(session, sent_at=NOW - timedelta(minutes=30))

    # Not before RECEIPT_BANK_WAIT_MINUTES have passed.
    assert await receipts.check_waiting(session, now=_later(0)) == []
    assert receipt.verdict == "waiting"

    assert await receipts.check_waiting(session, now=_later()) == [receipt]
    assert (receipt.verdict, receipt.verdict_reasons) == ("unconfirmed", [])
    assert replies.receipt_verdict_line(receipt) == (
        "⏳ Bankdan tasdiq hali yo'q (SMS kelmaydigan hisob bo'lishi mumkin)."
    )
    assert await _txn_count(session) == 0


async def test_phone_stale_never_suspicious(session, monkeypatch):
    """Paid to the owner's own card and no SMS — but the phone has not
    reported since the receipt came, so the SMS may simply not be here."""
    monkeypatch.setattr(settings, "payment_own_cards", "1234")
    await health.beat(session, "phone_seen", now=datetime.now(TZ) - timedelta(hours=3))
    receipt = await _receipt(session, sent_at=NOW - timedelta(minutes=30))
    assert receipt.direction == "in"

    await receipts.check_waiting(session, now=_later())
    assert (receipt.verdict, receipt.verdict_reasons) == ("unconfirmed", [])


async def test_own_card_with_a_reporting_phone_is_suspicious(session, monkeypatch):
    monkeypatch.setattr(settings, "payment_own_cards", "1234")
    await health.beat(session, "phone_app", now=_later(0) + timedelta(minutes=5))
    receipt = await _receipt(session, sent_at=NOW - timedelta(minutes=30))

    await receipts.check_waiting(session, now=_later())
    assert (receipt.verdict, receipt.verdict_reasons) == (
        "suspicious",
        ["own_card_no_bank"],
    )
    assert replies.receipt_verdict_line(receipt) == (
        "⚠️ Chek shubhali: karta sizniki, lekin bankdan xabar kelmadi. Pul "
        "kartangizga tushganini ilovadan tekshiring."
    )


async def test_an_outgoing_receipt_is_only_unconfirmed(session, monkeypatch):
    """The owner's own payment is never left waiting, and never flagged:
    the flags speak of money reaching the owner. An unread receipt has no
    verdict to give."""
    monkeypatch.setattr(settings, "payment_own_cards", "4321")
    sent = NOW - timedelta(hours=4)
    first = await _receipt(session, sent_at=sent)
    # The same image again, printed long before its message.
    out = await _receipt(
        session, sent_at=sent + timedelta(minutes=5), paid_at=sent - timedelta(days=5)
    )
    assert first.direction == out.direction == "out"
    row, _ = await _chat_photo(session)
    unread = await receipts.record(
        session, row, _reading(receipt=_fields(amount=None)), presume_receipt=False
    )
    assert await receipts.check_waiting(session, now=_later()) == [first, out]
    assert (out.verdict, out.verdict_reasons) == ("unconfirmed", [])
    assert replies.receipt_verdict_line(out) == replies.RECEIPT_UNCONFIRMED
    assert unread.verdict == "waiting"
    assert not receipts.owner_can_judge(out)
    assert not receipts.owner_can_judge(unread)


# --- the card, edited in place ----------------------------------------------------


class _EditingBot(_Bot):
    def __init__(self, *, fail_edit: Exception | None = None) -> None:
        super().__init__()
        self.edits: list[dict] = []
        self.fail_edit = fail_edit

    async def edit_message_text(self, text, **kwargs):
        if self.fail_edit is not None:
            raise self.fail_edit
        self.edits.append({"text": text, **kwargs})
        return True


async def test_the_card_is_edited_in_place_when_the_bank_confirms(session):
    session.add(m.ChatMonitor(tg_chat_id=CHAT, chat_type=ChatType.private, title="A"))
    paid = NOW - timedelta(hours=1)
    receipt = await _receipt(session, sent_at=NOW, paid_at=paid)
    await session.commit()
    bot = _EditingBot()
    await worker.receipt_notice_job(bot, now=NOW.replace(hour=12))
    [(card, kwargs)] = bot.sent
    assert card.splitlines()[-2] == "Bank tasdig'i: ⏳ kutilmoqda"
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == [
        f"rcp:ok:{receipt.id}",
        f"rcp:f:{receipt.id}",
    ]
    assert [b.text for b in buttons] == ["✅ Pul keldi", "🚩 Soxta"]

    sms_at = paid + timedelta(minutes=2)
    await _sms(session, sms_at, INCOME_SMS)
    await session.commit()
    await worker.receipt_check_job(bot, now=NOW.replace(hour=23, minute=50))

    [edit] = bot.edits
    assert edit["message_id"] == 901
    assert edit["chat_id"] == settings.owner_telegram_id
    assert edit["reply_markup"] is None
    lines = edit["text"].splitlines()
    assert lines[0] == "🧾 Chek · Akmal · Telegram"
    assert lines[-2] == (
        f"✅ Chek bankda tasdiqlandi: Payme {day_label(sms_at)} {clock(sms_at)}."
    )
    assert len(bot.sent) == 1  # edited, never sent again
    await session.refresh(receipt)
    assert receipt.verdict_notified_at is not None

    await worker.receipt_check_job(bot, now=NOW.replace(hour=12))
    assert len(bot.edits) == 1


async def test_a_card_that_cannot_be_edited_is_not_retried_forever(session):
    from aiogram.exceptions import TelegramBadRequest

    receipt = await _receipt(session, sent_at=NOW - timedelta(hours=3))
    receipts.mark_notified(receipt, now=NOW, message_id=55)
    await session.commit()
    gone = TelegramBadRequest(method=None, message="message to edit not found")
    bot = _EditingBot(fail_edit=gone)
    await worker.receipt_check_job(bot, now=_later())
    await session.refresh(receipt)
    assert receipt.verdict == "unconfirmed"
    assert receipt.verdict_notified_at is not None

    # A network failure is retried by the next sweep.
    other = await _receipt(
        session, sent_at=NOW - timedelta(hours=3, minutes=-1), image="h" * 64
    )
    receipts.mark_notified(other, now=NOW, message_id=56)
    await session.commit()
    await worker.receipt_check_job(
        _EditingBot(fail_edit=RuntimeError("down")), now=_later()
    )
    await session.refresh(other)
    assert other.verdict == "suspicious" and other.verdict_notified_at is None


def test_the_check_job_is_registered_every_minute():
    source = Path(worker.__file__).read_text()
    start = source.index("        receipt_check_job,\n")
    block = source[start : source.index("scheduler.add_job", start)]
    for needle in (
        "IntervalTrigger(minutes=1)",
        'id="receipt_check"',
        "max_instances=1",
        "coalesce=True",
    ):
        assert needle in block, needle


# --- the owner's verdict ----------------------------------------------------------


class _CardMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.edits.append(("", reply_markup))


class _Tap:
    def __init__(self, data: str, message: _CardMessage) -> None:
        self.data = data
        self.message = message
        self.answered = 0

    async def answer(self, *args, **kwargs):
        self.answered += 1


async def test_owner_verdict_buttons_idempotent(bound):
    receipt = await _receipt(bound, sent_at=NOW - timedelta(hours=3))
    await receipts.check_waiting(bound, now=_later())
    receipts.mark_notified(receipt, now=NOW, message_id=77, verdict=receipt.verdict)
    message = _CardMessage(77)

    for _ in range(2):
        await handlers.on_receipt_button(_Tap(f"rcp:ok:{receipt.id}", message))

    await bound.refresh(receipt)
    assert receipt.verdict == "owner_ok"
    assert [e["new"] for e in _verdict_entries(receipt)] == ["unconfirmed", "owner_ok"]
    assert _verdict_entries(receipt)[-1]["by"] == "button"
    [(text, markup), (again, _)] = message.edits
    assert text == again
    assert text.splitlines()[-2] == "✅ Pul keldi — o'zingiz tasdiqladingiz."
    assert markup is None
    assert receipt.verdict_notified_at is not None
    assert receipt.notice_tg_message_id == 77

    await handlers.on_receipt_button(_Tap(f"rcp:f:{receipt.id}", message))
    await bound.refresh(receipt)
    assert receipt.verdict == "owner_fake"
    assert message.edits[-1][0].splitlines()[-2] == "🚩 Soxta deb belgiladingiz."
    assert len(_verdict_entries(receipt)) == 3
    # Neither tap wrote, voided or linked money.
    assert await _txn_count(bound) == 0
    assert receipt.transaction_id is None


async def test_a_tap_for_a_gone_receipt_only_clears_the_buttons(bound):
    message = _CardMessage(78)
    tap = _Tap("rcp:ok:999999", message)
    await handlers.on_receipt_button(tap)
    assert message.edits == [("", None)]
    assert tap.answered == 1


def test_receipt_buttons_are_never_typed_answers():
    """A typed «1 ha» in a batch reply can never judge a receipt."""
    assert keyboards.RECEIPT_PREFIX not in handlers._button_handlers()
    markup = keyboards.receipt_verdict(2**40)
    for button in markup.inline_keyboard[0]:
        assert len(button.callback_data.encode()) <= 64


# --- configuration ----------------------------------------------------------------


def test_the_receipt_keys_are_documented_everywhere():
    fresh = Settings(_env_file=None)
    assert (
        fresh.receipt_match_minutes,
        fresh.receipt_bank_wait_minutes,
        fresh.receipt_stale_hours,
    ) == (30, 60, 72)
    env = (ROOT / ".env.example").read_text()
    readme = (ROOT / "README.md").read_text()
    for key in (
        "RECEIPT_MATCH_MINUTES",
        "RECEIPT_BANK_WAIT_MINUTES",
        "RECEIPT_STALE_HOURS",
    ):
        assert f"\n{key}=" in env
        assert f"`{key}`" in readme
        assert key in doctor.EXPECTED_ENV_KEYS


async def test_a_bot_card_edited_later_keeps_its_footer_rule(session):
    """The owner's own words booked the payment in the bot message: every
    later edit of that card leaves the "bank only" footer out too."""
    photo = m.Interaction(
        source=InteractionSource.receipt_photo,
        direction=Direction.in_,
        occurred_at=NOW,
        meta={receipts.BOOKED_KEY: True},
    )
    session.add(photo)
    await session.flush()
    receipt = await receipts.record(session, photo, _reading(), presume_receipt=True)
    source = await receipts.card_source(session, receipt)
    assert source == receipts.CardSource(from_bot=True, footer=False)
    text = replies.receipt_card_for(receipt, source)
    assert text.startswith("🧾 Chek · siz · MIYA bot\n")
    assert replies.RECEIPT_FOOTER not in text


# --- copies, retries and imports --------------------------------------------------


async def _bot_image(
    session,
    *,
    sent_at: datetime,
    paid_at: datetime,
    image: str = "a" * 64,
    error: str | None = None,
    source: InteractionSource = InteractionSource.receipt_photo,
    **fields,
) -> m.Receipt:
    """An image the owner sent the bot (or re-read with /process)."""
    row = m.Interaction(source=source, direction=Direction.in_, occurred_at=sent_at)
    session.add(row)
    await session.flush()
    reading = _reading(
        receipt=None
        if error
        else _fields(paid_at=paid_at.strftime("%Y-%m-%dT%H:%M"), **fields),
        is_receipt=error is None,
        error=error,
        image_sha256=image,
    )
    receipt = await receipts.record(session, row, reading, presume_receipt=True)
    assert receipt is not None
    return receipt


async def test_the_owners_forward_of_a_confirmed_receipt_is_confirmed(
    session, monkeypatch
):
    """The client's receipt took the bank row; the owner forwards the same
    photo to the bot. One payment, one bank row: the bot copy is confirmed
    by it too, never "karta sizniki, lekin bankdan xabar kelmadi"."""
    monkeypatch.setattr(settings, "payment_own_cards", "1234")
    paid = NOW - timedelta(hours=3)
    sms_at = paid + timedelta(minutes=1)
    await _sms(session, sms_at, INCOME_SMS)
    chat = await _receipt(session, sent_at=paid + timedelta(minutes=2), paid_at=paid)
    assert chat.verdict == "confirmed" and chat.direction == "in"
    await health.beat(session, "phone_seen", now=_later(0) + timedelta(minutes=1))

    copy = await _bot_image(session, sent_at=paid + timedelta(minutes=7), paid_at=paid)

    assert copy.verdict == "confirmed"
    assert copy.transaction_id is None  # the bank row stays the chat copy's
    [entry] = _verdict_entries(copy)
    assert entry["by"] == "bank"
    assert (entry["transaction_id"], entry["copy_of"]) == (chat.transaction_id, chat.id)
    detail = await receipts.verdict_detail(session, copy)
    assert (detail.bank_label, detail.bank_at) == ("Payme", sms_at)
    assert await receipts.check_waiting(session, now=_later()) == []
    assert await _txn_count(session) == 1


async def test_a_waiting_copy_is_confirmed_once_the_bank_row_is_taken(session):
    """Both copies waited; the SMS links one, the sweep confirms the other.
    A copy of a long-confirmed receipt forwarded today is confirmed by the
    check, never flagged for its old date."""
    paid = NOW - timedelta(hours=3)
    chat = await _receipt(session, sent_at=paid + timedelta(minutes=2), paid_at=paid)
    bot = await _bot_image(session, sent_at=paid + timedelta(minutes=5), paid_at=paid)
    await _sms(session, paid + timedelta(minutes=1), INCOME_SMS)
    await session.refresh(chat)
    await session.refresh(bot)
    assert [chat.verdict, bot.verdict].count("confirmed") == 1

    assert len(await receipts.match_bank(session, now=_later(0))) == 1
    assert chat.verdict == bot.verdict == "confirmed"

    old_chat = await _receipt(
        session,
        sent_at=paid - timedelta(days=4, minutes=-3),
        paid_at=paid - timedelta(days=4),
        image="9" * 64,
        reference="OLD-REF-1",
    )
    await _bank(session, paid - timedelta(days=4, minutes=-1), card=None)
    await receipts.match_bank(session, now=old_chat.created_at)
    old_forward = await _bot_image(
        session,
        sent_at=NOW - timedelta(minutes=5),
        paid_at=paid - timedelta(days=4),
        image="8" * 64,
        reference="OLD-REF-1",
    )
    assert old_chat.verdict == "confirmed"
    assert old_forward.verdict == "waiting"  # its old date keeps it from matching
    await receipts.check_waiting(session, now=_later())
    assert (old_forward.verdict, old_forward.verdict_reasons) == ("confirmed", [])


async def test_a_retried_read_is_not_a_resend(session):
    """The first read failed and the owner sent the photo again, or re-read
    one that was read (/process carries the original message's time):
    neither is the receipt sent twice."""
    sent = NOW - timedelta(hours=3)
    failed = await _bot_image(
        session,
        sent_at=sent - timedelta(minutes=2),
        paid_at=sent,
        image="1" * 64,
        error="APITimeoutError",
    )
    assert failed.state == "needs_review" and failed.image_sha256 == "1" * 64
    retried = await _bot_image(
        session,
        sent_at=sent,
        paid_at=sent - timedelta(hours=1),
        image="1" * 64,
        reference="RETRY-1",
    )
    first = await _bot_image(
        session,
        sent_at=sent + timedelta(minutes=9),
        paid_at=sent - timedelta(hours=1),
        image="2" * 64,
        reference="RETRY-2",
    )
    again = await _bot_image(
        session,
        sent_at=sent + timedelta(minutes=9),
        paid_at=sent - timedelta(hours=1),
        image="2" * 64,
        source=InteractionSource.assistant_bot,
        reference="RETRY-2",
    )
    await receipts.check_waiting(session, now=_later())
    for receipt in (retried, first, again):
        assert (receipt.verdict, receipt.verdict_reasons) == ("unconfirmed", [])


async def test_an_imported_receipt_to_the_own_card_is_only_unconfirmed(
    session, monkeypatch
):
    """An archive import, or a message read days late: the phone could
    never have delivered the bank's SMS of back then since it arrived."""
    monkeypatch.setattr(settings, "payment_own_cards", "1234")
    await health.beat(session, "phone_seen", now=_later(0) + timedelta(minutes=1))
    imported = await _receipt(session, sent_at=NOW - timedelta(hours=3))
    row = await session.get(m.Interaction, imported.interaction_id)
    row.meta = {**row.meta, "archive": True}
    late = await _receipt(
        session, sent_at=NOW - timedelta(days=5), image="7" * 64, reference="LATE-1"
    )
    live = await _receipt(
        session, sent_at=NOW - timedelta(hours=3), image="6" * 64, reference="LIVE-1"
    )
    await session.flush()
    await receipts.check_waiting(session, now=_later())
    assert (imported.verdict, imported.verdict_reasons) == ("unconfirmed", [])
    assert (late.verdict, late.verdict_reasons) == ("unconfirmed", [])
    assert (live.verdict, live.verdict_reasons) == ("suspicious", ["own_card_no_bank"])


# --- failures are contained -------------------------------------------------------


async def _broken(session, **kwargs):
    await session.execute(sa.text("select 1 / 0"))


async def test_a_failed_match_never_costs_the_receipt(session, monkeypatch):
    monkeypatch.setattr(receipts, "match_bank", _broken)
    receipt = await _receipt(session, sent_at=NOW, paid_at=NOW - timedelta(hours=1))
    assert receipt.state == "read" and receipt.verdict == "waiting"
    stored = await session.scalar(sa.select(m.Receipt).where(m.Receipt.id == receipt.id))
    assert stored is receipt

    resolved = await questions.auto_resolve(session, now=_later())
    assert resolved["receipts_bank"] == 0
    assert await session.scalar(sa.select(sa.literal(1))) == 1


async def test_one_failing_receipt_never_stops_the_sweep(session, monkeypatch):
    paid = NOW - timedelta(hours=1)
    bad = await _receipt(session, sent_at=NOW, paid_at=paid)
    good = await _receipt(session, sent_at=NOW, paid_at=paid, image="5" * 64)
    real = receipts._bank_row

    async def _bank_row(session, receipt, **kwargs):
        if receipt.id == bad.id:
            await session.execute(sa.text("select 1 / 0"))
        return await real(session, receipt, **kwargs)

    monkeypatch.setattr(receipts, "_bank_row", _bank_row)
    bank = await _bank(session, paid + timedelta(minutes=1))
    assert await receipts.match_bank(session, now=_later(0)) == [good]
    assert (good.verdict, good.transaction_id) == ("confirmed", bank.id)
    assert bad.verdict == "waiting"


async def test_the_check_job_judges_even_when_matching_fails(session, monkeypatch):
    receipt = await _receipt(session, sent_at=NOW - timedelta(hours=3))
    await session.commit()
    monkeypatch.setattr(receipts, "match_bank", _broken)
    await worker.receipt_check_job(_EditingBot(), now=_later())
    await session.refresh(receipt)
    assert receipt.verdict == "unconfirmed"


# --- the card stays true ----------------------------------------------------------


class _RacingBot(_EditingBot):
    """The owner taps ✅ Pul keldi in the bot process while this edit is on
    its way."""

    def __init__(self, receipt_id: int) -> None:
        super().__init__()
        self.receipt_id = receipt_id

    async def edit_message_text(self, text, **kwargs):
        await super().edit_message_text(text, **kwargs)
        if len(self.edits) == 1:
            async with worker.session_scope() as other:
                await receipts.set_owner_verdict(other, self.receipt_id, paid=True)


async def test_a_verdict_changed_meanwhile_is_edited_again(session):
    receipt = await _receipt(session, sent_at=NOW - timedelta(hours=3))
    receipts.mark_notified(receipt, now=NOW, message_id=60)
    await session.commit()
    bot = _RacingBot(receipt.id)

    await worker.receipt_check_job(bot, now=_later())
    await session.refresh(receipt)
    assert receipt.verdict == "owner_ok"
    assert receipt.verdict_notified_at is None  # the edit showed "unconfirmed"

    await worker.receipt_check_job(bot, now=_later())
    await session.refresh(receipt)
    assert [e["text"].splitlines()[-2] for e in bot.edits] == [
        replies.RECEIPT_UNCONFIRMED,
        replies.RECEIPT_OWNER_OK,
    ]
    assert receipt.verdict_notified_at is not None


async def test_a_card_sent_before_jv05_is_never_overwritten(session, monkeypatch):
    """Before JV-05 a bot card shared its message with the reply and its
    buttons: a fresh card goes out instead, and that one is edited later."""
    monkeypatch.setattr(settings, "money_receipts_silent_at_night", True)
    receipt = await _bot_image(
        session, sent_at=NOW - timedelta(hours=3), paid_at=NOW - timedelta(hours=4)
    )
    receipts.mark_notified(receipt, now=NOW, message_id=70)
    await session.commit()
    bot = _EditingBot()

    await worker.receipt_check_job(bot, now=_later())
    assert bot.edits == []
    [(card, kwargs)] = bot.sent
    assert card.splitlines()[-2] == replies.RECEIPT_UNCONFIRMED
    assert kwargs["reply_markup"] is not None
    await session.refresh(receipt)
    photo = await session.get(m.Interaction, receipt.interaction_id)
    await session.refresh(photo)
    assert receipt.notice_tg_message_id == 901
    assert receipt.verdict_notified_at is not None
    assert receipts.card_editable(photo)

    await receipts.set_owner_verdict(session, receipt.id, paid=False)
    await session.commit()
    await worker.receipt_check_job(bot, now=_later())
    [edit] = bot.edits
    assert edit["message_id"] == 901
    assert len(bot.sent) == 1
