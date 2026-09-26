"""WP-19: the brief, the evening report, /savollar and /holat carry the
question queue; nothing over the budget is lost."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import InteractionSource
from miya.services import claims
from miya.services import extraction as ex
from miya.worker import main as worker
from tests.test_close_and_correct import _Message

TZ = settings.tz


def _today(hour: int, minute: int = 0) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)


class _Bot:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent: list[tuple[str, object]] = []
        self.fail_after = fail_after

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("telegram down")
        self.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent))


async def _claims(session, count: int, *, at: datetime) -> list[int]:
    row = m.Interaction(
        source=InteractionSource.assistant_bot, occurred_at=at, raw_text="x"
    )
    session.add(row)
    await session.flush()
    ids = []
    for i in range(count):
        claim = await claims.create(
            session,
            row,
            claims.KIND_DEBT,
            ex.ExtractedDebt(
                direction="i_owe_them",
                person="Akmal",
                amount=5_000_000 + i,
                asserted_by="them",
            ),
            now=at + timedelta(seconds=i),
        )
        ids.append(claim.id)
    await session.commit()
    return ids


def _payloads(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_brief_has_no_question_buttons_and_is_followed_by_one_batch(session):
    await _claims(session, 7, at=_today(7))
    bot = _Bot()
    assert await worker.brief_job(bot, now=_today(9)) is True

    (brief, brief_markup), (batch, batch_markup) = bot.sent
    assert brief_markup is None or not any(
        p.startswith("cl:") for p in _payloads(brief_markup)
    )
    assert brief.count("/savollar") == 1
    assert batch.startswith("❓ <b>Bugungi savollar</b> · bugun 5/10")
    claim_rows = [p for p in _payloads(batch_markup) if p.startswith("cl:y:")]
    assert len(claim_rows) == settings.question_brief_slots


async def test_over_budget_items_are_counted_not_dropped(session):
    await _claims(session, 15, at=_today(7))
    bot = _Bot()
    await worker.brief_job(bot, now=_today(9))
    brief = bot.sent[0][0]
    assert "❓ Yana 10 ta savol navbatda (10 tasi pul bo'yicha) — /savollar" in brief
    states = list(await session.scalars(sa.select(m.Claim.state)))
    assert states == [claims.PENDING] * 15


async def test_evening_report_has_the_queue_line_and_the_evening_batch(session):
    await _claims(session, 4, at=_today(7))
    bot = _Bot()
    await worker.report_job(bot, now=_today(19))
    (report, _), (batch, markup) = bot.sent
    assert "❓ Yana 4 ta savol navbatda (4 tasi pul bo'yicha) — /savollar" in report
    assert batch.startswith("❓ <b>Kun yakunidagi savollar</b>")
    assert len([p for p in _payloads(markup) if p.startswith("cl:y:")]) == 3


async def test_brief_still_arrives_when_the_batch_send_fails(session):
    ids = await _claims(session, 2, at=_today(7))
    bot = _Bot(fail_after=1)
    assert await worker.brief_job(bot, now=_today(9)) is True
    assert len(bot.sent) == 1
    asked = list(await session.scalars(sa.select(m.Claim.asked_at)))
    assert asked == [None, None]
    assert await session.scalar(sa.select(sa.func.count(m.QuestionLog.id))) == 0
    assert ids


def _bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)


async def test_savollar_lists_everything_paged_and_spends_nothing(session, monkeypatch):
    ids = await _claims(session, 10, at=datetime.now(TZ) - timedelta(hours=1))
    _bound(session, monkeypatch)
    message = _Message()
    await handlers.cmd_questions(message)
    [(text, markup)] = message.sent
    assert text.startswith("❓ <b>Savollar</b> — 10 ta javob kutmoqda · bugun 0/10")
    assert "1/2-sahifa" in text
    payloads = _payloads(markup)
    assert len([p for p in payloads if p.startswith("cl:y:")]) == 8
    assert "sv:p:2" in payloads
    assert await session.scalar(sa.select(sa.func.count(m.QuestionLog.id))) == 0
    # Ranked by stake: the largest claims fill page 1 and count as seen.
    top = await session.get(m.Claim, ids[-1])
    assert top.asked_at is not None


async def test_savollar_empty(session, monkeypatch):
    _bound(session, monkeypatch)
    message = _Message()
    await handlers.cmd_questions(message)
    assert message.sent[0][0] == replies.SAVOLLAR_EMPTY


async def test_holat_line(session, monkeypatch):
    await _claims(session, 2, at=datetime.now(TZ) - timedelta(hours=1))
    _bound(session, monkeypatch)
    message = _Message()
    await handlers.cmd_status(message)
    text = message.sent[0][0]
    assert f"savollar 2 (/savollar) · bugun 0/{settings.question_budget_per_day}" in text


def test_queue_line_is_importable_from_the_recap_without_replies():
    import miya.bot.recap_text as recap_text

    assert "replies" not in recap_text.__dict__
    assert recap_text.queue_line(SimpleNamespace(waiting=2, money=0)) == (
        "❓ Yana 2 ta savol navbatda — /savollar"
    )
