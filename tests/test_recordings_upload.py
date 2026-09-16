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

from miya.api import main
from miya.api.main import app
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import call_recordings as cr
from miya.services import extraction as ex
from miya.services import ingest, purge
from miya.services.transcription import Transcript

TZ = settings.tz
TOKEN = "test-token"

AUDIO = b"RIFFfakeaudio-one-ten-second-test-call"
SHA = hashlib.sha256(AUDIO).hexdigest()
STARTED = "2026-09-06T14:30:25+05:00"
DEVICE = "b7f1c2e0-0000-4000-8000-000000000001"
# "<device_id>:<CallLog._ID>", exactly as RecordingScanner.kt mints it.
CALL_ID = f"{DEVICE}:4711"


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    directory = tmp_path / "call_recordings"
    directory.mkdir()
    monkeypatch.setattr(settings, "call_recordings_dir", str(directory))
    return directory


@pytest.fixture
def no_spooling(monkeypatch):
    """Watches for the one thing the old stack did before checking the token.

    `UploadFile` is a SpooledTemporaryFile: under a mebibyte it is memory, over
    it `rollover()` writes the payload to a real file in the system temp dir —
    not the ./data volume, so it fills the container's writable layer and takes
    Postgres down with it. Recording every rollover is how a test can say
    "nothing reached the disk" and mean it.
    """
    import tempfile

    class _Watch:
        def __init__(self) -> None:
            self.rolled_over: list[int] = []

    watch = _Watch()
    real = tempfile.SpooledTemporaryFile.rollover

    def spy(self):
        watch.rolled_over.append(getattr(self, "_max_size", -1))
        return real(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", spy)
    return watch


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


class _FrozenDatetime:
    """`datetime` with a scripted `now()`; everything else is the real thing."""

    def __init__(self, clock) -> None:
        self._clock = clock

    def now(self, tz=None):
        return next(self._clock)

    def __getattr__(self, name):
        return getattr(datetime, name)


def _meta(**overrides) -> dict:
    meta = {
        "schema": 1,
        "device_id": DEVICE,
        "call_id": CALL_ID,
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


def test_both_recording_routes_fail_closed_without_the_token(
    monkeypatch, recordings_dir, no_spooling
):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as anonymous:
        upload = anonymous.post(
            "/v1/recordings",
            data={"meta": json.dumps(_meta())},
            files={"audio": ("a.m4a", AUDIO, "audio/mp4")},
        )
        assert upload.status_code == 401
        assert anonymous.post("/v1/recordings/probe", json={}).status_code == 401
    # The status code is the cheap half of this guarantee. The other half is
    # that the 401 was decided from the headers, so the payload never reached
    # a temp file, a spool or the recordings directory.
    assert list(recordings_dir.iterdir()) == []
    assert no_spooling.rolled_over == []


def test_an_unauthenticated_oversized_upload_never_reaches_the_disk(
    monkeypatch, recordings_dir, no_spooling
):
    """The pre-auth hole, pinned.

    FastAPI parses a multipart body before it solves router dependencies, and
    Starlette streams a file part into a SpooledTemporaryFile that rolls over
    to a real on-disk file at 1 MB — so an anonymous POST used to have its
    whole payload written to /tmp before `require_token` ever ran, with no cap
    at any layer. Both decisions now happen in the ASGI guard, above routing:
    the body is refused before a byte of it is received.
    """
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    payload = b"x" * (4 * 1024 * 1024)

    with TestClient(app) as anonymous:
        response = anonymous.post(
            "/v1/recordings",
            data={"meta": json.dumps(_meta())},
            files={"audio": ("big.m4a", payload, "audio/mp4")},
        )

    assert response.status_code == 401
    assert no_spooling.rolled_over == []
    assert list(recordings_dir.iterdir()) == []


def test_a_declared_oversize_is_refused_from_the_headers_alone(
    monkeypatch, recordings_dir, no_spooling
):
    """Content-Length over the cap: 413 before the receive channel is drained."""
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    monkeypatch.setattr(cr, "RECORDING_UPLOAD_MAX_BYTES", 1024)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        response = _post(c, audio=b"y" * 8192, size_bytes=8192)

    assert response.status_code == 413
    assert "upload limit" in response.json()["detail"]
    assert no_spooling.rolled_over == []
    assert list(recordings_dir.iterdir()) == []


def test_a_device_token_can_upload_and_nothing_else(monkeypatch, recordings_dir):
    """An extracted APK must not be able to read the owner's debts back out."""
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    monkeypatch.setattr(settings, "upload_tokens", "phone:device-secret")
    with TestClient(app) as device:
        device.headers.update({"Authorization": "Bearer device-secret"})
        assert device.post("/v1/recordings/probe", json={}).status_code == 200
        assert device.get("/v1/config").status_code == 401
        assert device.post("/v1/ask", json={"question": "qarz?"}).status_code == 401


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
    assert body["call_id"] == CALL_ID
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


async def test_a_multi_megabyte_recording_arrives_byte_for_byte(
    session, client, recordings_dir, no_spooling
):
    """The realistic size, through the hand-rolled parser.

    A real Samsung m4a is tens of megabytes and reaches the server as hundreds
    of slices with boundaries falling anywhere; the sha256 check is what proves
    they were reassembled in order and with nothing of the multipart framing
    left in. Nothing spools on the way: the bytes go from the socket to the
    staging file.
    """
    big = bytes(range(256)) * 12_000  # ~3 MB, and not compressible into a pattern
    response = _post(
        client,
        audio=big,
        sha256=hashlib.sha256(big).hexdigest(),
        size_bytes=len(big),
    )

    assert response.status_code == 202
    assert (recordings_dir / response.json()["staged_as"]).read_bytes() == big
    assert no_spooling.rolled_over == []


async def test_a_bloated_meta_part_is_refused(session, client, recordings_dir):
    """`meta` is a small JSON object; it must not become a way to make us buffer."""
    response = client.post(
        "/v1/recordings",
        data={"meta": json.dumps(_meta(recorded_by="x" * 200_000))},
        files={"audio": ("a.m4a", AUDIO, "audio/mp4")},
    )

    assert response.status_code == 413
    assert list(recordings_dir.iterdir()) == []


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


async def test_a_half_written_upload_is_invisible_to_the_sweep(
    session, client, recordings_dir, monkeypatch
):
    """Asked of the handler, not of a filename a test author typed.

    The staging name is the handler's own; if it ever stopped being one the
    scanner refuses, this test sees it, which an assertion about a hand-written
    `.part` name could not.
    """
    seen: dict = {}
    real = main._write_chunk

    def spy(fh, digest, chunk):
        real(fh, digest, chunk)
        fh.flush()
        present = sorted(recordings_dir.iterdir())
        seen["present"] = [p.name for p in present]
        seen["ready"] = [p.name for p in present if cr.is_ready_recording(p)]

    monkeypatch.setattr(main, "_write_chunk", spy)
    assert _post(client).status_code == 202

    # The bytes really were on disk mid-flight …
    assert seen["present"], "the handler staged nothing"
    assert all(name.endswith(".part") for name in seen["present"])
    # … and the sweep would have ignored every one of them.
    assert seen["ready"] == []


async def test_an_abandoned_staging_file_is_reaped_by_the_sweep(session, recordings_dir):
    """A killed process leaves up to 200 MB behind that nothing else removes."""
    abandoned = recordings_dir / f"20260906-143025-{SHA[:12]}.m4a.a1b2c3d4.part"
    abandoned.write_bytes(AUDIO)
    stale = time.time() - 2 * cr.STALE_UPLOAD_SECONDS
    os.utime(abandoned, (stale, stale))
    fresh = recordings_dir / f"20260906-143026-{SHA[:12]}.m4a.b2c3d4e5.part"
    fresh.write_bytes(AUDIO)

    assert await cr.scan_directory(session) == []

    assert not abandoned.exists()
    assert fresh.exists(), "an upload still in flight must survive the sweep"


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
            media={"type": "call_recording", "sha256": SHA, "call_id": CALL_ID},
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
            occurred_at=datetime.fromisoformat(STARTED),
            media={"type": "call_recording", "sha256": "0" * 64, "call_id": CALL_ID},
        )
    )
    await session.commit()

    other = b"re-encoded copy of the same conversation"
    response = _post(
        client,
        audio=other,
        sha256=hashlib.sha256(other).hexdigest(),
        size_bytes=len(other),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert list(recordings_dir.iterdir()) == []


async def test_a_reused_call_id_from_a_different_call_is_not_a_duplicate(
    session, client, recordings_dir
):
    """The one case where "duplicate" destroys a recording.

    `CallLog._ID` restarts at 1 when the call log's storage is cleared, while
    the companion's device id survives — they are different apps — so a call
    made afterwards can inherit a call_id that is already on file. The phone
    treats a duplicate verdict as success: it marks the row done and may delete
    the source. A call_id alone must therefore never be enough; the two have to
    agree about when the call happened as well.
    """
    session.add(
        m.Interaction(
            source=InteractionSource.phone_call,
            occurred_at=datetime.fromisoformat(STARTED) - timedelta(days=200),
            media={"type": "call_recording", "sha256": "0" * 64, "call_id": CALL_ID},
        )
    )
    await session.commit()

    response = _post(client)

    assert response.status_code == 202, "a new recording was thrown away"
    assert (recordings_dir / response.json()["staged_as"]).read_bytes() == AUDIO


async def test_a_call_id_from_another_device_is_refused(session, client, recordings_dir):
    """call_id is "<device_id>:<_ID>" — one handset cannot veto another's."""
    response = _post(client, call_id="some-other-device:4711")

    assert response.status_code == 422
    assert "device_id" in response.json()["detail"]
    assert list(recordings_dir.iterdir()) == []


async def test_a_broken_clock_does_not_break_idempotency(
    session, client, recordings_dir, monkeypatch
):
    """The retry-stability of the staged name, in the branch that breaks it.

    A clock-suspect upload gets a server-side `now()` for its occurred_at. If
    that value also picked the filename, every retry from that handset would
    land on a new name a second later, `audio_path.exists()` would never match,
    and the volume would fill with copies — copies retention then refuses to
    delete and the sweep re-hashes every minute forever.
    """

    def ticking(start: datetime):
        """A clock that has moved on between every retry, as a real one would."""
        while True:
            yield start
            start += timedelta(seconds=41)

    monkeypatch.setattr(
        main, "datetime", _FrozenDatetime(ticking(datetime(2026, 9, 6, 12, 0, tzinfo=TZ)))
    )

    statuses = [
        _post(client, started_at="2031-01-01T09:00:00+05:00").status_code
        for _ in range(3)
    ]

    assert statuses == [202, 200, 200]
    assert [p.name for p in sorted(recordings_dir.glob("*.m4a"))] == [
        f"20310101-090000-{SHA[:12]}.m4a"
    ]
    sidecar = json.loads(
        (recordings_dir / f"20310101-090000-{SHA[:12]}.m4a.json").read_text()
    )
    # The correction still happens — it just lives where it belongs.
    assert sidecar["clock_suspect"] is True
    assert sidecar["started_at"] == "2026-09-06T12:00:00+05:00"


# --- rejection ---------------------------------------------------------------


async def test_a_non_audio_upload_is_refused_with_a_usable_message(
    session, client, recordings_dir
):
    response = _post(client, filename="notes.txt", mime="text/plain")

    assert response.status_code == 422
    assert "unsupported audio type" in response.json()["detail"]
    assert list(recordings_dir.iterdir()) == []


async def test_an_oversized_recording_is_cut_off_at_the_cap(
    session, client, recordings_dir, monkeypatch, no_spooling
):
    """Over a mebibyte, which is where the old stack silently spooled to disk.

    The declared size is a lie here, so the only thing that can stop this is
    counting the bytes as they arrive. Nothing is left behind, and nothing was
    ever spooled: the audio part is streamed, never buffered into a temp file.
    """
    monkeypatch.setattr(cr, "RECORDING_UPLOAD_MAX_BYTES", 64 * 1024)
    big = b"x" * (2 * 1024 * 1024)

    response = _post(
        client,
        audio=big,
        sha256=hashlib.sha256(big).hexdigest(),
        # The claimed size is a lie; the bytes actually arriving are counted.
        size_bytes=8,
    )

    assert response.status_code == 413
    assert "upload limit" in response.json()["detail"]
    assert no_spooling.rolled_over == []
    assert list(recordings_dir.iterdir()) == []


async def test_a_probe_body_is_bounded_too(session, client):
    """`await request.body()` is unbounded; the guard is what stops it."""
    response = client.post(
        "/v1/recordings/probe",
        content=json.dumps({"sha256": ["a" * 64] * 40_000}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


async def test_an_impossible_duration_is_refused_at_the_edge(
    session, client, recordings_dir
):
    """This number bills the transcription; unbounded, it corrupts the ledger."""
    response = _post(client, duration_seconds=999_999_999)

    assert response.status_code == 422
    assert "duration_seconds" in response.json()["detail"]
    assert list(recordings_dir.iterdir()) == []


async def test_a_sidecar_orphaned_by_a_failed_rename_is_cleaned_up(
    session, client, recordings_dir, monkeypatch
):
    """The sidecar names the counterparty; it must not outlive its audio."""
    real = os.replace

    def fail_on_audio(src, dst):
        if str(dst).endswith(".json"):
            return real(src, dst)
        raise OSError("no space left on device")

    monkeypatch.setattr(main.os, "replace", fail_on_audio)
    with pytest.raises(OSError):
        _post(client)

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
    assert interaction.media["call_id"] == CALL_ID
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


# --- the sidecar is part of the recording, everywhere it is deleted ----------


async def test_retention_deletes_the_sidecar_with_the_audio(
    session, client, recordings_dir, stub_pipeline, monkeypatch
):
    """The audio says a name out loud; the sidecar writes it down."""
    assert _post(client).status_code == 202
    await cr.scan_directory(session)
    await session.commit()
    old = time.time() - 200 * 86400
    for path in recordings_dir.iterdir():
        os.utime(path, (old, old))

    deleted = await cr.purge_old_audio(session)

    assert deleted == 1
    assert list(recordings_dir.iterdir()) == [], "the counterparty is still on disk"


async def test_unut_leaves_no_trace_of_the_person_on_disk(
    session, client, recordings_dir, stub_pipeline
):
    """/unut is the owner asking to forget someone, not to forget the audio."""
    assert _post(client).status_code == 202
    await cr.scan_directory(session)
    await session.commit()
    person = await session.scalar(sa.select(m.Person))
    assert person is not None
    # Everything the owner asked to forget is really in those files.
    sidecars = list(recordings_dir.glob("*.json"))
    assert len(sidecars) == 1
    assert "Akmal aka" in sidecars[0].read_text()
    assert "+998901234567" in sidecars[0].read_text()

    plan = await purge.plan_person(session, person)
    result = await purge.execute(session, plan)
    await session.commit()

    assert result.interactions == 1
    assert list(recordings_dir.iterdir()) == []
