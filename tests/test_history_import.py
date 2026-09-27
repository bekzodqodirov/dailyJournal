"""WP-76: an archive-only history import — stored and searchable, never
extracted, never an open question."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.services import extraction, loops, passages, recall, windows
from miya.tools import backfill
from tests.test_userbot_catchup import CHAT, _message, _monitor

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    def boom():
        raise AssertionError("an archive import must never build a model client")

    monkeypatch.setattr(extraction, "get_client", boom)


class _Client:
    """Newest first, the way iter_messages hands out history."""

    def __init__(self, messages) -> None:
        self.messages = sorted(messages, key=lambda msg: msg.id, reverse=True)

    async def iter_messages(self, chat_id, *, offset_id=0, **kwargs):
        for message in self.messages:
            if offset_id and message.id >= offset_id:
                continue
            yield message


def _voice(mid: int, *, at):
    message = _message(mid, at=at, text=None)
    message.voice = object()
    message.file = type(
        "F", (), {"name": "v.ogg", "size": 1000, "mime_type": "audio/ogg"}
    )()
    return message


def _history():
    return [
        _message(1, at=NOW - timedelta(days=40), text="juda eski"),
        _message(2, at=NOW - timedelta(days=10), text="konteyner narxi qancha?"),
        _message(3, at=NOW - timedelta(days=9), text="konteyner bojxonada"),
    ]


async def _run(**kw):
    await _monitor(last_seen=0, since=NOW - timedelta(days=1))
    return await backfill.archive_private_chats(
        _Client(kw.pop("history", _history())), 30, **kw
    )


async def _rows(session):
    session.expire_all()
    return list(
        await session.scalars(
            sa.select(m.Interaction)
            .where(m.Interaction.tg_chat_id == CHAT)
            .order_by(m.Interaction.id)
        )
    )


async def test_archive_rows_are_processed_and_never_windowed(session):
    [counts] = await _run()

    assert counts.stored == 2
    rows = await _rows(session)
    assert [r.raw_text for r in rows] == [
        "konteyner bojxonada",
        "konteyner narxi qancha?",
    ]
    assert all(r.processed and r.meta["archive"] for r in rows)
    assert await windows.flush_ready_windows(session, now=NOW + timedelta(days=1)) == []


async def test_archive_rows_are_indexed_for_search(session):
    await _run()
    await passages.index_pending(session)

    result = await recall.search(session, None, "konteyner", now=NOW)

    hits = [line.text for e in result.episodes for line in e.lines if line.hit]
    assert "konteyner bojxonada" in hits


async def test_archive_questions_are_not_open_loops(session):
    await _run(history=[_message(4, at=NOW - timedelta(hours=20), text="qachon keladi?")])

    found = await loops.unanswered_questions(session, now=NOW)

    assert not [q for q in found if getattr(q, "tg_chat_id", None) == CHAT]
    assert found == []


async def test_import_is_idempotent(session):
    await _run()
    [again] = await backfill.archive_private_chats(_Client(_history()), 30)

    assert again.stored == 0
    assert len(await _rows(session)) == 2


async def test_voice_not_transcribed_without_flag(session):
    [counts] = await _run(history=[_voice(5, at=NOW - timedelta(days=2))])

    assert counts.stored == 1 and counts.skipped == 1 and counts.transcribed == 0
    [row] = await _rows(session)
    assert row.media["processed"] is True and row.media["skipped"] == "archive"
