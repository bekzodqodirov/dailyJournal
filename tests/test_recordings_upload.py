"""POST /v1/recordings — the Android companion's only way in (design §2).

The phone does not record calls; the OEM dialer does, and the app forwards the
file the dialer wrote. So the guarantees worth testing here are the ones a
retrying phone on a bad link depends on: the same recording never becomes two
interactions, a half-written upload is never visible to the sweep, and what the
phone knows about the call (who, when, which direction) survives all the way
into the interaction instead of being guessed out of a filename.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from miya.api.main import app
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import call_recordings as cr
from miya.services import extraction as ex
from miya.services import ingest
from miya.services.transcription import Transcript

TZ = settings.tz
TOKEN = "test-token"

AUDIO = b"RIFFfakeaudio-one-ten-second-test-call"
SHA = hashlib.sha256(AUDIO).hexdigest()
STARTED = "2026-09-06T14:30:25+05:00"


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    directory = tmp_path / "call_recordings"
    directory.mkdir()
    monkeypatch.setattr(settings, "call_recordings_dir", str(directory))
    return directory


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _meta(**overrides) -> dict:
    meta = {
        "schema": 1,
        "device_id": "b7f1c2e0-0000-4000-8000-000000000001",
        "call_id": "b7f1c2e0:4711",
        "sha256": SHA,
        "size_bytes": len(AUDIO),
        "started_at": STARTED,
        "duration_seconds": 412,
        "direction": "outgoing",
        "counterparty_name": "Akmal aka",
        "phone_e164": "+998901234567",
        "locale": "uz",
        "original_filename": "Call recording Akmal_260906_143025.m4a",
        "correlation": "call_log",
        "app_version": "1.0.3",
    }
    meta.update(overrides)
    return meta


def _post(client, *, audio=AUDIO, filename="Call recording Akmal.m4a", **overrides):
    mime = overrides.pop("mime", "audio/mp4")
    return client.post(
        "/v1/recordings",
        data={"meta": json.dumps(_meta(mime=mime, **overrides))},
        files={"audio": (filename, audio, mime)},
    )


# --- auth --------------------------------------------------------------------


def test_both_recording_routes_fail_closed_without_the_token(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as anonymous:
        upload = anonymous.post(
            "/v1/recordings",
            data={"meta": json.dumps(_meta())},
            files={"audio": ("a.m4a", AUDIO, "audio/mp4")},
        )
        assert upload.status_code == 401
        assert anonymous.post("/v1/recordings/probe", json={}).status_code == 401


def test_an_unconfigured_server_accepts_nothing(monkeypatch, recordings_dir):
    """No token in .env means no uploads, not uploads from anyone."""
    monkeypatch.setattr(settings, "api_bearer_token", "")
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer anything"})
        assert _post(c).status_code == 503
    assert list(recordings_dir.iterdir()) == []


# --- staging -----------------------------------------------------------------


async def test_an_upload_is_staged_with_its_sidecar_and_nothing_else(
    session, client, recordings_dir
):
    response = _post(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["sha256"] == SHA
    assert body["call_id"] == "b7f1c2e0:4711"
    assert body["staged_as"] == f"20260906-143025-{SHA[:12]}.m4a"

    audio = recordings_dir / body["staged_as"]
    assert audio.read_bytes() == AUDIO
    sidecar = json.loads((recordings_dir / f"{body['staged_as']}.json").read_text())
    assert sidecar["counterparty_name"] == "Akmal aka"
    assert sidecar["direction"] == "outgoing"
    assert sidecar["started_at"] == STARTED
    # Nothing half-written survives the request.
    assert sorted(p.name for p in recordings_dir.iterdir()) == [
        body["staged_as"],
        f"{body['staged_as']}.json",
    ]


async def test_the_client_filename_never_becomes_the_path(
    session, client, recordings_dir
):
    """Only the suffix is taken — a name is provenance, not a destination."""
    response = _post(client, filename="../../../etc/passwd.m4a")

    assert response.status_code == 202
    assert response.json()["staged_as"] == f"20260906-143025-{SHA[:12]}.m4a"
    assert [p.name for p in recordings_dir.iterdir() if p.suffix == ".m4a"] == [
        f"20260906-143025-{SHA[:12]}.m4a"
    ]


async def test_a_half_written_upload_is_invisible_to_the_sweep(recordings_dir):
    """The bytes land under `.part`, which the scanner already refuses."""
    partial = recordings_dir / f"20260906-143025-{SHA[:12]}.m4a.a1b2c3d4.part"
    partial.write_bytes(AUDIO)
    old = time.time() - 120
    os.utime(partial, (old, old))

    assert cr.is_ready_recording(partial) is False


# --- idempotency -------------------------------------------------------------


async def test_a_retried_upload_is_a_duplicate_not_a_second_recording(
    session, client, recordings_dir
):
    """The phone retries a lost 202; both answers are success, one file lands."""
    first = _post(client)
    second = _post(client)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["staged_as"] == first.json()["staged_as"]
    assert len(list(recordings_dir.glob("*.m4a"))) == 1


async def test_a_recording_already_ingested_is_not_uploaded_again(
    session, client, recordings_dir
):
    """The audio is purged after 90 days; the interaction outlives it."""
    session.add(
        m.Interaction(
            source=InteractionSource.phone_call,
            occurred_at=datetime.now(TZ),
            media={"type": "call_recording", "sha256": SHA, "call_id": "b7f1c2e0:4711"},
        )
    )
    await session.commit()

    response = _post(client)

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert list(recordings_dir.iterdir()) == []


async def test_a_re_encoded_copy_is_caught_by_the_call_id(
    session, client, recordings_dir
):
    """Different bytes, same call — the hash misses it, the call id does not."""
    session.add(
        m.Interaction(
            source=InteractionSource.phone_call,
            occurred_at=datetime.now(TZ),
            media={"type": "call_recording", "sha256": "0" * 64, "call_id": "dev7:4711"},
        )
    )
    await session.commit()

    other = b"re-encoded copy of the same conversation"
    response = _post(
        client,
        audio=other,
        sha256=hashlib.sha256(other).hexdigest(),
        size_bytes=len(other),
        call_id="dev7:4711",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert list(recordings_dir.iterdir()) == []


# --- rejection ---------------------------------------------------------------


async def test_a_non_audio_upload_is_refused_with_a_usable_message(
    session, client, recordings_dir
):
    response = _post(client, filename="notes.txt", mime="text/plain")

    assert response.status_code == 422
    assert "unsupported audio type" in response.json()["detail"]
    assert list(recordings_dir.iterdir()) == []


async def test_an_oversized_recording_is_refused_before_it_reaches_the_disk(
    session, client, recordings_dir, monkeypatch
):
    monkeypatch.setattr(cr, "RECORDING_UPLOAD_MAX_BYTES", 16)
    big = b"x" * 4096

    response = _post(
        client,
        audio=big,
        sha256=hashlib.sha256(big).hexdigest(),
        # The claimed size is a lie; the bytes actually arriving are counted.
        size_bytes=8,
    )

    assert response.status_code == 422
    assert "upload limit" in response.json()["detail"]
    assert list(recordings_dir.iterdir()) == []


async def test_a_corrupted_transfer_leaves_nothing_behind(
    session, client, recordings_dir
):
    response = _post(client, sha256="a" * 64)

    assert response.status_code == 422
    assert response.json()["detail"] == "sha256 mismatch: transfer corrupted"
    assert list(recordings_dir.iterdir()) == []


async def test_meta_that_cannot_be_trusted_is_refused(session, client, recordings_dir):
    assert _post(client, sha256="not-a-hash").status_code == 422
    broken = client.post(
        "/v1/recordings",
        data={"meta": "{not json"},
        files={"audio": ("a.m4a", AUDIO, "audio/mp4")},
    )
    assert broken.status_code == 422
    assert "not valid JSON" in broken.json()["detail"]
    assert list(recordings_dir.iterdir()) == []


async def test_a_phone_with_a_broken_clock_cannot_poison_the_report_day(
    session, client, recordings_dir
):
    """A call filed in 2031 would land in a day-bucket nobody ever reads."""
    response = _post(client, started_at="2031-01-01T09:00:00+05:00")

    assert response.status_code == 202
    sidecar = json.loads(
        (recordings_dir / f"{response.json()['staged_as']}.json").read_text()
    )
    assert sidecar["clock_suspect"] is True
    stamped = datetime.fromisoformat(sidecar["started_at"])
    assert abs(stamped - datetime.now(TZ)) < timedelta(minutes=5)


# --- probe -------------------------------------------------------------------


async def test_the_probe_reports_what_is_already_here(session, client, recordings_dir):
    """A staged file counts too — it is a minute away from an interaction."""
    _post(client)
    session.add(
        m.Interaction(
            source=InteractionSource.phone_call,
            occurred_at=datetime.now(TZ),
            media={"type": "call_recording", "sha256": "b" * 64, "call_id": "dev7:99"},
        )
    )
    await session.commit()

    body = client.post(
        "/v1/recordings/probe",
        json={"sha256": [SHA, "b" * 64, "c" * 64], "call_id": ["dev7:99", "dev7:100"]},
    ).json()

    assert body["known_sha256"] == sorted([SHA, "b" * 64])
    assert body["known_call_id"] == ["dev7:99"]


async def test_an_empty_probe_is_the_apps_connection_test(session, client):
    """It proves reachability *and* the token, which /health cannot."""
    response = client.post("/v1/recordings/probe", json={})
    assert response.status_code == 200
    assert response.json() == {"known_sha256": [], "known_call_id": []}


# --- the metadata reaches the interaction ------------------------------------


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Scribe and Haiku stubbed; everything between them is real."""
    seen: dict = {}

    class _Stub:
        name = "elevenlabs"
        model = "scribe_v1"

        async def transcribe(self, path, *, language_hint=None):
            seen["language_hint"] = language_hint
            return Transcript(text="Akmalga 5 mln berdim", language="uz", duration=0.0)

    async def _extract(text, *, now=None):
        seen["text"] = text
        seen["now"] = now
        return ex.ExtractionOutcome(
            result=ex.ExtractionResult(summary="qarz"),
            model=settings.extract_model,
            usage=None,
        )

    monkeypatch.setattr(ingest, "get_transcriber", lambda: _Stub())
    monkeypatch.setattr(ingest, "extract", _extract)
    return seen


