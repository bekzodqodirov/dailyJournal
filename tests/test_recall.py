"""WP-56: hybrid recall — words, typos and meaning, fused with recency,
returned as cited episodes."""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import passages, recall
from miya.services.embeddings import Embedder, EmbeddingError
from tests.test_memories import FakeEmbedder

TZ = settings.tz
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=TZ)
DM, DM2, GROUP = 6101, 6102, -6103
_ids = itertools.count(1)


async def _person(session, name: str, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=[], **fields)
    session.add(person)
    await session.flush()
    return person


async def _chats(session):
    session.add_all(
        [
            m.ChatMonitor(tg_chat_id=DM, chat_type=ChatType.private, title="Sardor"),
            m.ChatMonitor(tg_chat_id=DM2, chat_type=ChatType.private, title="Vali"),
            m.ChatMonitor(tg_chat_id=GROUP, chat_type=ChatType.group, title="Yuk"),
        ]
    )
    await session.flush()


async def _say(session, text, *, person=None, chat=DM, at=NOW, out=False, meta=None):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        meta={"tg_message_id": next(_ids), **(meta or {})},
    )
    session.add(row)
    await session.flush()
    return row


async def _index(session, embedder=None):
    await passages.index_pending(session)
    if embedder is not None:
        await passages.embed_pending(session, embedder)


async def _search(session, text, embedder=None, **kw):
    return await recall.search(session, embedder, text, now=NOW, **kw)


def _hits(result) -> list[str]:
    return [line.text for e in result.episodes for line in e.lines if line.hit]


def test_parse_question_extracts_person_content_and_period():
    sardor = m.Person(id=1, display_name="Sardor", aliases=[])
    parsed = recall.parse_question("o'tgan hafta Sardor nima degan edi", [sardor], NOW)
    assert parsed.person is sardor
    assert parsed.content_terms == []
    assert parsed.period == (date(2026, 9, 14), date(2026, 9, 20))


def test_two_akmals_become_candidates_not_a_filter():
    a = m.Person(id=1, display_name="Akmal", aliases=[])
    b = m.Person(id=2, display_name="Akmal", aliases=[])
    parsed = recall.parse_question("Akmal konteyner haqida nima dedi", [a, b], NOW)
    assert parsed.person is None and {p.id for p in parsed.person_candidates} == {1, 2}
    assert parsed.content_terms == ["konteyner"]


def test_cyrillic_person_is_detected():
    sardor = m.Person(id=1, display_name="Sardor", aliases=[])
    parsed = recall.parse_question("Сардор нима деди", [sardor], NOW)
    assert parsed.person is sardor


async def test_lexical_prefix_finds_suffixed_forms(session):
    await _chats(session)
    await _say(session, "Konteynerni bojxona ushlab qoldi")
    await _say(session, "salom")
    await _index(session)
    result = await _search(session, "konteyner nima bo'ldi")
    assert _hits(result) == ["Konteynerni bojxona ushlab qoldi"]


async def test_trigram_fallback_catches_a_typo(session):
    await _chats(session)
    await _say(session, "Konteynerni bojxona ushlab qoldi")
    await _index(session)
    assert _hits(await _search(session, "kontener")) == [
        "Konteynerni bojxona ushlab qoldi"
    ]


async def test_recency_breaks_ties(session):
    await _chats(session)
    await _say(session, "konteyner keldi", at=NOW - timedelta(days=200))
    await _say(session, "konteyner keldi", chat=DM2, at=NOW - timedelta(days=1))
    await _index(session)
    result = await _search(session, "konteyner", k=1)
    [episode] = result.episodes
    assert episode.when == NOW - timedelta(days=1)


async def test_hard_date_filter_from_tool_args(session):
    await _chats(session)
    await _say(session, "konteyner eski", at=NOW - timedelta(days=30))
    await _say(session, "konteyner yangi", at=NOW - timedelta(days=1), chat=DM2)
    await _index(session)
    result = await _search(
        session,
        "konteyner",
        date_from=(NOW - timedelta(days=40)).date(),
        date_to=(NOW - timedelta(days=20)).date(),
    )
    assert _hits(result) == ["konteyner eski"]


async def test_person_filter_includes_group_lines_by_them_and_mentions_of_them(session):
    await _chats(session)
    sardor = await _person(session, "Sardor")
    vali = await _person(session, "Vali")
    await _say(session, "narx kelishildi", person=sardor)
    await _say(
        session, "narx qimmat", person=sardor, chat=GROUP, at=NOW - timedelta(hours=2)
    )
    await _say(
        session, "Sardor narx aytdi", person=vali, chat=GROUP, at=NOW - timedelta(hours=4)
    )
    await _say(session, "narx boshqa", person=vali, chat=DM2, at=NOW - timedelta(hours=6))
    await _index(session)
    result = await _search(session, "narx", person=sardor)
    assert sorted(_hits(result)) == [
        "Sardor narx aytdi",
        "narx kelishildi",
        "narx qimmat",
    ]


async def test_context_lines_surround_the_hit(session):
    await _chats(session)
    for i in range(3):
        await _say(session, f"oldin {i}", at=NOW - timedelta(minutes=30 - i))
    await _say(session, "konteyner keldi", at=NOW - timedelta(minutes=20))
    for i in range(3):
        await _say(session, f"keyin {i}", at=NOW - timedelta(minutes=10 - i))
    await _index(session)
    [episode] = (await _search(session, "konteyner")).episodes
    assert [line.text for line in episode.lines] == [
        "oldin 0",
        "oldin 1",
        "oldin 2",
        "konteyner keldi",
        "keyin 0",
        "keyin 1",
        "keyin 2",
    ]
    assert [line.hit for line in episode.lines].count(True) == 1
    assert episode.where == "Sardor bilan shaxsiy chat"


async def test_episode_merges_hits_within_15_minutes(session):
    await _chats(session)
    await _say(session, "konteyner birinchi", at=NOW - timedelta(minutes=20))
    await _say(session, "konteyner ikkinchi", at=NOW - timedelta(minutes=10))
    await _say(session, "konteyner uchinchi", at=NOW - timedelta(hours=5))
    await _index(session)
    result = await _search(session, "konteyner")
    assert len(result.episodes) == 2


async def test_question_rows_are_never_returned(session):
    note = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text="konteyner qayerda?",
        meta={"kind": "question"},
    )
    session.add(note)
    await session.flush()
    await _index(session)
    assert (await _search(session, "konteyner")).is_empty()


class _Broken(Embedder):
    name = "broken"

    async def embed(self, texts):
        raise EmbeddingError("down")


async def test_embedding_failure_degrades_to_lexical(session):
    await _chats(session)
    await _say(session, "konteyner keldi")
    await _index(session, FakeEmbedder())
    result = await _search(session, "konteyner", _Broken())
    assert result.degraded == "semantic_unavailable"
    assert _hits(result) == ["konteyner keldi"]


async def test_empty_result_is_empty(session):
    result = await _search(session, "hech narsa yoq")
    assert result.is_empty() and result.episodes == [] and result.facts == []
