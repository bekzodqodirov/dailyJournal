"""Regression tests for the pre-merge review of the audit-fix commit.

The audit fixes were themselves reviewed before merging, and these pin what
that pass found. Each test names the owner-visible consequence of the bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Direction,
    EventSource,
    EventStatus,
    InteractionSource,
    WindowStatus,
)
from miya.services import gcal, windows
from miya.services.media_policy import MediaKind
from miya.userbot.main import _suffix_for

TZ = settings.tz


# --- gcal: what counts as a deletion -----------------------------------------


class _Api(gcal.CalendarAPI):
    """Records the moment list_events was called, so a push racing a pull can
    be simulated precisely."""

    def __init__(self, items, on_list=None):
        self.items = items
        self.on_list = on_list

    async def list_events(self, time_min, time_max):
        if self.on_list is not None:
            await self.on_list()
        return self.items

    async def insert_event(self, body):  # pragma: no cover - pull never inserts
        raise AssertionError("pull must never insert")


async def test_an_event_google_still_has_survives_an_unparseable_start(session):
    """A malformed start time is not a deletion.

    The event is still in Google; cancelling it locally would silently drop a
    real meeting from the plan and the reminders.
    """
    start = datetime.now(TZ) + timedelta(days=2)
    good = {
        "id": "keep-me",
        "summary": "Uchrashuv",
        "status": "confirmed",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    await gcal.pull(session, _Api([good]))
    await session.flush()

    # Next pull: Google returns the same event, but with a start MIYA cannot
    # parse (a field it does not understand, a malformed value).
    broken = {**good, "start": {"dateTime": "not-a-timestamp"}}
    await gcal.pull(session, _Api([broken]))
    await session.flush()

    event = await session.scalar(
        sa.select(m.Event).where(m.Event.gcal_event_id == "keep-me")
    )
    assert event.status is EventStatus.planned


async def test_a_meeting_miya_pushed_is_cancelled_when_google_loses_it(session):
    """Deletion detection covers pushed events too — they live in the same
    calendar and the owner deletes them the same way."""
    start = datetime.now(TZ) + timedelta(days=1)
    session.add(
        m.Event(
            title="Bojxona uchrashuvi",
            start_at=start,
            source=EventSource.extracted,
            gcal_event_id="pushed-1",
            status=EventStatus.planned,
        )
    )
    await session.flush()

    await gcal.pull(session, _Api([]))
    await session.flush()

    event = await session.scalar(
        sa.select(m.Event).where(m.Event.gcal_event_id == "pushed-1")
    )
    assert event.status is EventStatus.cancelled


async def test_an_event_pushed_while_the_pull_is_in_flight_is_not_cancelled(session):
    """gcal_push runs every five minutes and can land mid-pull.

    The response predates the push, so the new event is missing from it — but
    it exists in Google. Cancelling it would delete a meeting seconds after
    creating it.
    """
    start = datetime.now(TZ) + timedelta(days=1)

    async def _push_during_the_call():
        session.add(
            m.Event(
                title="Yangi uchrashuv",
                start_at=start,
                source=EventSource.extracted,
                gcal_event_id="raced-1",
                status=EventStatus.planned,
            )
        )
        await session.flush()

    await gcal.pull(session, _Api([], on_list=_push_during_the_call))
    await session.flush()

    event = await session.scalar(
        sa.select(m.Event).where(m.Event.gcal_event_id == "raced-1")
    )
    assert event.status is EventStatus.planned


# --- windows: truncate, never skip -------------------------------------------


async def _msg(session, chat, *, minutes_ago, text=None, media=None):
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=chat,
        occurred_at=datetime.now(TZ) - timedelta(minutes=minutes_ago),
        raw_text=text,
        media=media,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def test_a_pending_voice_note_holds_back_the_messages_after_it(session):
    """The backlog is cut at the unsettled message, not filtered around it.

    Skipping it would window the messages on either side together — the voice
    note would later form a stray window of its own, out of order and stripped
    of the conversation it belongs to.
    """
    now = datetime.now(TZ)
    chat = -100777
    await _msg(session, chat, minutes_ago=50, text="yuk qachon ketadi")
    await _msg(session, chat, minutes_ago=45, media={"type": "voice", "processed": False})
    await _msg(session, chat, minutes_ago=40, text="rahmat")

    grouped = await windows._unclaimed_by_chat(session, now=now)
    texts = [i.raw_text for i in grouped.get(chat, [])]
    assert texts == ["yuk qachon ketadi"]


async def test_a_chat_whose_first_message_is_still_transcribing_waits(session):
    now = datetime.now(TZ)
    chat = -100778
    await _msg(session, chat, minutes_ago=5, media={"type": "voice", "processed": False})
    await _msg(session, chat, minutes_ago=4, text="keyingi xabar")

    grouped = await windows._unclaimed_by_chat(session, now=now)
    assert chat not in grouped


async def test_the_hour_cutoff_releases_a_download_that_crashed(session):
    """Media stuck unprocessed forever must not wedge the chat permanently."""
    now = datetime.now(TZ)
    chat = -100779
    await _msg(
        session, chat, minutes_ago=180, media={"type": "voice", "processed": False}
    )
    await _msg(session, chat, minutes_ago=170, text="keyingi xabar")

    grouped = await windows._unclaimed_by_chat(session, now=now)
    assert len(grouped.get(chat, [])) == 2


# --- batch: an unreadable stream cannot wedge a window forever ---------------


async def test_a_permanently_broken_result_stream_falls_back_to_real_time(
    session, monkeypatch
):
    """Draining the stream before applying protects the advisory lock, but a
    stream that fails on every poll must not leave the conversation in
    `submitted` for good."""
    import anthropic

    from miya.services import batch as batch_svc

    window = m.ConversationWindow(
        tg_chat_id=-100888,
        started_at=datetime.now(TZ) - timedelta(hours=2),
        ended_at=datetime.now(TZ) - timedelta(hours=1),
        message_count=2,
        char_count=40,
        text="[2026-08-19 10:00] [THEM (Akmal)] 5 mln qarz oldim",
        status=WindowStatus.submitted,
        custom_id="w-broken-stream",
        batch_id="batch_broken",
        attempts=0,
    )
    session.add(window)
    await session.flush()

    def _api_error():
        return anthropic.APIConnectionError(request=None)

    class _Batches:
        async def retrieve(self, batch_id):
            return type("B", (), {"processing_status": "ended", "id": batch_id})()

        async def results(self, batch_id):
            raise _api_error()

    class _Client:
        messages = type("M", (), {"batches": _Batches()})()

    monkeypatch.setattr(batch_svc, "get_client", lambda: _Client())

    extracted = {}

    async def _fake_extract(text, *, now=None):
        extracted["called"] = True
        from miya.services.extraction import ExtractionOutcome, ExtractionResult

        return ExtractionOutcome(
            result=ExtractionResult(summary="qarz haqida"),
            model=settings.extract_model,
        )

    monkeypatch.setattr(batch_svc, "extract", _fake_extract)

    for _ in range(settings.batch_max_attempts):
        await batch_svc.collect_batch(session, "batch_broken")
    await session.flush()

    assert (
        window.status is not WindowStatus.submitted
    ), "a stream that never becomes readable must not wedge the window"
    assert extracted.get("called"), "the ladder must end in a real-time extraction"


# --- userbot: an audio file keeps its real container -------------------------


def test_a_shared_audio_file_keeps_its_real_extension():
    """Scribe is handed the bytes with the extension they actually are."""
    assert _suffix_for(MediaKind.audio, "qongiroq.m4a") == ".m4a"
    assert _suffix_for(MediaKind.audio, "note.ogg") == ".ogg"
    # A voice note has no filename; the recorded container is the right guess.
    assert _suffix_for(MediaKind.voice, None) == ".ogg"
    assert _suffix_for(MediaKind.audio, None) == ".mp3"


# --- worker catch-up: a startup path that can never kill the worker ----------


async def test_catch_up_recovers_a_report_missed_across_midnight(session, monkeypatch):
    """The headline case: the VPS is down from 18:00 to the next morning.

    Yesterday's 19:00 report never ran, and checking only "today" would never
    notice — the owner simply loses that day.
    """
    from miya.worker import main as worker

    morning = datetime.now(TZ).replace(hour=8, minute=0, second=0, microsecond=0)
    yesterday = (morning - timedelta(days=1)).date()

    day = await worker._missed_report_day(morning)
    assert day == yesterday


async def test_a_lunchtime_hisobot_does_not_mask_the_missed_evening_report(
    session, monkeypatch
):
    """`/hisobot` writes a row for the same date; the row alone proves nothing."""
    from miya.worker import main as worker

    evening = datetime.now(TZ).replace(hour=20, minute=0, second=0, microsecond=0)
    lunchtime = evening.replace(hour=12)

    session.add(
        m.DailyReport(
            report_date=evening.date(),
            content="lunchtime /hisobot",
            stats={},
            created_at=lunchtime,
        )
    )
    await session.commit()
    try:
        # The row exists, but it predates REPORT_TIME — the evening report was
        # still missed.
        assert await worker._missed_report_day(evening) == evening.date()

        # A row written after REPORT_TIME does settle it.
        await session.execute(
            sa.update(m.DailyReport)
            .where(m.DailyReport.report_date == evening.date())
            .values(created_at=evening)
        )
        await session.commit()
        assert await worker._missed_report_day(evening) is None
    finally:
        await session.execute(
            sa.delete(m.DailyReport).where(m.DailyReport.report_date == evening.date())
        )
        await session.commit()


def test_a_backup_missed_by_half_an_hour_is_still_recovered(monkeypatch, tmp_path):
    """The old "older than 25 hours" test skipped exactly the case it targeted.

    Down at 03:30, back at 04:00: yesterday's backup is 24.5 hours old, so the
    run was skipped and the real gap stretched to 48 hours.
    """
    import asyncio
    import os

    from miya.services import backup as backup_svc
    from miya.worker import main as worker

    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_time", "03:30")

    now = datetime.now(TZ).replace(hour=4, minute=0, second=0, microsecond=0)
    yesterdays = now - timedelta(hours=24, minutes=30)
    stale = tmp_path / f"miya-20260818-033000{backup_svc.BACKUP_SUFFIX}"
    stale.write_bytes(b"x")
    os.utime(stale, (yesterdays.timestamp(), yesterdays.timestamp()))

    assert asyncio.run(worker._backup_is_missing(now)) is True

    # A backup taken after today's scheduled time settles it.
    fresh = tmp_path / f"miya-20260819-033000{backup_svc.BACKUP_SUFFIX}"
    fresh.write_bytes(b"x")
    taken = now.replace(minute=31, hour=3)
    os.utime(fresh, (taken.timestamp(), taken.timestamp()))
    assert asyncio.run(worker._backup_is_missing(now)) is False


async def test_catch_up_never_takes_the_worker_down(monkeypatch):
    """catch_up runs before the worker settles into its loop.

    An exception there is a crash loop under `restart: unless-stopped`, so
    every branch must be caught — including the ones outside the inner jobs.
    """
    from miya.worker import main as worker

    async def _explode(*args, **kwargs):
        raise RuntimeError("the database is not up yet")

    def _explode_sync(*args, **kwargs):
        raise OSError("/data/backups is not writable")

    monkeypatch.setattr(worker, "_missed_report_day", _explode)
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    monkeypatch.setattr(worker.backup, "backup_dir", _explode_sync)

    # Must return, not raise.
    await worker.catch_up(bot=None)


# --- a blank API key degrades, it does not explode ---------------------------


def test_an_empty_api_key_raises_a_type_every_caller_already_handles(monkeypatch):
    """The SDK's own failure for a blank key is a bare TypeError raised deep
    inside header building, which no fallback path expects.

    Found by running the worker for real: a blank ANTHROPIC_API_KEY took the
    whole daily report down instead of degrading to its data block.
    """
    from miya.services import extraction

    monkeypatch.setattr(settings, "anthropic_api_key", "   ")
    monkeypatch.setattr(extraction, "_client", None)

    try:
        extraction.get_client()
    except extraction.AnthropicUnavailable:
        pass
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("a blank key must fail before the SDK is reached")

    # And it is a member of the tuple every fallback catches.
    assert isinstance(extraction.AnthropicUnavailable("x"), extraction.API_FAILURES)


async def test_the_daily_report_still_arrives_without_an_api_key(session, monkeypatch):
    """No key means the deterministic data block *is* the report — never nothing."""
    from miya.services import extraction, reports

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(extraction, "_client", None)

    content = await reports.generate_report(session)
    await session.flush()

    assert "HISOBOT KUNI" in content
    stored = await session.scalar(
        sa.select(m.DailyReport.content).where(
            m.DailyReport.report_date == datetime.now(TZ).date()
        )
    )
    assert stored == content


async def test_the_planner_still_answers_without_an_api_key(session, monkeypatch):
    from miya.services import extraction, planner

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(extraction, "_client", None)

    plan = await planner.plan_tomorrow(session)
    assert plan.strip(), "the planner must fall back, not return nothing"