async def test_what_the_phone_knew_reaches_the_interaction(
    session, client, recordings_dir, stub_pipeline
):
    """The four fields that replace guesswork, end to end through the sweep."""
    assert _post(client).status_code == 202

    results = await cr.scan_directory(session)
    await session.commit()

    assert len(results) == 1
    interaction = results[0].interaction
    # Direction: hardcoded `na` for every call before the app existed.
    assert interaction.direction is Direction.out
    # The call's own moment, with the phone's real offset — not the mtime of a
    # file that was written when the call ended.
    assert interaction.occurred_at.astimezone(TZ) == datetime.fromisoformat(STARTED)
    assert interaction.media["counterparty"] == "Akmal aka"
    assert interaction.media["phone"] == "998901234567"
    assert interaction.media["call_id"] == "b7f1c2e0:4711"
    assert interaction.media["duration_seconds"] == 412

    # The contact name resolved to a real Person and reached the extractor,
    # which is what attaches a debt to the right person.
    person = await session.scalar(sa.select(m.Person))
    assert person is not None
    assert person.display_name == "Akmal aka"
    assert interaction.person_id == person.id
    assert stub_pipeline["text"].startswith("[qo'ng'iroq → Akmal aka]")
    assert stub_pipeline["now"] == interaction.occurred_at

    # The locale became Scribe's language_code, and the call log's duration is
    # what was billed — Scribe reported none for this recording.
    assert stub_pipeline["language_hint"] == "uz"
    usage = await session.scalar(sa.select(m.UsageLog))
    assert usage is not None
    assert usage.operation == "transcribe"
    assert usage.audio_seconds == Decimal("412.00")


