"""Google Calendar sync (Phase 3). The Google client is faked; rows are real."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import EventSource, EventStatus
from miya.services import gcal

TZ = settings.tz


class FakeCalendarAPI(gcal.CalendarAPI):
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.inserted: list[dict[str, Any]] = []

    async def list_events(self, time_min, time_max):
        return self.items

    async def insert_event(self, body):
        self.inserted.append(body)
        return {"id": f"gcal-{len(self.inserted)}"}


def _gcal_item(**overrides) -> dict[str, Any]:
    start = datetime.now(TZ).replace(microsecond=0) + timedelta(days=1)
    item = {
        "id": "abc123",
        "summary": "Mijoz bilan uchrashuv",
        "status": "confirmed",
        "location": "Tashkent City",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        "attendees": [{"displayName": "Akmal"}, {"email": "x@y.z"}],
    }
    item.update(overrides)
    return item


# --- pull --------------------------------------------------------------------


async def test_pull_imports_timed_and_all_day_events(session):
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    api = FakeCalendarAPI(
        [
            _gcal_item(),
            _gcal_item(
                id="allday1",
                summary="Yuk kuni",
                start={"date": tomorrow.isoformat()},
                end={"date": (tomorrow + timedelta(days=1)).isoformat()},
            ),
        ]
    )

    assert await gcal.pull(session, api) == 2

    events = {e.gcal_event_id: e for e in await session.scalars(sa.select(m.Event))}
    assert events["abc123"].source is EventSource.gcal
    assert events["abc123"].attendees == ["Akmal", "x@y.z"]
    allday = events["allday1"]
    assert allday.start_at.astimezone(TZ).hour == 0  # all-day → local midnight


async def test_repulling_the_same_events_changes_nothing(session):
    api = FakeCalendarAPI([_gcal_item()])
    await gcal.pull(session, api)

    assert await gcal.pull(session, api) == 0
    total = await session.scalar(sa.select(sa.func.count()).select_from(m.Event))
    assert total == 1


async def test_pull_updates_a_changed_event_in_place(session):
    api = FakeCalendarAPI([_gcal_item()])
    await gcal.pull(session, api)

    api.items = [_gcal_item(summary="Uchrashuv ko'chirildi", status="cancelled")]
    assert await gcal.pull(session, api) == 1

    event = await session.scalar(sa.select(m.Event))
    assert event.title == "Uchrashuv ko'chirildi"
    assert event.status is EventStatus.cancelled


# --- push --------------------------------------------------------------------


def _extracted_event(*, start_at: datetime, **overrides) -> m.Event:
    fields: dict[str, Any] = {
        "title": "Bojxona uchrashuvi",
        "start_at": start_at,
        "source": EventSource.extracted,
        "status": EventStatus.planned,
        "description": "hujjatlarni olib borish",
    }
    fields.update(overrides)
    return m.Event(**fields)


async def test_push_creates_the_event_with_the_miya_marker(session):
    start = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0)
    start += timedelta(days=1)
    session.add(_extracted_event(start_at=start))
    await session.flush()
    api = FakeCalendarAPI()

    assert await gcal.push_pending(session, api) == 1

    [body] = api.inserted
    assert body["summary"] == "Bojxona uchrashuvi"
    assert gcal.MIYA_MARKER in body["description"]
    assert body["start"]["dateTime"] == start.isoformat()

    event = await session.scalar(sa.select(m.Event))
    assert event.gcal_event_id == "gcal-1"

    # Already pushed — the next run must not create a duplicate.
    assert await gcal.push_pending(session, api) == 0
    assert len(api.inserted) == 1


async def test_push_skips_dateonly_past_and_foreign_events(session):
    now = datetime.now(TZ)
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    session.add_all(
        [
            # Date-only extraction (local midnight) stays out of the calendar.
            _extracted_event(start_at=midnight_tomorrow),
            # The past is not pushed.
            _extracted_event(start_at=now - timedelta(hours=3)),
            # Events that came FROM Google are never pushed back.
            _extracted_event(
                start_at=now + timedelta(days=1, hours=2),
                source=EventSource.gcal,
                gcal_event_id="from-google",
            ),
        ]
    )
    await session.flush()
    api = FakeCalendarAPI()

    assert await gcal.push_pending(session, api) == 0
    assert api.inserted == []


async def test_a_pushed_event_is_not_reimported_by_the_next_pull(session):
    start = (datetime.now(TZ) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    session.add(_extracted_event(start_at=start))
    await session.flush()
    api = FakeCalendarAPI()
    await gcal.push_pending(session, api)

    # Google now returns our own event; it must map onto the existing row.
    api.items = [
        _gcal_item(
            id="gcal-1",
            summary="Bojxona uchrashuvi",
            start={"dateTime": start.isoformat()},
            end={"dateTime": (start + timedelta(hours=1)).isoformat()},
            description=f"hujjatlarni olib borish\n\n{gcal.MIYA_MARKER}",
            attendees=[],
        )
    ]
    await gcal.pull(session, api)

    events = list(await session.scalars(sa.select(m.Event)))
    assert len(events) == 1
    assert events[0].source is EventSource.extracted  # provenance survives


async def test_a_failed_insert_leaves_the_event_queued(session):
    class ExplodingAPI(FakeCalendarAPI):
        async def insert_event(self, body):
            raise RuntimeError("google is down")

    start = (datetime.now(TZ) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    session.add(_extracted_event(start_at=start))
    await session.flush()

    assert await gcal.push_pending(session, ExplodingAPI()) == 0

    event = await session.scalar(sa.select(m.Event))
    assert event.gcal_event_id is None  # retried on the next run
