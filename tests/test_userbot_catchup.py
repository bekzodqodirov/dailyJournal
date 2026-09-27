"""WP-21: after downtime the userbot reads allowed chats forward from the
last message it saw — never before the chat was switched on, never twice."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType
from miya.db.session import session_scope
from miya.services import chats, health
from miya.tools import backfill
from miya.userbot import main as userbot

TZ = settings.tz
CHAT = 424242
PEER = User(id=CHAT, first_name="Akmal", username="akmal_test")


def _message(mid: int, *, at: datetime, text: str | None = "salom", sticker=None):
    async def get_chat():
        return PEER

    async def get_sender():
        return PEER

    return SimpleNamespace(
        id=mid,
        chat_id=CHAT,
        date=at,
        message=text,
        out=False,
        mentioned=False,
        file=None,
        sticker=sticker,
        gif=None,
        voice=None,
        audio=None,
        video_note=None,
        photo=None,
        video=None,
        document=None,
        get_chat=get_chat,
        get_sender=get_sender,
    )


class _Client:
    """History and dialogs, the way Telethon hands them out."""

    def __init__(self, messages, *, flood_after: int | None = None) -> None:
        self.messages = sorted(messages, key=lambda msg: msg.id)
        self.flood_after = flood_after
        self.calls: list[dict] = []

    async def iter_messages(self, chat_id, **kwargs):
        self.calls.append(kwargs)
        min_id = kwargs.get("min_id") or 0
        since = kwargs.get("offset_date")
        yielded = 0
        for message in self.messages:
            if message.id <= min_id or (since is not None and message.date < since):
                continue
            if self.flood_after is not None and yielded >= self.flood_after:
                raise FloodWaitError(request=None, capture=600)
            yielded += 1
            yield message

    async def iter_dialogs(self):
        top = self.messages[-1] if self.messages else None
        yield SimpleNamespace(id=CHAT, entity=PEER, message=top)


async def _monitor(*, last_seen=None, since=None, enabled=True) -> None:
    async with session_scope() as session:
        session.add(
            m.ChatMonitor(
                tg_chat_id=CHAT,
                chat_type=ChatType.private,
                title="Akmal",
                monitor_enabled=enabled,
                monitoring_since=since or datetime.now(TZ) - timedelta(days=30),
                last_seen_message_id=last_seen,
            )
        )


async def _stored_ids(session) -> list[int]:
    rows = await session.scalars(
        sa.select(m.Interaction.meta).where(m.Interaction.tg_chat_id == CHAT)
    )
    return sorted(meta["tg_message_id"] for meta in rows)


async def _cursor(session) -> int | None:
    return await session.scalar(
        sa.select(m.ChatMonitor.last_seen_message_id).where(
            m.ChatMonitor.tg_chat_id == CHAT
        )
    )


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(userbot, "_last_catch_up", None)
    monkeypatch.setattr(userbot, "_dialog_tops", {})


def _history(ids, *, start=None):
    start = start or datetime.now(TZ) - timedelta(hours=5)
    return [_message(i, at=start + timedelta(minutes=i)) for i in ids]


async def test_catch_up_reads_only_after_the_cursor(session):
    await _monitor(last_seen=100)
    client = _Client(_history(range(99, 106)))
    assert await userbot.catch_up_sweep(client) == 5
    assert await _stored_ids(session) == [101, 102, 103, 104, 105]
    assert await _cursor(session) == 105


async def test_catch_up_never_reads_before_monitoring_since(session):
    switched_on = datetime.now(TZ) - timedelta(hours=1)
    await _monitor(since=switched_on)
    old = _history(range(1, 4), start=switched_on - timedelta(days=2))
    new = _history(range(10, 13), start=switched_on)
    assert await userbot.catch_up_sweep(_Client(old + new)) == 3
    assert await _stored_ids(session) == [10, 11, 12]


async def test_disabled_chats_are_not_read(session):
    await _monitor(last_seen=1, enabled=False)
    client = _Client(_history(range(1, 5)))
    assert await userbot.catch_up_sweep(client) == 0
    assert client.calls == []
    assert await _stored_ids(session) == []


async def test_catch_up_is_idempotent(session):
    await _monitor(last_seen=0)
    client = _Client(_history(range(1, 6)))
    assert await userbot.catch_up_sweep(client) == 5
    assert await userbot.catch_up_sweep(client) == 0
    assert await _stored_ids(session) == [1, 2, 3, 4, 5]


async def test_a_sticker_advances_the_cursor(session):
    await _monitor(last_seen=10)
    sticker = _message(11, at=datetime.now(TZ), text=None, sticker=object())
    assert await userbot.catch_up_sweep(_Client([sticker])) == 0
    assert await _cursor(session) == 11
    assert await _stored_ids(session) == []


async def test_flood_wait_does_not_crash_the_sweep(session):
    await _monitor(last_seen=0)
    client = _Client(_history(range(1, 10)), flood_after=3)
    assert await userbot.catch_up_sweep(client) == 3
    assert await _cursor(session) == 3


async def test_live_message_during_catch_up_is_stored_once(session):
    await _monitor(last_seen=0)
    message = _message(7, at=datetime.now(TZ))
    results = await asyncio.gather(
        userbot.ingest_message(None, message), userbot.ingest_message(None, message)
    )
    assert sorted(results) == [False, True]
    assert await _stored_ids(session) == [7]


async def test_catch_up_line_survives_the_next_heartbeat(session):
    await _monitor(last_seen=0)
    assert await userbot.catch_up_sweep(_Client(_history(range(1, 4)))) == 3
    await userbot.beat_userbot(enabled=True, connected=True)
    beats = await health.beats(session)
    assert beats["userbot"].detail["caught_up"] == 3


async def test_the_holat_line_shows_for_a_day(session):
    from miya.bot import replies

    await _monitor(last_seen=0)
    await userbot.catch_up_sweep(_Client(_history(range(1, 3))))
    await userbot.beat_userbot(enabled=True, connected=True)
    status = await health.gather(session)
    assert "uzilishdan keyin 2 ta xabar qayta o'qildi" in replies.status_report(
        status, health.problems(status)
    )


async def test_catch_up_chat_uses_offset_date_without_a_cursor():
    client = _Client([])
    since = datetime.now(TZ) - timedelta(days=1)
    await backfill.catch_up_chat(client, CHAT, after_id=None, since=since, limit=10)
    assert client.calls == [{"reverse": True, "limit": 10, "offset_date": since}]


async def test_toggle_on_sets_monitoring_since_and_queues_private_backfill(session):
    monitor = m.ChatMonitor(
        tg_chat_id=555, chat_type=ChatType.private, title="Vali", monitor_enabled=False
    )
    session.add(monitor)
    await session.flush()
    now = datetime.now(TZ)
    await chats.toggle(session, monitor.id, "monitor_enabled", now=now)
    assert monitor.monitoring_since == now
    assert monitor.backfill_requested_at == now
