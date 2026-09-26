"""WP-59: `/manba` opens the original words; `/qidir` is hybrid and says
where and who."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import codes, passages, rag
from miya.services.embeddings import EmbeddingError
from tests.test_close_and_correct import _command, _Message, bound  # noqa: F401
from tests.test_memories import FakeEmbedder

NOW = datetime.now(settings.tz).replace(hour=12, minute=0, second=0, microsecond=0)
DM, GROUP = 7101, -7102


def _reply(message: _Message) -> str:
    [(text, _)] = message.sent
    return text


async def _person(session, name: str) -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _say(session, text, *, person=None, chat=DM, at=NOW, out=False, **fields):
    row = m.Interaction(
        source=fields.pop("source", InteractionSource.telegram_userbot),
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def _chats(session):
    session.add_all(
        [
            m.ChatMonitor(tg_chat_id=DM, chat_type=ChatType.private, title="Sardor"),
            m.ChatMonitor(tg_chat_id=GROUP, chat_type=ChatType.group, title="Yuk"),
        ]
    )
    await session.flush()


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(handlers, "get_embedder", lambda: fake)
    return fake


async def _manba(ref: str) -> str:
    message = _Message()
    await handlers.cmd_source(message, _command("manba", ref))
    return _reply(message)


async def _qidir(query: str) -> str:
    message = _Message()
    await handlers.cmd_search(message, _command("qidir", query))
    return _reply(message)


async def test_manba_shows_the_hit_line_bold_with_neighbours(bound):  # noqa: F811
    await _chats(bound)
    sardor = await _person(bound, "Sardor")
    minute = timedelta(minutes=1)
    await _say(bound, "salom", person=sardor, at=NOW - 2 * minute)
    await _say(bound, "yuk qachon?", out=True, at=NOW - minute)
    hit = await _say(bound, "konteyner ertaga chiqadi", person=sardor)
    await _say(bound, "rahmat", out=True, at=NOW + minute)

    text = await _manba(f"m{hit.id}")

    assert text.startswith("📄 <b>Asl yozuv</b> · ")
    assert "Sardor bilan shaxsiy chat" in text
    assert "<b>12:00 Sardor: konteyner ertaga chiqadi</b>" in text
    assert "Siz: yuk qachon?" in text and "Siz: rahmat" in text
    assert text.index("salom") < text.index("konteyner") < text.index("rahmat")


async def test_manba_on_a_call_shows_the_chunk(bound, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "passage_chunk_chars", 60)
    monkeypatch.setattr(settings, "passage_chunk_overlap", 0)
    sardor = await _person(bound, "Sardor")
    transcript = "Birinchi qism gaplar. " * 3 + "Ikkinchi qismda narx kelishildi. " * 3
    call = await _say(
        bound,
        None,
        person=sardor,
        chat=None,
        source=InteractionSource.phone_call,
        transcript=transcript,
        media={"type": "call"},
    )
    await passages.index_pending(bound)

    whole = await _manba(f"m{call.id}")
    part = await _manba(f"m{call.id}#2")

    assert "qo'ng'iroq (Sardor)" in whole and "Birinchi qism" in whole
    assert "Ikkinchi qismda" in part and "Birinchi qism" not in part


async def test_manba_on_a_fact_links_its_source(bound):  # noqa: F811
    sardor = await _person(bound, "Sardor")
    said = await _say(bound, "men Samarqanddanman", person=sardor)
    fact = m.Memory(
        content="Sardor Samarqanddan",
        person_id=sardor.id,
        source_interaction_id=said.id,
        occurred_at=NOW,
    )
    bound.add(fact)
    await bound.flush()

    text = await _manba(f"f{fact.id}")

    assert text.startswith("💡 <b>Xotira</b> · ")
    assert " · Sardor" in text.splitlines()[0]
    assert "Sardor Samarqanddan" in text
    assert text.endswith(f"Manba: /manba m{said.id}")


async def test_manba_unknown_ref_says_not_found(bound):  # noqa: F811
    assert await _manba("m99999999") == replies.MANBA_NOT_FOUND
    assert await _manba("x12") == replies.MANBA_NOT_FOUND
    assert await _manba("") == replies.MANBA_USAGE


async def test_manba_escapes_html_in_counterparty_text(bound):  # noqa: F811
    await _chats(bound)
    sardor = await _person(bound, "Sardor <b>")
    hit = await _say(bound, "<script>alert(1)</script> " + "x" * 5000, person=sardor)

    text = await _manba(f"m{hit.id}")

    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "Sardor &lt;b&gt;" in text
    assert len(text) <= replies.TELEGRAM_LIMIT


async def test_qidir_lists_where_and_who_with_refs(bound, embedder):  # noqa: F811
    await _chats(bound)
    sardor = await _person(bound, "Sardor")
    dm = await _say(bound, "konteyner narxi 1200 dollar", person=sardor)
    group = await _say(
        bound,
        "konteyner bojxonada",
        person=sardor,
        chat=GROUP,
        at=NOW - timedelta(days=1),
    )
    await passages.index_pending(bound)
    await passages.embed_pending(bound, embedder)

    text = await _qidir("konteyner")

    assert text.startswith("🔍 <b>konteyner</b> — ")
    assert "Sardor bilan shaxsiy chat · Sardor: «konteyner narxi 1200 dollar» " in text
    assert f"<code>m{dm.id}</code>" in text
    assert "«Yuk» guruhi · Sardor: «konteyner bojxonada»" in text
    assert f"<code>m{group.id}</code>" in text
    assert rag.SEMANTIC_DEGRADED not in text


async def test_qidir_keeps_the_exact_code_block_on_top(bound, embedder):  # noqa: F811
    await _chats(bound)
    akmal = await _person(bound, "Akmal")
    await codes.attach(bound, akmal, "GS367", source="command", by="command")
    row = await _say(bound, "GS367 yuki keldi", person=akmal, chat=GROUP)
    await codes.index_interaction(bound, row)
    await passages.index_pending(bound)

    text = await _qidir("GS367")

    assert text.startswith(replies.EXACT_HITS_HEADER)
    assert "🔍 <b>GS367</b>" in text
    assert text.index(replies.EXACT_HITS_HEADER) < text.index("🔍 <b>GS367</b>")


async def test_qidir_degrades_to_lexical_when_embeddings_fail(bound, monkeypatch):  # noqa: F811
    class Broken(FakeEmbedder):
        async def embed(self, texts):
            raise EmbeddingError("down")

    monkeypatch.setattr(handlers, "get_embedder", lambda: Broken())
    await _chats(bound)
    sardor = await _person(bound, "Sardor")
    await _say(bound, "bojxona hujjatlari tayyor", person=sardor)
    await passages.index_pending(bound)

    text = await _qidir("bojxona")

    assert "«bojxona hujjatlari tayyor»" in text
    assert text.endswith(rag.SEMANTIC_DEGRADED)

    empty = await _qidir("zilzila")
    assert empty.startswith(replies.qidir_empty("zilzila"))
