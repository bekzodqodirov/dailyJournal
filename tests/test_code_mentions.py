"""WP-39: every GS code and waybill in stored text, indexed for exact recall."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import codes, ingest
from miya.services import extraction as ex
from miya.services.persistence import apply_extraction
from miya.services.vision import VisionResult


async def _note(session, text: str | None = "salom", **fields) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(settings.tz),
        raw_text=text,
        **fields,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _mentions(session, interaction_id: int | None = None) -> list[tuple[str, str]]:
    query = sa.select(m.CodeMention.kind, m.CodeMention.code).order_by(m.CodeMention.id)
    if interaction_id is not None:
        query = query.where(m.CodeMention.interaction_id == interaction_id)
    return [tuple(row) for row in (await session.execute(query)).all()]


async def test_a_note_indexes_its_codes_once(session):
    note = await _note(session, "GS367 va GS412ga 3 ta karobka, YW26-004715")
    assert await codes.index_interaction(session, note) == 3
    assert await _mentions(session, note.id) == [
        ("client", "GS367"),
        ("client", "GS412"),
        ("waybill", "YW26-004715"),
    ]
    assert note.codes_indexed_at is not None
    await codes.index_interaction(session, note)
    assert len(await _mentions(session, note.id)) == 3


async def test_windows_and_questions_are_stamped_not_indexed(session):
    window = await _note(session, "GS367", meta={"kind": "window"})
    question = await _note(session, "GS368", meta={"kind": "question"})
    assert await codes.index_interaction(session, window) == 0
    assert await codes.index_interaction(session, question) == 0
    assert window.codes_indexed_at is not None
    assert await _mentions(session) == []


async def test_a_changed_transcript_is_indexed_again(session):
    voice = await _note(session, None, media={"type": "voice"})
    await codes.index_pending(session)
    assert voice.codes_indexed_at is not None
    voice.transcript = "GS367 yuki keldi"
    await session.flush()
    assert voice.codes_indexed_at is None
    await codes.index_pending(session)
    assert await _mentions(session, voice.id) == [("client", "GS367")]


async def test_a_vision_description_is_indexed_again(session, monkeypatch, tmp_path):
    photo = await _note(session, None, media={"type": "photo"})
    await codes.index_interaction(session, photo)

    async def describe(path):
        return VisionResult(text="Stiker: GS367, YW26-004715", model="x")

    monkeypatch.setattr(ingest, "describe_image", describe)
    await ingest.describe_into(session, photo, tmp_path / "x.jpg")
    await session.flush()
    assert photo.codes_indexed_at is None
    await codes.index_pending(session)
    assert await _mentions(session, photo.id) == [
        ("client", "GS367"),
        ("waybill", "YW26-004715"),
    ]


async def test_deleting_the_interaction_takes_its_mentions(session):
    note = await _note(session, "GS367")
    await codes.index_interaction(session, note)
    await session.delete(note)
    await session.flush()
    assert await _mentions(session) == []


async def test_index_pending_respects_the_limit_and_the_order(session):
    first = await _note(session, "GS1")
    second = await _note(session, "GS2")
    third = await _note(session, "GS3")
    assert await codes.index_pending(session, limit=2) == 2
    assert first.codes_indexed_at is not None and second.codes_indexed_at is not None
    assert third.codes_indexed_at is None


async def test_at_most_two_hundred_mentions(session):
    note = await _note(session, " ".join(f"GS{i}" for i in range(1, 301)))
    assert await codes.index_interaction(session, note) == 200


async def test_a_fact_carries_its_codes_as_tags(session):
    note = await _note(session, "x")
    await apply_extraction(
        session, note, ex.ExtractionResult(facts=["GS367 yuklari kechikdi"])
    )
    [memory] = list(await session.scalars(sa.select(m.Memory)))
    assert "GS367" in memory.tags