async def test_an_upload_is_never_ingested_twice_by_the_sweep(
    session, client, recordings_dir, stub_pipeline
):
    _post(client)

    first = await cr.scan_directory(session)
    await session.commit()
    second = await cr.scan_directory(session)
    await session.commit()

    assert len(first) == 1
    assert second == []
    total = await session.scalar(sa.select(sa.func.count()).select_from(m.Interaction))
    assert total == 1


async def test_a_sidecar_free_recording_still_falls_back_to_the_filename(
    session, recordings_dir, stub_pipeline
):
    """Syncthing keeps working: no sidecar, no structured metadata, no crash."""
    path = recordings_dir / "Call recording Akmal_250817_143025.m4a"
    path.write_bytes(b"synced by syncthing")
    stamped = datetime(2025, 8, 17, 14, 30, 25, tzinfo=TZ).timestamp()
    os.utime(path, (stamped, stamped))

    results = await cr.scan_directory(session)
    await session.commit()

    assert len(results) == 1
    assert results[0].interaction.direction is Direction.na
    assert results[0].interaction.media["counterparty"] == "Akmal"
    assert results[0].interaction.media["call_id"] is None
    # A name guessed from a filename must not create a Person.
    assert await session.scalar(sa.select(m.Person)) is None


async def test_a_file_the_database_already_has_is_not_rehashed_every_minute(
    session, recordings_dir, stub_pipeline, monkeypatch
):
    """Once the phone pushes continuously, per-sweep hashing is unbounded I/O."""
    assert Path(settings.call_recordings_dir) == recordings_dir
    path = recordings_dir / "Call recording Akmal_250817_143025.m4a"
    path.write_bytes(b"synced by syncthing")
    old = time.time() - 120
    os.utime(path, (old, old))

    await cr.scan_directory(session)
    await session.commit()

    hashed: list[Path] = []
    real = cr.file_sha256
    monkeypatch.setattr(cr, "file_sha256", lambda p, **kw: hashed.append(p) or real(p))

    assert await cr.scan_directory(session) == []
    assert hashed == []
