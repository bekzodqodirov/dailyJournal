"""WP-20: new groups arrive as one small digest a day, active ones first;
channels are left off by rule; nothing a switched-off group says is stored."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa
from telethon.tl.types import Chat

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType
from miya.services import brief, chats, questions
from miya.userbot import main as userbot
from miya.worker import main as worker
from tests.test_close_and_correct import _Callback, _Message

TZ = settings.tz


def _today(hour: int, minute: int = 0) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent))


def _group(i: int, **kw) -> m.ChatMonitor:
    fields = {
        "tg_chat_id": -1000 - i,
        "chat_type": ChatType.group,
        "title": f"Guruh {i}",
        "monitor_enabled": False,
        "seen_count": 1,
    }
    fields.update(kw)
    return m.ChatMonitor(**fields)


def _payloads(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_first_sync_of_100_groups_is_one_small_digest(session):
    session.add_all([_group(i) for i in range(100)])
    session.add(m.ReminderLog(kind=brief.BRIEF_KIND, ref=_today(0).date().isoformat()))
    await session.commit()
    bot = _Bot()

    await worker.question_job(bot, now=_today(11))
    await worker.question_job(bot, now=_today(13, 30))

    [(text, markup)] = bot.sent
    assert text.startswith("👥 <b>Yangi guruhlar</b> — 3 ta. Qaysilarini o'qiyin?")
    assert "Yana 97 tasi keyingi safar." in text
    assert len(markup.inline_keyboard) == settings.question_group_digest_size + 1
    assert _payloads(markup)[-1] == "ng:r"
    logs = list(await session.scalars(sa.select(m.QuestionLog)))
    assert len(logs) == 3 and all(row.ref.startswith("g") for row in logs)


async def test_channels_are_decided_by_rule_and_never_offered(session):
    channel = _group(1, chat_type=ChatType.channel, title="Kanal")
    session.add(channel)
    await session.commit()
    now = datetime.now(TZ)
    assert await chats.apply_default_rules(session, now=now) == 1
    await session.refresh(channel)
    assert channel.decided_by == "rule:channel"
    assert await chats.awaiting_join_question(session, now=now) == []
    listed, _ = await chats.list_monitors(session)
    assert channel.id in [row.id for row in listed]


async def test_question_ask_channels_true_offers_them(session, monkeypatch):
    monkeypatch.setattr(settings, "question_ask_channels", True)
    channel = _group(1, chat_type=ChatType.channel, title="Kanal")
    session.add(channel)
    await session.commit()
    now = datetime.now(TZ)
    assert await chats.apply_default_rules(session, now=now) == 0
    assert [m_.id for m_ in await chats.awaiting_join_question(session, now=now)] == [
        channel.id
    ]


async def test_lazy_group_waits_for_traffic(session):
    quiet = _group(1, seen_count=0)
    session.add(quiet)
    await session.commit()
    now = datetime.now(TZ)
    assert await chats.awaiting_join_question(session, now=now) == []
    quiet.seen_count = 1
    await session.commit()
    assert len(await chats.awaiting_join_question(session, now=now)) == 1


async def test_digest_order_addressed_then_owner_active_then_busiest(session):
    now = datetime.now(TZ)
    busy = _group(1, seen_count=50)
    active = _group(2, owner_active_at=now)
    addressed = _group(3, addressed_at=now)
    quiet = _group(4, seen_count=2)
    session.add_all([busy, active, addressed, quiet])
    await session.commit()
    order = [row.id for row in await chats.awaiting_join_question(session, now=now)]
    assert order == [addressed.id, active.id, busy.id, quiet.id]
    body = replies.group_digest([addressed, active])
    assert "📣" in body and "✍️" in body
    labels = [
        b.text
        for row in keyboards.group_digest([addressed, active]).inline_keyboard
        for b in row
    ]
    assert labels[0].startswith("✅ 📣 ") and labels[2].startswith("✅ ✍️ ")


def _bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)


async def _digest(session, n: int):
    rows = [_group(i) for i in range(n)]
    session.add_all(rows)
    await session.flush()
    now = datetime.now(TZ)
    for row in rows:
        chats.mark_offered(row, now=now)
    await session.flush()
    return rows, keyboards.group_digest(rows)


async def test_tap_ha_trims_only_its_row_switches_on_and_queues_backfill(
    session, monkeypatch
):
    _bound(session, monkeypatch)
    rows, markup = await _digest(session, 3)
    message = _Message(reply_markup=markup)
    await handlers.on_new_group_button(_Callback(f"ng:y:{rows[1].id}", message))
    left = _payloads(message.edited_markup)
    assert f"ng:y:{rows[1].id}" not in left and f"ng:y:{rows[0].id}" in left
    assert "ng:r" in left
    assert rows[1].monitor_enabled and rows[1].backfill_requested_at is not None
    assert rows[1].decided_by == "owner"
    assert "endi o'qiyman" in message.sent[0][0]


async def test_qolganlari_kerak_emas_declines_the_rest(session, monkeypatch):
    _bound(session, monkeypatch)
    rows, markup = await _digest(session, 3)
    message = _Message(reply_markup=markup)
    await handlers.on_new_group_button(_Callback("ng:r", message))
    assert message.edited_markup is None
    assert message.sent[0][0] == replies.GROUP_REST_DONE.format(n=3)
    assert {row.decided_by for row in rows} == {"owner"}
    assert not any(row.monitor_enabled for row in rows)


async def test_two_unanswered_digests_default_to_rule_ignored(session):
    group = _group(1, digest_shows=2, offered_at=datetime.now(TZ) - timedelta(days=2))
    session.add(group)
    await session.commit()
    assert await chats.apply_default_rules(session, now=datetime.now(TZ)) == 1
    await session.refresh(group)
    assert group.decided_by == "rule:ignored"


async def test_chats_toggle_sets_decided_by_owner(session):
    group = _group(1)
    session.add(group)
    await session.flush()
    await chats.toggle(session, group.id, "monitor_enabled")
    assert group.decided_by == "owner" and group.monitor_enabled


async def test_userbot_counts_a_switched_off_group_without_storing_it(
    session, monkeypatch
):  # session: needs the database (and cleans up after)
    from miya.db.session import session_scope

    monkeypatch.setattr(settings, "owner_aliases", "Bekzod, Bekzod aka")
    chat = Chat(
        id=777, title="Yuk", photo=None, participants_count=3, date=None, version=1
    )

    def _msg(text: str, *, out: bool = False, mid: int = 1):
        async def get_chat():
            return chat

        return SimpleNamespace(
            id=mid,
            chat_id=-777,
            date=datetime.now(TZ),
            message=text,
            out=out,
            mentioned=False,
            file=None,
            sticker=None,
            gif=None,
            voice=None,
            audio=None,
            video_note=None,
            photo=None,
            video=None,
            document=None,
            get_chat=get_chat,
        )

    assert (
        await userbot.ingest_message(None, _msg("Bekzod aka, konteyner qachon?")) is False
    )
    async with session_scope() as session:
        monitor = await chats.get_monitor(session, -777)
        assert monitor.seen_count == 1 and monitor.addressed_at is not None
        assert monitor.owner_active_at is None
        assert await session.scalar(sa.select(sa.func.count(m.Interaction.id))) == 0
    assert await userbot.ingest_message(None, _msg("ok", out=True, mid=2)) is False
    async with session_scope() as session:
        monitor = await chats.get_monitor(session, -777)
        assert monitor.seen_count == 2 and monitor.owner_active_at is not None
        await session.execute(sa.delete(m.ChatMonitor))


def test_group_ids_in_reads_the_digest():
    rows = [
        SimpleNamespace(
            id=i, title=f"G{i}", tg_chat_id=-i, addressed_at=None, owner_active_at=None
        )
        for i in (4, 7)
    ]
    assert keyboards.group_ids_in(keyboards.group_digest(rows)) == [4, 7]
    assert questions.KIND_GROUPS == "groups"
