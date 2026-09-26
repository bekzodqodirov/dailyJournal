"""WP-55: passages — what was actually said, indexed for recall."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import health, passages
from tests.test_memories import FakeEmbedder

TZ = settings.tz
OLD = datetime.now(TZ) - timedelta(hours=3)
DM, GROUP = 5101, -5102
_ids = itertools.count(1)


async def _person(session, name="Akmal") -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _chats(session):
    session.add(m.ChatMonitor(tg_chat_id=DM, chat_type=ChatType.private, title="Akmal"))
    session.add(m.ChatMonitor(tg_chat_id=GROUP, chat_type=ChatType.group, title="Yuk"))
    await session.flush()


async def _row(
    session,
    text="salom",
    *,
    source=InteractionSource.telegram_userbot,
    chat=DM,
    person=None,
    out=False,
    at=OLD,
    meta=None,
    media=None,
    transcript=None,
):
    row = m.Interaction(
        source=source,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat if source is InteractionSource.telegram_userbot else None,
        occurred_at=at,
        raw_text=text,
        transcript=transcript,
        meta={"tg_message_id": next(_ids), **(meta or {})},
        media=media,
    )
    session.add(row)
    await session.flush()
    return row


async def _passages(session, interaction_id=None) -> list[m.Passage]:
    query = sa.select(m.Passage).order_by(m.Passage.interaction_id, m.Passage.chunk_no)
    if interaction_id is not None:
        query = query.where(m.Passage.interaction_id == interaction_id)
    return list(await session.scalars(query))


async def test_every_userbot_message_gets_one_passage(session):
    await _chats(session)
    akmal = await _person(session)
    rows = [await _row(session, f"xabar {i}", person=akmal) for i in range(3)]
    assert await passages.index_pending(session) == 3
    assert [p.interaction_id for p in await _passages(session)] == [r.id for r in rows]
    assert all(r.search_indexed_at is not None for r in rows)


async def test_window_and_question_rows_are_never_indexed_but_leave_the_queue(session):
    window = await _row(session, "window", meta={"kind": "window"})
    question = await _row(
        session,
        "savol?",
        source=InteractionSource.assistant_bot,
        meta={"kind": "question"},
    )
    await passages.index_pending(session)
    assert await _passages(session) == []
    assert window.search_indexed_at is not None and question.search_indexed_at is not None


async def test_otp_sms_is_not_indexed(session):
    otp = await _row(
        session,
        "Kod: 123456",
        source=InteractionSource.phone_sms,
        media={"type": "sms", "money": {"reason": "otp"}},
    )
    conflict = await _row(
        session,
        "Kod 1234, to'lov 50 000",
        source=InteractionSource.phone_sms,
        media={"type": "sms", "money": {"reason": "otp_conflict"}},
    )
    await passages.index_pending(session)
    assert await _passages(session, otp.id) == []
    assert otp.search_indexed_at is not None
    assert len(await _passages(session, conflict.id)) == 1


async def test_a_long_call_transcript_is_chunked_with_overlap(session, monkeypatch):
    monkeypatch.setattr(settings, "passage_chunk_chars", 200)
    monkeypatch.setattr(settings, "passage_chunk_overlap", 50)
    text = ". ".join(f"gap {i} konteyner haqida" for i in range(40))
    call = await _row(
        session,
        None,
        source=InteractionSource.phone_call,
        transcript=text,
        media={"type": "call_recording", "processed": True},
    )
    await passages.index_pending(session)
    chunks = await _passages(session, call.id)
    assert len(chunks) > 1
    assert [c.chunk_no for c in chunks] == list(range(len(chunks)))
    assert all(len(c.body) <= 200 for c in chunks)
    assert chunks[0].body[-20:] in chunks[1].body or chunks[1].body[:10] in chunks[0].body


async def test_voice_waits_for_its_transcript(session):
    await _chats(session)
    voice = await _row(
        session, None, at=datetime.now(TZ), media={"type": "voice", "processed": False}
    )
    await passages.index_pending(session)
    assert voice.search_indexed_at is None and await _passages(session) == []


async def test_setting_a_transcript_reindexes_through_the_listener(session):
    await _chats(session)
    voice = await _row(
        session, None, media={"type": "voice", "processed": True}, transcript="birinchi"
    )
    await passages.index_pending(session)
    assert voice.search_indexed_at is not None
    voice.transcript = "konteyner keldi"
    await session.flush()
    assert voice.search_indexed_at is None
    await passages.index_pending(session)
    [passage] = await _passages(session, voice.id)
    assert passage.body == "konteyner keldi"


async def test_attribution_dm_group_call(session):
    await _chats(session)
    akmal = await _person(session)
    dm_in = await _row(session, "a", person=akmal)
    dm_out = await _row(session, "b", person=akmal, out=True)
    group_in = await _row(session, "c", chat=GROUP, person=akmal)
    group_out = await _row(session, "d", chat=GROUP, out=True)
    call = await _row(
        session,
        "e",
        source=InteractionSource.phone_call,
        person=akmal,
        media={"type": "call_recording", "processed": True},
    )
    note = await _row(session, "f", source=InteractionSource.assistant_bot)
    await passages.index_pending(session)

    def who(row):
        [p] = [p for p in passages_ if p.interaction_id == row.id]
        return p.speaker_person_id, p.chat_person_id, p.from_owner

    passages_ = await _passages(session)
    assert who(dm_in) == (akmal.id, akmal.id, False)
    assert who(dm_out) == (None, akmal.id, True)
    assert who(group_in) == (akmal.id, None, False)
    assert who(group_out) == (None, None, True)
    assert who(call) == (None, akmal.id, False)
    assert who(note) == (None, None, True)


async def test_short_messages_are_lexical_only(session):
    await _chats(session)
    await _row(session, "ok")
    await _row(session, "konteyner bojxonada ushlab qolindi")
    await passages.index_pending(session)
    embedder = FakeEmbedder()
    assert await passages.embed_pending(session, embedder) == 1
    short, long = await _passages(session)
    assert short.embedding is None and long.embedding is not None


async def test_tsv_matches_prefix_query(session):
    await _chats(session)
    await _row(session, "Konteynerni bojxona ushlab qoldi")
    await passages.index_pending(session)
    hits = await session.scalar(
        sa.select(sa.func.count(m.Passage.id)).where(
            m.Passage.search_tsv.op("@@")(sa.func.to_tsquery("simple", "'konteyner':*"))
        )
    )
    assert hits == 1


async def test_deleting_an_interaction_removes_its_passages(session):
    await _chats(session)
    row = await _row(session, "konteyner")
    await passages.index_pending(session)
    await session.delete(row)
    await session.flush()
    assert await _passages(session) == []


async def test_search_down_counts_unembedded_passages(session):
    await _chats(session)
    await _row(session, "konteyner bojxonada ushlab qolindi")
    await passages.index_pending(session)
    await session.commit()
    status = await health.gather(session)
    assert status.embed_backlog >= 1 and status.embed_oldest_at is not None
