"""WP-14: /qayta never re-extracts an applied row, and /tekshir settles every
money text with one tap."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import brief, health, ingest, money_events, queries, reports, sms_money
from tests.test_close_and_correct import _Callback, _command, _Message
from tests.test_pipeline import _open_debt

TZ = settings.tz
DECLINED = "Oplata 250 000 UZS otklonena. Karta *1234"
OTP = "To'lovni tasdiqlash kodi: 482913. Summa: 150 000 so'm"
PUSH = "Payme: Oplata 250 000 so'm muvaffaqiyatli. Karta *1234"


@pytest.fixture
def bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


class _Chat(_Message):
    bot = None
    chat = SimpleNamespace(id=1)


async def _money_text(session, body, *, ago=timedelta(minutes=5), app=False):
    at = datetime.now(TZ) - ago
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
    await money_events.apply_reading(
        session, row, sms_money.read(body, received_at=at), channel=channel, now=at
    )
    await session.commit()
    return row


def _no_extract(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("extract must not be called")

    monkeypatch.setattr(ingest, "extract", boom)


# --- /qayta -----------------------------------------------------------------------


async def test_qayta_never_re_extracts_an_applied_row(bound, monkeypatch):
    _, debt = await _open_debt(bound)
    row = await bound.get(m.Interaction, debt.source_interaction_id)
    row.processed = True
    row.needs_review = True
    await bound.commit()
    _no_extract(monkeypatch)

    message = _Chat()
    await handlers.cmd_retry(message)
    assert message.sent[0][0] == replies.RETRY_NOTHING_TO_DO
    assert await bound.scalar(sa.select(sa.func.count(m.Debt.id))) == 1


async def test_money_texts_are_never_retried(session):
    await _money_text(session, DECLINED)
    assert await queries.retryable_interactions(session) == []


async def test_a_failed_row_is_retried_once(bound, monkeypatch):
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(TZ),
        raw_text="Akmal 5 mln qarz",
        processed=False,
        needs_review=True,
    )
    bound.add(row)
    await bound.commit()

    async def fake(session, interaction):
        interaction.processed = True
        return ingest.IngestResult(interaction=interaction, applied=None)

    monkeypatch.setattr(handlers, "process_interaction", fake)
    first = _Chat()
    await handlers.cmd_retry(first)
    assert first.sent[0][0] == replies.retry_report(1, 0)
    again = _Chat()
    await handlers.cmd_retry(again)
    assert again.sent[0][0] == replies.RETRY_NOTHING_TO_DO


async def test_process_interaction_refuses_a_processed_row(session, monkeypatch):
    _no_extract(monkeypatch)
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        occurred_at=datetime.now(TZ),
        raw_text="x",
        processed=True,
    )
    session.add(row)
    await session.flush()
    result = await ingest.process_interaction(session, row)
    assert result.error == "already_processed"


# --- /tekshir's money block --------------------------------------------------------


async def _tap(data: str, markup=None) -> _Message:
    message = _Message(reply_markup=markup)
    await handlers.on_review_button(_Callback(data, message))
    return message


async def test_tekshir_lists_money_first_with_buttons(bound):
    row = await _money_text(bound, DECLINED)
    message = _Message()
    await handlers.cmd_review(message, _command("tekshir", ""))
    [(text, markup)] = message.sent
    assert "Pul xabarlari — 1 ta tekshiruvda" in text
    assert "rad etilgan ko'rinadi" in text and "250 ming so'm" in text
    payloads = [b.callback_data for r in markup.inline_keyboard for b in r]
    assert payloads == [f"rv:e:{row.id}", f"rv:i:{row.id}", f"rv:n:{row.id}"]


async def test_chiqim_books_once_and_a_second_tap_is_gone(bound):
    row = await _money_text(bound, DECLINED)
    first = await _tap(f"rv:e:{row.id}")
    assert first.sent[0][0].startswith("✅ Yozildi: 📉 Chiqim 250 ming so'm")
    [txn] = list(await bound.scalars(sa.select(m.Transaction)))
    assert await bound.scalar(sa.select(sa.func.count(m.TransactionEvidence.id))) == 1
    await bound.refresh(row)
    assert row.needs_review is False
    assert row.media["money"]["resolved"]["action"] == "expense"
    assert row.media["money"]["transaction_id"] == txn.id

    second = await _tap(f"rv:e:{row.id}")
    assert second.sent[0][0] == replies.REVIEW_GONE
    assert await bound.scalar(sa.select(sa.func.count(m.Transaction.id))) == 1


async def test_chiqim_merges_into_an_already_booked_push(bound):
    await _money_text(bound, PUSH, ago=timedelta(minutes=4), app=True)
    row = await _money_text(bound, DECLINED)
    message = await _tap(f"rv:e:{row.id}")
    assert "allaqachon bor" in message.sent[0][0]
    assert await bound.scalar(sa.select(sa.func.count(m.Transaction.id))) == 1


async def test_pul_emas_books_nothing(bound):
    row = await _money_text(bound, DECLINED)
    message = await _tap(f"rv:n:{row.id}")
    assert message.sent[0][0] == replies.REVIEW_NOT_MONEY
    await bound.refresh(row)
    assert row.needs_review is False
    assert await bound.scalar(sa.select(sa.func.count(m.Transaction.id))) == 0


async def test_the_bulk_button_clears_only_old_rows(bound):
    old = await _money_text(bound, DECLINED, ago=timedelta(days=9))
    fresh = await _money_text(bound, DECLINED, ago=timedelta(days=1))
    rows, _ = await queries.flagged_money(bound)
    markup = keyboards.money_review(rows, now=datetime.now(TZ))
    assert markup.inline_keyboard[-1][0].callback_data == "rv:nold"
    message = await _tap("rv:nold", markup)
    assert message.sent[0][0] == replies.REVIEW_NOT_MONEY_BULK.format(n=1)
    await bound.refresh(old)
    await bound.refresh(fresh)
    assert (old.needs_review, fresh.needs_review) == (False, True)


async def test_hammasi_lists_ignored_texts_and_books_them(bound):
    row = await _money_text(bound, OTP, ago=timedelta(days=2))
    repeat = await _money_text(bound, "Oplata 25 000 sum. Karta *1234")
    repeat.media = {
        **repeat.media,
        "money": {**repeat.media["money"], "verdict": "ignore", "reason": "repeat"},
    }
    await bound.commit()

    plain = _Message()
    await handlers.cmd_review(plain, _command("tekshir", ""))
    assert "E'tiborsiz" not in plain.sent[0][0]

    message = _Message()
    await handlers.cmd_review(message, _command("tekshir", "hammasi"))
    [(text, markup)] = message.sent
    assert "E'tiborsiz qoldirilganlar — oxirgi 7 kun, 1 ta" in text
    assert "SMS-kod" in text
    payloads = [b.callback_data for r in markup.inline_keyboard for b in r]
    assert f"rv:e:{row.id}" in payloads and f"rv:e:{repeat.id}" not in payloads

    booked = await _tap(f"rv:e:{row.id}")
    assert booked.sent[0][0].startswith("✅ Yozildi")
    await bound.refresh(row)
    assert row.media["money"]["resolved"]["action"] == "expense"
    assert (await _tap(f"rv:e:{row.id}")).sent[0][0] == replies.REVIEW_GONE


async def test_the_report_counts_ignored_texts(session):
    today = datetime.now(TZ).date()
    data = await reports.gather(session, today)
    assert "e'tiborsiz" not in reports.render_data_block(data)

    await _money_text(session, "Vash kod: 1234. Nikomu ne soobshchayte")
    await _money_text(session, "Vash kod: 5678. Nikomu ne soobshchayte")
    data = await reports.gather(session, today)
    assert reports.REPORT_IGNORED_LINE.format(n=2) in reports.render_data_block(data)


async def test_health_backlog_ignores_money_rows(session):
    await _money_text(session, DECLINED)
    status = await health.gather(session)
    assert status.needs_review == 0 and status.money_review == 1


async def test_the_brief_carries_the_count_line(session):
    await _money_text(session, DECLINED)
    morning = await brief.gather(session)
    assert not morning.is_empty()
    assert replies.MONEY_REVIEW_LINE.format(n=1) in replies.morning_brief(morning)


async def test_seen_dismisses_a_processed_row(bound):
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        occurred_at=datetime.now(TZ),
        raw_text="rasm",
        processed=True,
        needs_review=True,
    )
    unprocessed = m.Interaction(
        source=InteractionSource.phone_call,
        occurred_at=datetime.now(TZ),
        processed=False,
        needs_review=True,
    )
    bound.add_all([row, unprocessed])
    await bound.commit()
    markup = keyboards.money_review([], [row, unprocessed])
    assert [r[0].callback_data for r in markup.inline_keyboard] == [f"rv:ok:{row.id}"]
    message = await _tap(f"rv:ok:{row.id}")
    assert message.sent[0][0] == replies.REVIEW_SEEN
    await bound.refresh(row)
    assert row.needs_review is False and "reviewed" in row.meta
