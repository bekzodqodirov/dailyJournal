"""WP-15: a receipt for each booked payment, folded for bursts, one summary
for a first import, quiet-hours aware."""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import money_events, sms_money
from miya.worker import main as worker

TZ = settings.tz
TODAY = datetime.now(TZ).replace(hour=14, minute=30, second=0, microsecond=0)
BOOK = "Oplata {n} 000 sum\nKarta *1234\nKORZINKA.UZ"
REVIEW = "Oplata 250 000 UZS otklonena. Karta *1234"
IGNORE = "Vash kod: {n}. Nikomu ne soobshchayte"


class _Bot:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((text, kwargs))


async def _event(session, body, *, at, now=None, app=False):
    if app:
        source, media = InteractionSource.phone_notification, {"app_label": "Payme"}
        channel = money_events.channel_for_app("uz.dida.payme")
    else:
        source, media = InteractionSource.phone_sms, {"type": "sms", "sender": "Payme"}
        channel = money_events.channel_for_sms("Payme")
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
        session, row, sms_money.read(body, received_at=at), channel=channel, now=now or at
    )
    await session.commit()
    return row, outcome


def _payloads(markup) -> list[str]:
    return [b.callback_data for r in markup.inline_keyboard for b in r]


async def test_one_booked_sms_is_one_receipt(session):
    row, outcome = await _event(session, BOOK.format(n=250), at=TODAY)
    bot = _Bot()
    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=1))
    [(text, kwargs)] = bot.sent
    txn = outcome.transaction
    assert text.startswith("📉 Chiqim: <b>250 ming so'm</b> · KORZINKA.UZ · karta *1234")
    assert f"<code>x{txn.id}</code>" in text and "✉️" in text
    assert f"rec:v:x{txn.id}" in _payloads(kwargs["reply_markup"])
    assert kwargs["disable_notification"] is False

    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=2))
    assert len(bot.sent) == 1


async def test_merged_review_and_ignored_events_send_nothing(session):
    await _event(session, BOOK.format(n=250), at=TODAY)
    bot = _Bot()
    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=1))
    bot.sent.clear()

    push = "Payme: Oplata 250 000 so'm muvaffaqiyatli. Karta *1234"
    merged, _ = await _event(session, push, at=TODAY + timedelta(seconds=40), app=True)
    review, _ = await _event(session, REVIEW, at=TODAY)
    ignored, _ = await _event(session, IGNORE.format(n=1234), at=TODAY)
    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=2))
    assert bot.sent == []
    for row in (merged, review, ignored):
        await session.refresh(row)
        assert "money_notice" not in (row.meta or {})


async def test_a_burst_is_folded(session):
    for n in range(5):
        await _event(session, BOOK.format(n=100 + n), at=TODAY + timedelta(minutes=n * 3))
    bot = _Bot()
    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=20))
    [(text, kwargs)] = bot.sent
    assert text.startswith("💳 <b>5 ta yangi to'lov</b>")
    assert len(_payloads(kwargs["reply_markup"])) == 10


async def test_quiet_hours_hold_or_send_silently(session, monkeypatch):
    night = TODAY.replace(hour=23, minute=40)
    await _event(session, BOOK.format(n=250), at=night)
    bot = _Bot()
    await worker.money_notice_job(bot, now=night + timedelta(minutes=5))
    assert bot.sent == []

    monkeypatch.setattr(settings, "money_receipts_silent_at_night", True)
    await worker.money_notice_job(bot, now=night + timedelta(minutes=6))
    [(_, kwargs)] = bot.sent
    assert kwargs["disable_notification"] is True


async def test_a_held_receipt_goes_out_in_the_morning(session):
    night = TODAY.replace(hour=23, minute=40)
    await _event(session, BOOK.format(n=250), at=night)
    bot = _Bot()
    await worker.money_notice_job(bot, now=night + timedelta(minutes=5))
    morning = (night + timedelta(days=1)).replace(hour=7, minute=31)
    await worker.money_notice_job(bot, now=morning)
    assert len(bot.sent) == 1


async def test_a_first_import_is_one_summary(session):
    old = TODAY - timedelta(days=3)
    for n in range(30):
        await _event(
            session, BOOK.format(n=100 + n), at=old + timedelta(hours=n), now=TODAY
        )
    for n in range(6):
        await _event(session, REVIEW, at=old + timedelta(minutes=n * 7), now=TODAY)
    for n in range(4):
        await _event(session, IGNORE.format(n=1000 + n), at=old, now=TODAY)
    bot = _Bot()
    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=1))
    [(text, _)] = bot.sent
    assert "30 ta to'lov yozildi, 6 tasi /tekshir'da, 4 tasi e'tiborsiz" in text

    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=2))
    assert len(bot.sent) == 1
    stamped = await session.scalar(
        sa.select(sa.func.count(m.Interaction.id)).where(
            m.Interaction.meta.has_key("money_notified")
        )
    )
    assert stamped == 40


async def test_a_failed_send_marks_nothing_and_retries(session):
    row, _ = await _event(session, BOOK.format(n=250), at=TODAY)
    await worker.money_notice_job(_Bot(fail=True), now=TODAY + timedelta(minutes=1))
    await session.refresh(row)
    assert "money_notified" not in row.meta
    bot = _Bot()
    await worker.money_notice_job(bot, now=TODAY + timedelta(minutes=2))
    assert len(bot.sent) == 1


async def test_receipts_off_queues_nothing(session, monkeypatch):
    monkeypatch.setattr(settings, "money_receipts", "off")
    row, _ = await _event(session, BOOK.format(n=250), at=TODAY)
    await session.refresh(row)
    assert "money_notice" not in (row.meta or {})
