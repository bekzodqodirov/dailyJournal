"""WP-85: speaker turns in call transcripts."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import passages, transcription

# A diarised Scribe response, word by word (recorded shape, no network).
DIARIZED = {
    "language_code": "uzb",
    "text": "Assalomu alaykum. Konteyner qachon? Ertaga chiqadi.",
    "words": [
        {
            "text": "Assalomu",
            "type": "word",
            "start": 0.0,
            "end": 0.5,
            "speaker_id": "speaker_0",
        },
        {"text": " ", "type": "spacing", "speaker_id": "speaker_0"},
        {
            "text": "alaykum.",
            "type": "word",
            "start": 0.5,
            "end": 1.0,
            "speaker_id": "speaker_0",
        },
        {"text": " ", "type": "spacing", "speaker_id": "speaker_0"},
        {
            "text": "Konteyner",
            "type": "word",
            "start": 1.2,
            "end": 1.8,
            "speaker_id": "speaker_1",
        },
        {"text": " ", "type": "spacing", "speaker_id": "speaker_1"},
        {
            "text": "qachon?",
            "type": "word",
            "start": 1.8,
            "end": 2.2,
            "speaker_id": "speaker_1",
        },
        {"text": " ", "type": "spacing", "speaker_id": "speaker_1"},
        {
            "text": "Ertaga",
            "type": "word",
            "start": 2.5,
            "end": 3.0,
            "speaker_id": "speaker_0",
        },
        {"text": " ", "type": "spacing", "speaker_id": "speaker_0"},
        {
            "text": "chiqadi.",
            "type": "word",
            "start": 3.0,
            "end": 3.6,
            "speaker_id": "speaker_0",
        },
    ],
}


def test_diarized_payload_renders_speaker_lines():
    segments = transcription.segments_from_payload(DIARIZED)

    assert segments == [
        (1, "Assalomu alaykum."),
        (2, "Konteyner qachon?"),
        (1, "Ertaga chiqadi."),
    ]
    assert transcription.render_segments(segments) == (
        "[1-ovoz] Assalomu alaykum.\n[2-ovoz] Konteyner qachon?\n[1-ovoz] Ertaga chiqadi."
    )


def test_missing_speaker_ids_fall_back_to_plain_text():
    plain = {
        "text": "salom",
        "words": [{"text": "salom", "type": "word", "start": 0, "end": 1}],
    }
    assert transcription.segments_from_payload(plain) == []


async def test_non_call_audio_is_not_diarized(tmp_path, monkeypatch):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"ogg")
    sent = []

    async def fake_post(self, url, *, headers, data, files):
        sent.append(dict(data))
        return httpx.Response(200, json=DIARIZED, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    scribe = transcription.ElevenLabsScribe(api_key="k")

    voice = await scribe.transcribe(audio)
    call = await scribe.transcribe(audio, diarize=True)

    assert "diarize" not in sent[0] and sent[1]["diarize"] == "true"
    assert voice.text == DIARIZED["text"] and voice.segments == []
    assert call.text.startswith("[1-ovoz] Assalomu alaykum.")


async def test_a_passage_cites_the_specific_turn(session):
    call = m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.in_,
        occurred_at=datetime.now(settings.tz),
        raw_text="[qo'ng'iroq ← Akmal]",
        transcript=transcription.render_segments(
            transcription.segments_from_payload(DIARIZED)
        ),
        media={"type": "call", "processed": True},
        processed=True,
    )
    session.add(call)
    await session.flush()

    await passages.index_pending(session)

    rows = list(
        await session.scalars(
            sa.select(m.Passage)
            .where(m.Passage.interaction_id == call.id)
            .order_by(m.Passage.chunk_no)
        )
    )
    turn = next(p for p in rows if "Konteyner" in p.body)
    assert turn.body == "[2-ovoz] Konteyner qachon?"
    assert "qo'ng'iroq · 2-ovoz: [2-ovoz] Konteyner qachon?" in turn.embed_text


@pytest.fixture(autouse=True)
def _off(monkeypatch):
    monkeypatch.setattr(settings, "transcribe_diarize_calls", False)
