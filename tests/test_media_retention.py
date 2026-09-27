"""WP-79: retention for photos, documents and videos — files only, off by
default, the text always kept."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import call_recordings as cr
from miya.services import health

NOW = datetime.now(settings.tz).replace(microsecond=0)


@pytest.fixture
def media(tmp_path, monkeypatch):
    recordings = tmp_path / "call_recordings"
    recordings.mkdir()
    monkeypatch.setattr(settings, "call_recordings_dir", str(recordings))
    root = tmp_path / "bot_media"
    root.mkdir()
    monkeypatch.setattr(settings, "media_retention_days", 30)
    monkeypatch.setattr(settings, "video_retention_days", 7)
    return root


def _file(root, name: str, *, days_old: float):
    path = root / name
    path.write_bytes(b"x")
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


async def _row(session, media: dict, **fields) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW - timedelta(days=60),
        raw_text=fields.pop("raw_text", "rasm izohi"),
        media=media,
        processed=fields.pop("processed", True),
        needs_review=fields.pop("needs_review", False),
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def test_an_old_ingested_photo_is_deleted_and_stamped(session, media):
    photo = _file(media, "a.jpg", days_old=40)
    row = await _row(session, {"type": "photo", "path": str(photo)}, transcript="GS367")

    assert await cr.purge_old_media(session, now=NOW) == 1

    assert not photo.exists()
    await session.refresh(row)
    assert row.media["purged_at"]
    assert row.raw_text == "rasm izohi" and row.transcript == "GS367"


async def test_needs_review_and_never_ingested_files_are_kept(session, media):
    flagged = _file(media, "b.jpg", days_old=40)
    await _row(session, {"type": "photo", "path": str(flagged)}, needs_review=True)
    stranger = _file(media, "c.pdf", days_old=40)

    assert await cr.purge_old_media(session, now=NOW) == 0
    assert flagged.exists() and stranger.exists()


async def test_an_old_video_goes_while_its_younger_audio_stays(session, media):
    video = _file(media, "n.mp4", days_old=10)
    audio = _file(media, "n.mp3", days_old=1)
    await _row(
        session,
        {"type": "video_note", "path": str(video), "audio_path": str(audio)},
    )

    assert await cr.purge_old_media(session, now=NOW) == 1
    assert not video.exists() and audio.exists()


async def test_zero_keeps_a_class_and_the_defaults_delete_nothing(
    session, media, monkeypatch
):
    photo = _file(media, "d.jpg", days_old=400)
    video = _file(media, "e.mp4", days_old=400)
    await _row(session, {"type": "photo", "path": str(photo)})
    await _row(session, {"type": "video", "path": str(video)})

    monkeypatch.setattr(settings, "media_retention_days", 0)
    assert await cr.purge_old_media(session, now=NOW) == 1
    assert photo.exists() and not video.exists()

    monkeypatch.setattr(settings, "video_retention_days", 0)
    again = _file(media, "f.mp4", days_old=400)
    await _row(session, {"type": "video", "path": str(again)})
    assert await cr.purge_old_media(session, now=NOW) == 0
    assert again.exists()


async def test_part_files_are_untouched(session, media):
    part = _file(media, "g.jpg.part", days_old=40)
    await _row(session, {"type": "photo", "path": str(part)})

    assert await cr.purge_old_media(session, now=NOW) == 0
    assert part.exists()


def test_disk_low_names_the_switch_only_when_both_are_off(monkeypatch):
    from tests.test_health import GB, _status

    monkeypatch.setattr(settings, "media_retention_days", 0)
    monkeypatch.setattr(settings, "video_retention_days", 0)
    [problem] = health.problems(_status(disk_low=True, disk_free_bytes=GB))
    assert "VIDEO_RETENTION_DAYS" in problem.text

    monkeypatch.setattr(settings, "video_retention_days", 30)
    [problem] = health.problems(_status(disk_low=True, disk_free_bytes=GB))
    assert "VIDEO_RETENTION_DAYS" not in problem.text
