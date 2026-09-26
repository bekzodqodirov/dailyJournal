"""WP-54: "🌙 Kecha" — yesterday and the night — before the morning brief."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import anthropic
import httpx
import sqlalchemy as sa

from miya.bot import handlers, recap_text, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    Direction,
    InteractionSource,
    TransactionType,
)
from miya.services import recaps
from miya.worker import main as worker
from tests.test_close_and_correct import _Message, bound  # noqa: F401
from tests.test_open_loops_surface import _Bot, _buttons, _seed_loops

TZ = settings.tz
TODAY = datetime.now(TZ).date()
YESTERDAY = TODAY - timedelta(days=1)
_ids = itertools.count(1)


def _at(day, hour, minute=0) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=TZ).replace(
        hour=hour, minute=minute
    )


NINE = _at(TODAY, 9)
CUTOFF = _at(YESTERDAY, 19)


def _boom():
    raise AssertionError("no model call was expected")


class _Down:
    messages = None

    def __init__(self):
        self.messages = self

    def with_options(self, **kwargs):
        return self

    async def create(self, **kwargs):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        )


async def _person(session, name: str) -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _chat(session, person: m.Person, at: datetime, text: str = "salom"):
    chat = 9000 + person.id
    known = await session.scalar(
        sa.select(m.ChatMonitor).where(m.ChatMonitor.tg_chat_id == chat)
    )
    if known is None:
        session.add(m.ChatMonitor(tg_chat_id=chat, chat_type=ChatType.private, title=""))
    session.add(
        m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.in_,
            person_id=person.id,
            tg_chat_id=chat,
            occurred_at=at,
            raw_text=text,
            meta={"tg_message_id": next(_ids)},
        )
    )
    await session.flush()


async def _delivered_evening(session, subjects=(), prose=None):
    session.add(
        m.DailyReport(
            report_date=YESTERDAY,
            kind="evening",
            content="x",
            parts=["x"],
            parts_sent=1,
            delivered_at=CUTOFF,
            window_start=_at(YESTERDAY, 0),
            window_end=CUTOFF,
            stats={"subjects": list(subjects)},
        )
    )
    for key, text in (prose or {}).items():
        session.add(
            m.RecapDigest(
                digest_date=YESTERDAY,
                subject_key=key,
                window_start=_at(YESTERDAY, 0),
                window_end=CUTOFF,
                input_hash="0" * 64,
                prose=text,
                model="stub",
            )
        )
    await session.flush()


async def _morning(session):
    return await recaps.build_morning(session, now=NINE, store=False)


async def test_reminder_mode_reuses_evening_digests_without_a_model_call(
    session, monkeypatch
):
    monkeypatch.setattr(recaps, "get_client", _boom)
    akmal = await _person(session, "Akmal")
    await _delivered_evening(
        session, [f"p:{akmal.id}"], {f"p:{akmal.id}": "Invoys haqida gaplashildi"}
    )
    text = "\n\n".join((await _morning(session)).parts)
    assert recap_text.KECHA_TOP in text
    assert "<b>Akmal</b> — 🤖 <i>Invoys haqida gaplashildi</i>" in text


async def test_late_section_covers_after_the_cutoff_only(session, monkeypatch):
    monkeypatch.setattr(recaps, "get_client", _Down)
    vali = await _person(session, "Vali")
    akmal = await _person(session, "Akmal")
    await _chat(session, vali, _at(YESTERDAY, 12))
    await _chat(session, akmal, _at(YESTERDAY, 22))
    await _delivered_evening(session)
    text = "\n\n".join((await _morning(session)).parts)
    late = text[text.index("🌃") :]
    assert "19:00 dan keyin" in late
    assert "Akmal" in late and "Vali" not in late


async def test_full_mode_when_the_evening_recap_was_not_delivered(session, monkeypatch):
    monkeypatch.setattr(recaps, "get_client", _Down)
    vali = await _person(session, "Vali")
    await _chat(session, vali, _at(YESTERDAY, 12))
    result = await _morning(session)
    text = "\n\n".join(result.parts)
    assert recap_text.KECHA_FULL in text and recap_text.KECHA_FULL_NOTE in text
    assert "Vali" in text and result.stats["mode"] == "full"


async def test_yesterday_money_is_the_whole_calendar_day(session, monkeypatch):
    monkeypatch.setattr(recaps, "get_client", _boom)
    for at, amount in (
        (_at(YESTERDAY, 1), 100),
        (_at(YESTERDAY, 23), 200),
        (_at(TODAY, 8), 400),
    ):
        session.add(
            m.Transaction(
                type=TransactionType.income,
                amount=amount,
                currency=Currency.USD,
                occurred_at=at,
            )
        )
    await _delivered_evening(session)
    text = "\n\n".join((await _morning(session)).parts)
    assert "💰 Kirim: $300" in text


async def test_brief_job_sends_kecha_first_then_the_brief_with_buttons_then_the_batch(
    session, monkeypatch
):
    monkeypatch.setattr(recaps, "get_client", _Down)
    await _seed_loops(session)
    vali = await _person(session, "Vali")
    await _chat(session, vali, _at(YESTERDAY, 12))
    await session.commit()
    bot = _Bot()
    await worker.brief_job(bot)
    texts = bot.texts
    assert texts[0].startswith("🌙 <b>Kecha</b>") and bot.sent[0][1] is None
    [brief_at] = [i for i, t in enumerate(texts) if replies.BRIEF_HEADER in t]
    assert brief_at > 0 and _buttons(bot.sent[brief_at][1])
    assert texts[-1].startswith("❓ <b>Bugungi savollar</b>")


async def test_brief_arrives_when_the_morning_recap_raises(session, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("recap broke")

    monkeypatch.setattr(recaps, "build_morning", explode)
    bot = _Bot()
    await worker.brief_job(bot)
    assert any(replies.BRIEF_HEADER in t for t in bot.texts)


async def test_brief_arrives_with_the_model_down(session, monkeypatch):
    monkeypatch.setattr(recaps, "get_client", _Down)
    vali = await _person(session, "Vali")
    await _chat(session, vali, _at(YESTERDAY, 12))
    await session.commit()
    bot = _Bot()
    await worker.brief_job(bot)
    assert recap_text.PROSE_DOWN in bot.texts[0]
    assert any(replies.BRIEF_HEADER in t for t in bot.texts)


async def test_nothing_yesterday_means_only_the_brief(session, monkeypatch):
    monkeypatch.setattr(recaps, "get_client", _boom)
    bot = _Bot()
    await worker.brief_job(bot)
    assert not any(t.startswith("🌙") for t in bot.texts)
    assert replies.BRIEF_HEADER in bot.texts[0]


async def test_kecha_command_and_ertalab_order(bound, monkeypatch):  # noqa: F811
    monkeypatch.setattr(recaps, "get_client", _Down)
    empty = _Message()
    await handlers.cmd_yesterday(empty)
    assert empty.sent == [(recap_text.KECHA_NONE, None)]

    vali = await _person(bound, "Vali")
    await _chat(bound, vali, _at(YESTERDAY, 12))
    kecha = _Message()
    await handlers.cmd_yesterday(kecha)
    assert kecha.sent[0][0].startswith("🌙 <b>Kecha</b>")
    ertalab = _Message()
    await handlers.cmd_brief(ertalab)
    assert ertalab.sent[0][0].startswith("🌙 <b>Kecha</b>")
    assert replies.BRIEF_HEADER in ertalab.sent[-1][0]
