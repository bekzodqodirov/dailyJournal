"""Call-recording ingestion (Phase 2).

Scribe and the extractor are stubbed; filename parsing, hashing, dedupe,
person matching, retention and the database rows are all real.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import InteractionSource
from miya.services import call_recordings as cr
from miya.services import extraction as ex
from miya.services import ingest
from miya.services.transcription import Transcript, TranscriptionError

TZ = settings.tz


# --- filename parsing (pure) -------------------------------------------------


def test_samsung_name_with_contact_and_timestamp():
    parsed = cr.parse_filename(Path("Call recording Akmal aka_250817_143025.m4a"))
    assert parsed.counterparty == "Akmal aka"
    assert parsed.phone is None
    # %y parses "25" as 2025.
    assert parsed.recorded_at == datetime(2025, 8, 17, 14, 30, 25, tzinfo=TZ)


def test_samsung_name_with_phone_number():
    parsed = cr.parse_filename(Path("Call recording +998901234567_250817_143025.m4a"))
    assert parsed.phone == "998901234567"
    assert parsed.recorded_at is not None


def test_unrecognised_name_yields_nothing_but_does_not_fail():
    parsed = cr.parse_filename(Path("random-audio.mp3"))
    assert parsed.counterparty == "random-audio"
    assert parsed.recorded_at is None

    parsed = cr.parse_filename(Path("Voice 001_250817_143025.amr"))
    # A bare counter identifies nobody.
    assert parsed.counterparty is None
    assert parsed.recorded_at is not None


def test_impossible_timestamp_falls_back_to_none():
    parsed = cr.parse_filename(Path("Call recording Akmal_259999_999999.m4a"))
    assert parsed.recorded_at is None
    assert parsed.counterparty == "Akmal"


# --- readiness ---------------------------------------------------------------


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    directory = tmp_path / "call_recordings"
    directory.mkdir()
    monkeypatch.setattr(settings, "call_recordings_dir", str(directory))
    return directory


def _write_audio(directory: Path, name: str, *, age_seconds: float = 120) -> Path:
    path = directory / name
    path.write_bytes(b"RIFFfakeaudio" + name.encode())
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def test_syncthing_temp_and_fresh_files_are_not_ready(recordings_dir):
    ready = _write_audio(recordings_dir, "Call recording A_250817_143025.m4a")
    temp = _write_audio(recordings_dir, ".syncthing.Call recording B.m4a.tmp")
    fresh = _write_audio(
        recordings_dir, "Call recording C_250817_143026.m4a", age_seconds=1
    )
    not_audio = _write_audio(recordings_dir, "notes.txt")

    assert cr.is_ready_recording(ready) is True
    assert cr.is_ready_recording(temp) is False
    assert cr.is_ready_recording(fresh) is False
    assert cr.is_ready_recording(not_audio) is False


def test_an_empty_file_is_never_ready(recordings_dir):
    path = recordings_dir / "empty.m4a"
    path.touch()
    old = time.time() - 120
    os.utime(path, (old, old))
    assert cr.is_ready_recording(path) is False


# --- ingestion (real database, stubbed Scribe + extractor) -------------------


@pytest.fixture
def stub_scribe(monkeypatch):
    def _install(text="Akmalga 5 mln berdim", *, fail=False):
        class _Stub:
            name = "elevenlabs"
            model = "scribe_v1"

            async def transcribe(self, path, *, language_hint=None):
                if fail:
                    raise TranscriptionError("stubbed failure")
                return Transcript(text=text, language="uz", duration=42.0)

        monkeypatch.setattr(ingest, "get_transcriber", lambda: _Stub())

    return _install


@pytest.fixture
def stub_extract(monkeypatch):
    def _install(result=None):
        outcome = ex.ExtractionOutcome(
            result=result if result is not None else ex.ExtractionResult(),
            model=settings.extract_model,
            usage=None,
        )

        async def _extract(text, *, now=None):
            return outcome

        monkeypatch.setattr(ingest, "extract", _extract)

    return _install


async def test_a_recording_becomes_a_phone_call_interaction(
    session, recordings_dir, stub_scribe, stub_extract
):
    stub_scribe()
    stub_extract(
        ex.ExtractionResult(
            summary="Qarz haqida qo'ng'iroq",
            debts=[
                ex.ExtractedDebt(
                    direction="they_owe_me", person="Akmal", amount=5_000_000
                )
            ],
        )
    )
    path = _write_audio(recordings_dir, "Call recording Akmal aka_250817_143025.m4a")

    results = await cr.scan_directory(session)
    await session.commit()

    assert len(results) == 1
    interaction = results[0].interaction
    assert interaction.source is InteractionSource.phone_call
    assert interaction.transcript == "Akmalga 5 mln berdim"
    assert interaction.media["sha256"] == cr.file_sha256(path)
    assert interaction.media["counterparty"] == "Akmal aka"
    # Timestamp came from the filename, in Tashkent time.
    assert (
        interaction.occurred_at.astimezone(TZ).strftime("%y%m%d_%H%M%S")
        == "250817_143025"
    )

    debt = await session.scalar(sa.select(m.Debt))
    assert debt is not None
    assert debt.source_interaction_id == interaction.id


async def test_the_same_file_is_never_ingested_twice(
    session, recordings_dir, stub_scribe, stub_extract
):
    stub_scribe()
    stub_extract()
    _write_audio(recordings_dir, "Call recording Akmal_250817_143025.m4a")

    first = await cr.scan_directory(session)
    await session.commit()
    second = await cr.scan_directory(session)
    await session.commit()

    assert len(first) == 1
    assert second == []
    total = await session.scalar(sa.select(sa.func.count()).select_from(m.Interaction))
    assert total == 1


async def test_a_renamed_copy_is_still_deduped_by_hash(
    session, recordings_dir, stub_scribe, stub_extract
):
    stub_scribe()
    stub_extract()
    original = _write_audio(recordings_dir, "Call recording Akmal_250817_143025.m4a")
    await cr.scan_directory(session)
    await session.commit()

    # Syncthing conflict-copy: same bytes, different name.
    copy = recordings_dir / "Call recording Akmal_250817_143025.sync-conflict.m4a"
    copy.write_bytes(original.read_bytes())
    old = time.time() - 120
    os.utime(copy, (old, old))

    results = await cr.scan_directory(session)
    assert results == []


async def test_a_known_phone_number_links_the_call_to_the_person(
    session, recordings_dir, stub_scribe, stub_extract
):
    stub_scribe()
    stub_extract()
    person = m.Person(display_name="Akmal", aliases=[], phone="+998 90 123-45-67")
    session.add(person)
    await session.flush()

    _write_audio(recordings_dir, "Call recording +998901234567_250817_143025.m4a")
    results = await cr.scan_directory(session)
    await session.commit()

    assert results[0].interaction.person_id == person.id


async def test_a_failed_transcription_flags_review_but_never_retries_forever(
    session, recordings_dir, stub_scribe, stub_extract
):
    stub_scribe(fail=True)
    stub_extract()
    _write_audio(recordings_dir, "Call recording Akmal_250817_143025.m4a")

    first = await cr.scan_directory(session)
    await session.commit()

    assert len(first) == 1
    assert not first[0].ok
    assert first[0].interaction.needs_review is True

    # The hash row must stop the every-minute scan from re-billing Scribe.
    second = await cr.scan_directory(session)
    assert second == []


async def test_one_broken_file_does_not_stop_the_sweep(
    session, recordings_dir, stub_scribe, stub_extract, monkeypatch
):
    stub_scribe()
    stub_extract()
    _write_audio(recordings_dir, "Call recording A_250817_140000.m4a")
    _write_audio(recordings_dir, "Call recording B_250817_150000.m4a")

    real_ingest = cr.ingest_recording

    async def _flaky(session_, path):
        if "A_" in path.name:
            raise OSError("disk error")
        return await real_ingest(session_, path)

    monkeypatch.setattr(cr, "ingest_recording", _flaky)

    results = await cr.scan_directory(session)
    assert len(results) == 1  # B made it despite A blowing up


async def test_scan_of_a_missing_directory_is_a_quiet_noop(session, monkeypatch):
    monkeypatch.setattr(settings, "call_recordings_dir", "/nonexistent/path")
    assert await cr.scan_directory(session) == []


# --- retention ---------------------------------------------------------------


async def test_retention_deletes_only_old_audio(
    session, recordings_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "audio_retention_days", 90)
    media_dir = tmp_path / "bot_media"
    media_dir.mkdir()
    monkeypatch.setattr(type(settings), "media_dir", property(lambda self: media_dir))

    old_call = _write_audio(
        recordings_dir, "old_250101_120000.m4a", age_seconds=91 * 86400
    )
    new_call = _write_audio(recordings_dir, "new_250817_120000.m4a", age_seconds=86400)
    old_voice = _write_audio(media_dir, "note.ogg", age_seconds=91 * 86400)
    transcriptless = _write_audio(recordings_dir, "keep.txt", age_seconds=91 * 86400)

    deleted = await cr.purge_old_audio(session)

    assert deleted == 2
    assert not old_call.exists()
    assert not old_voice.exists()
    assert new_call.exists()
    assert transcriptless.exists()  # only audio suffixes are touched


async def test_retention_spares_audio_awaiting_review(
    session, recordings_dir, stub_scribe, stub_extract, monkeypatch
):
    """A failed transcription's audio is the only copy of that call — keep it."""
    monkeypatch.setattr(settings, "audio_retention_days", 90)
    stub_scribe(fail=True)
    stub_extract()
    path = _write_audio(
        recordings_dir, "Call recording Akmal_250101_120000.m4a", age_seconds=120
    )
    await cr.scan_directory(session)

    # Age the file past the retention window after ingestion.
    old = time.time() - 91 * 86400
    os.utime(path, (old, old))

    deleted = await cr.purge_old_audio(session)

    assert deleted == 0
    assert path.exists()
