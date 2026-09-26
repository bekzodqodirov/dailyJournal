"""WP-40: exact, dated answers for a client code or a waybill number."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import codes, rag
from miya.services.embeddings import EmbeddingError
from tests.test_close_and_correct import _command, _Message, bound  # noqa: F401

NOW = datetime.now(settings.tz).replace(hour=12, minute=0, second=0, microsecond=0)


class _TextMessage(_Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self.date = NOW


def _reply(message: _Message) -> str:
    [(text, _)] = message.sent
    return text


async def _person(session, name: str, code: str | None = None) -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    if code:
        await codes.attach(session, person, code, source="command", by="command")
    return person


async def _said(session, text: str, *, person=None, chat=None, at=None) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at or NOW,
        raw_text=text,
    )
    session.add(interaction)
    await session.flush()
    await codes.index_interaction(session, interaction)
    return interaction


@pytest.fixture
async def seeded(bound):  # noqa: F811
    akmal = await _person(bound, "Akmal", "GS367")
    vali = await _person(bound, "Vali")
    bound.add(
        m.ChatMonitor(tg_chat_id=-100, chat_type=ChatType.group, title="Yuk guruhi")
    )
    await bound.flush()
    await _said(
        bound,
        "GS367ga 3 ta karobka keldi, YW26-004715",
        person=vali,
        chat=-100,
        at=NOW - timedelta(days=2),
    )
    await _said(bound, "YW26-004715 bojxonada", person=vali, chat=-100, at=NOW)
    return akmal


async def test_kim_shows_the_codes_and_where_others_mentioned_them(bound, seeded):  # noqa: F811
    message = _Message()
    await handlers.cmd_person(message, _command("kim", "GS367"))
    text = _reply(message)
    assert "🏷 GS367" in text
    assert replies.CODE_MENTIONS_HEADER in text and "GS367ga" in text
    assert "/tarix GS367" in text


async def test_tarix_puts_the_code_in_its_header(bound, seeded):  # noqa: F811
    bound.add(
        m.Interaction(
            source=InteractionSource.phone_call,
            direction=Direction.in_,
            person_id=seeded.id,
            occurred_at=NOW,
            summary="Yuk haqida gaplashdik",
        )
    )
    await bound.flush()
    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "GS367"))
    assert "<b>Akmal</b> (GS367) — tarix" in _reply(message)


async def test_yuk_lists_mentions_oldest_first(bound, seeded):  # noqa: F811
    message = _Message()
    await handlers.cmd_waybill(message, _command("yuk", "yw26 004715"))
    text = _reply(message)
    assert text.startswith("📦 <b>YW26-004715</b> — 2 ta xabarda:")
    assert text.index("karobka") < text.index("bojxonada")
    none = _Message()
    await handlers.cmd_waybill(none, _command("yuk", "YW26-999999"))
    assert _reply(none) == "📦 <b>YW26-999999</b> hech bir xabarda uchramadi."
    usage = _Message()
    await handlers.cmd_waybill(usage, _command("yuk", ""))
    assert _reply(usage) == replies.YUK_USAGE


async def test_a_bare_code_is_a_lookup_without_the_model(bound, seeded, monkeypatch):  # noqa: F811
    async def boom(*args, **kwargs):
        raise AssertionError("no model call for a bare code")

    monkeypatch.setattr(handlers, "process_interaction", boom)
    monkeypatch.setattr(handlers.rag, "answer", boom)

    message = _TextMessage("GS367")
    await handlers.on_text(message)
    assert "<b>Akmal</b>" in _reply(message)
    row = await bound.scalar(
        sa.select(m.Interaction).where(m.Interaction.raw_text == "GS367")
    )
    assert row.meta == {"kind": "question"} and row.processed is True

    waybill = _TextMessage("yw26-004715?")
    await handlers.on_text(waybill)
    assert _reply(waybill).startswith("📦 <b>YW26-004715</b>")


async def test_qidir_shows_exact_hits_when_the_embedder_is_down(
    bound,  # noqa: F811
    seeded,
    monkeypatch,
):
    def broken():
        raise EmbeddingError("down")

    monkeypatch.setattr(handlers, "get_embedder", broken)
    message = _Message()
    await handlers.cmd_search(message, _command("qidir", "YW26-004715"))
    text = _reply(message)
    assert text.startswith(replies.EXACT_HITS_HEADER)
    assert replies.SEARCH_UNAVAILABLE not in text


async def test_a_mention_is_escaped(bound):  # noqa: F811
    await _said(bound, "GS500 <script>alert(1)</script>")
    message = _Message()
    await handlers.cmd_waybill(message, _command("yuk", "GS500"))
    text = _reply(message)
    assert "<script>" not in text and "&lt;script&gt;" in text


async def test_lookup_code_tool(session):
    akmal = await _person(session, "Akmal", "GS367")
    await _said(session, "GS367 yuki keldi", person=akmal)
    output = json.loads(
        await rag._run_tool(session, None, "lookup_code", {"code": "gs-367"})
    )
    assert output["code"] == "GS367" and output["kind"] == "client"
    assert output["holder"]["display_name"] == "Akmal"
    assert output["holder"]["client_codes"] == ["GS367"]
    assert [line["text"] for line in output["mentions"]] == ["GS367 yuki keldi"]

    unknown = json.loads(
        await rag._run_tool(session, None, "lookup_code", {"code": "GS999"})
    )
    assert unknown["holder"] is None and unknown["mentions"] == []


def test_lookup_code_is_a_tool():
    assert "lookup_code" in [tool["name"] for tool in rag.TOOLS]
