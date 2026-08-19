"""Regression tests for the production-readiness audit fixes.

Each test pins one bug found by the audit pass: if a future change reverts the
fix, the test names exactly what breaks for the owner.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    Direction,
    EventStatus,
    InteractionSource,
    TransactionType,
)
from miya.services import call_recordings, gcal, queries, windows
from miya.services.chats import TELEGRAM_SERVICE_ID, DialogInfo, default_monitor_enabled
from miya.services.extraction import ExtractionResult
from miya.services.media_policy import MediaKind, plan_for
from miya.services.people import resolve_person
from miya.services.persistence import apply_extraction

TZ = settings.tz


# --- spec §5: the summary reaches the memory store ---------------------------


async def test_the_interaction_summary_becomes_a_memory(session):
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(TZ),
        raw_text="Akmal bilan yuk haqida gaplashdik",
    )
    session.add(interaction)
    await session.flush()

    result = ExtractionResult(
        summary="Akmal bilan Guangzhou yuki muhokama qilindi",
        facts=["Yuk 25-avgustda jo'naydi"],
    )
    await apply_extraction(session, interaction, result)
    await session.flush()

    contents = set(await session.scalars(sa.select(m.Memory.content)))
    # Both the fact and the summary must be searchable later.
    assert "Yuk 25-avgustda jo'naydi" in contents
    assert "Akmal bilan Guangzhou yuki muhokama qilindi" in contents


async def test_a_summary_identical_to_a_fact_is_not_stored_twice(session):
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(TZ),
        raw_text="x",
    )
    session.add(interaction)
    await session.flush()

    result = ExtractionResult(summary="Bir xil matn", facts=["Bir xil matn"])
    await apply_extraction(session, interaction, result)
    await session.flush()

    n = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.Memory)
        .where(m.Memory.content == "Bir xil matn")
    )
    assert n == 1


# --- distinct telegram accounts stay distinct people -------------------------


async def test_two_telegram_accounts_with_similar_names_stay_two_people(session):
    first = await resolve_person(session, "Akmal Karimov", telegram_id=111)
    await session.flush()
    second = await resolve_person(session, "Akmal", telegram_id=222)
    await session.flush()

    # Without the guard, "Akmal" fuzzy-matches "Akmal Karimov" at 100 and the
    # second supplier's debts would land on the first.
    assert second.id != first.id
    assert first.telegram_id == 111
    assert second.telegram_id == 222


async def test_a_nameless_mention_still_matches_the_telegram_contact(session):
    known = await resolve_person(session, "Akmal Karimov", telegram_id=111)
    await session.flush()
    # The owner's voice note has no telegram_id — fuzzy matching must still
    # attach it to the known contact.
    same = await resolve_person(session, "Akmal aka")
    assert same.id == known.id


# --- userbot defaults: bots and the service chat stay off --------------------


def test_bot_dms_and_the_telegram_service_chat_are_not_monitored_by_default():
    human = DialogInfo(tg_chat_id=1, chat_type=ChatType.private, title="Akmal")
    bot = DialogInfo(tg_chat_id=2, chat_type=ChatType.private, title="MIYA", is_bot=True)
    service = DialogInfo(
        tg_chat_id=TELEGRAM_SERVICE_ID, chat_type=ChatType.private, title="Telegram"
    )
    group = DialogInfo(tg_chat_id=3, chat_type=ChatType.group, title="Yuklar")

    assert default_monitor_enabled(human) is True
    # Login codes / 2FA notifications must never reach the extraction API.
    assert default_monitor_enabled(service) is False
    # MIYA's own assistant chat would be double-extracted.
    assert default_monitor_enabled(bot) is False
    assert default_monitor_enabled(group) is False


# --- audio files are capped, voice notes are not -----------------------------


def test_a_shared_podcast_is_not_transcribed_but_a_voice_note_always_is():
    huge = settings.audio_max_bytes + 1
    podcast = plan_for(
        MediaKind.audio, vision_enabled=False, docs_enabled=True, size=huge
    )
    assert not podcast.download and podcast.skip_reason == "too_large"

    small = plan_for(
        MediaKind.audio, vision_enabled=False, docs_enabled=True, size=1_000_000
    )
    assert small.download and small.transcribe

    voice = plan_for(MediaKind.voice, vision_enabled=False, docs_enabled=True, size=huge)
    assert voice.download and voice.transcribe


# --- windows wait for in-flight transcription --------------------------------


async def test_a_window_waits_for_a_voice_message_still_being_transcribed(session):
    now = datetime.now(TZ)
    chat = -100555
    for minutes, media in [
        (50, None),
        (45, {"type": "voice", "processed": False}),  # Scribe still running
    ]:
        session.add(
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.in_,
                tg_chat_id=chat,
                occurred_at=now - timedelta(minutes=minutes),
                raw_text="salom" if media is None else None,
                media=media,
            )
        )
    await session.flush()

    grouped = await windows._unclaimed_by_chat(session, now=now)
    texts = [i.raw_text for i in grouped.get(chat, [])]
    # The text message is claimable; the pending voice message is not — its
    # transcript would otherwise be replaced by "[ovozli xabar]" forever.
    assert texts == ["salom"]


async def test_a_voice_message_stuck_unprocessed_for_an_hour_is_released(session):
    now = datetime.now(TZ)
    chat = -100556
    session.add(
        m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.in_,
            tg_chat_id=chat,
            occurred_at=now - timedelta(hours=2),
            media={"type": "voice", "processed": False},
        )
    )
    await session.flush()

    grouped = await windows._unclaimed_by_chat(session, now=now)
    # A crash mid-download must not wedge the message out of every window.
    assert len(grouped.get(chat, [])) == 1


# --- gcal pull notices deletions ---------------------------------------------


class _Api(gcal.CalendarAPI):
    def __init__(self, items):
        self.items = items

    async def list_events(self, time_min, time_max):
        return self.items

    async def insert_event(self, body):  # pragma: no cover - not used here
        raise AssertionError("pull must never insert")


async def test_an_event_deleted_in_google_is_cancelled_locally(session):
    start = datetime.now(TZ) + timedelta(days=2)
    item = {
        "id": "gone-1",
        "summary": "Uchrashuv",
        "status": "confirmed",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    await gcal.pull(session, _Api([item]))
    await session.flush()

    # Next pull: the owner deleted the meeting in Google — it just vanishes
    # from the listing.
    await gcal.pull(session, _Api([]))
    await session.flush()

    event = await session.scalar(
        sa.select(m.Event).where(m.Event.gcal_event_id == "gone-1")
    )
    assert event.status is EventStatus.cancelled


async def test_a_deleted_event_stops_haunting_the_planner(session):
    start = datetime.now(TZ) + timedelta(days=1)
    item = {
        "id": "gone-2",
        "summary": "Bekor qilingan uchrashuv",
        "status": "confirmed",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    await gcal.pull(session, _Api([item]))
    await gcal.pull(session, _Api([]))
    await session.flush()

    upcoming = await queries.events_between(
        session, datetime.now(TZ), datetime.now(TZ) + timedelta(days=7)
    )
    assert all(e.gcal_event_id != "gone-2" for e in upcoming)


# --- biggest expenses are ranked per currency --------------------------------


async def test_biggest_expenses_do_not_mix_currencies(session):
    now = datetime.now(TZ)
    amounts = [
        (Decimal("300000"), Currency.UZS),
        (Decimal("250000"), Currency.UZS),
        (Decimal("200000"), Currency.UZS),
        (Decimal("150000"), Currency.UZS),
        (Decimal("10000"), Currency.USD),  # numerically small, actually huge
    ]
    for amount, currency in amounts:
        session.add(
            m.Transaction(
                type=TransactionType.expense,
                amount=amount,
                currency=currency,
                category="transport",
                occurred_at=now,
            )
        )
    await session.flush()

    summary = await queries.spending_summary(session, now.date(), now.date())
    by_currency = {}
    for txn in summary.biggest:
        by_currency.setdefault(txn.currency, []).append(txn.amount)

    # The $10,000 payment must be present even though four UZS rows outrank it
    # numerically.
    assert by_currency[Currency.USD] == [Decimal("10000.00")]
    assert len(by_currency[Currency.UZS]) == 3  # top three, not all four


# --- retention never deletes what was never ingested -------------------------


async def test_retention_spares_a_recording_the_scan_has_not_ingested_yet(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "call_recordings_dir", str(tmp_path / "calls"))
    calls = Path(settings.call_recordings_dir)
    calls.mkdir(parents=True)

    import os

    old = datetime.now(TZ) - timedelta(days=400)
    never_ingested = calls / "Call recording Akmal_240101_120000.m4a"
    never_ingested.write_bytes(b"historical audio")
    os.utime(never_ingested, (old.timestamp(), old.timestamp()))

    ingested = calls / "Call recording Botir_240102_120000.m4a"
    ingested.write_bytes(b"already processed audio")
    os.utime(ingested, (old.timestamp(), old.timestamp()))
    session.add(
        m.Interaction(
            source=InteractionSource.phone_call,
            direction=Direction.na,
            occurred_at=old,
            processed=True,
            media={"type": "call_recording", "path": str(ingested), "processed": True},
        )
    )
    await session.flush()

    deleted = await call_recordings.purge_old_audio(session)
    # The first Syncthing sync delivers months of history with old mtimes; the
    # archive must survive until the scan job has worked through it.
    assert never_ingested.exists()
    assert not ingested.exists()
    assert deleted == 1


# --- filename timestamps from another timezone -------------------------------


def test_a_filename_stamped_in_china_time_falls_back_to_mtime(tmp_path):
    import os

    # The phone (in China, UTC+8) stamps 14:30; the file's mtime is the true
    # end of the call — 11:35 Tashkent. Three hours' drift means the stamp is
    # from another timezone and must lose.
    path = tmp_path / "Call recording Akmal_260819_143000.m4a"
    path.write_bytes(b"x")
    true_end = datetime(2026, 8, 19, 11, 35, tzinfo=TZ)
    os.utime(path, (true_end.timestamp(), true_end.timestamp()))

    parsed = call_recordings.parse_filename(path)
    assert parsed.recorded_at is not None
    drift = abs((true_end - parsed.recorded_at).total_seconds())
    assert drift > 90 * 60  # this is the case ingest_recording now catches


async def test_ingest_uses_mtime_when_the_stamp_disagrees(session, tmp_path, monkeypatch):
    import os

    from miya.services import ingest as ingest_svc

    async def _no_transcribe(session_, interaction, path):
        interaction.transcript = "salom"
        return "salom"

    async def _no_process(session_, interaction):
        from miya.services.persistence import Applied

        interaction.processed = True
        return ingest_svc.IngestResult(interaction=interaction, applied=Applied())

    monkeypatch.setattr(call_recordings, "transcribe_into", _no_transcribe)
    monkeypatch.setattr(call_recordings, "process_interaction", _no_process)

    path = tmp_path / "Call recording Akmal_260819_143000.m4a"
    path.write_bytes(b"audio")
    true_end = datetime(2026, 8, 19, 11, 35, tzinfo=TZ)
    os.utime(path, (true_end.timestamp(), true_end.timestamp()))

    result = await call_recordings.ingest_recording(session, path)
    assert result is not None
    occurred = result.interaction.occurred_at.astimezone(TZ)
    assert occurred == true_end


# --- /tekshir shows the filename wherever it lives ---------------------------


def test_review_report_reads_the_filename_from_media_too():
    from types import SimpleNamespace

    from miya.bot import replies

    it = SimpleNamespace(
        source=SimpleNamespace(value="telegram_userbot"),
        raw_text=None,
        transcript=None,
        meta={},
        media={"filename": "invoice.pdf"},
        occurred_at=datetime.now(TZ),
    )
    body = replies.review_report([it], total=1)
    assert "invoice.pdf" in body
