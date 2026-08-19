"""Google Calendar sync (spec §7D).

Pull: upcoming events land in ``events`` with ``source=gcal``, upserted by
``gcal_event_id`` so re-pulls update instead of duplicating. Push: extracted
events with a concrete time are created in Google Calendar with a ``[MIYA]``
marker; the stored ``gcal_event_id`` is what stops the next pull from
re-importing them as new rows.

The Google client is wrapped in a tiny ``CalendarAPI`` interface so tests (and
a future CalDAV backend) can swap it without touching the sync logic. Auth is
a token file produced by ``python -m miya.tools.gcal_auth`` — until it exists,
every entry point here is a quiet no-op.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import EventSource, EventStatus
from miya.db.models import Event

log = logging.getLogger(__name__)

GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]
MIYA_MARKER = "[MIYA]"


class CalendarAPI(ABC):
    @abstractmethod
    async def list_events(
        self, time_min: datetime, time_max: datetime
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def insert_event(self, body: dict[str, Any]) -> dict[str, Any]: ...


class GoogleCalendarAPI(CalendarAPI):
    """google-api-python-client wrapper; all sync calls go through a thread."""

    def __init__(self, calendar_id: str | None = None) -> None:
        self._calendar_id = calendar_id or settings.gcal_calendar_id
        self._service = None

    def _get_service(self):
        if self._service is None:
            # Deferred imports: the google client stack is only needed when
            # calendar sync is actually configured.
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(
                settings.google_token_json, GCAL_SCOPES
            )
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())
                Path(settings.google_token_json).write_text(creds.to_json())
            self._service = build(
                "calendar", "v3", credentials=creds, cache_discovery=False
            )
        return self._service

    def _list(self, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]:
        service = self._get_service()
        items: list[dict[str, Any]] = []
        token = None
        while True:
            response = (
                service.events()
                .list(
                    calendarId=self._calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=token,
                )
                .execute()
            )
            items.extend(response.get("items", []))
            token = response.get("nextPageToken")
            if not token:
                return items

    def _insert(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._get_service()
        return service.events().insert(calendarId=self._calendar_id, body=body).execute()

    async def list_events(
        self, time_min: datetime, time_max: datetime
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list, time_min, time_max)

    async def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._insert, body)


def is_configured() -> bool:
    return Path(settings.google_token_json).is_file()


def get_api() -> GoogleCalendarAPI | None:
    return GoogleCalendarAPI() if is_configured() else None


def _parse_gcal_time(value: dict[str, Any] | None) -> datetime | None:
    """Google sends {"dateTime": iso} for timed and {"date": iso} for all-day."""
    if not value:
        return None
    raw = value.get("dateTime") or value.get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("unparseable gcal time: %r", raw)
        return None
    if parsed.tzinfo is None:  # all-day events come as bare dates
        parsed = parsed.replace(tzinfo=settings.tz)
    return parsed


_GCAL_STATUS = {
    "confirmed": EventStatus.planned,
    "tentative": EventStatus.planned,
    "cancelled": EventStatus.cancelled,
}


async def pull(
    session: AsyncSession, api: CalendarAPI, *, days: int | None = None
) -> int:
    """Sync the next N days of Google events into `events`. Returns changed rows."""
    now = datetime.now(settings.tz)
    window_end = now + timedelta(days=days or settings.gcal_days_ahead)

    # Snapshot the candidates *before* the API call. Anything that gains a
    # gcal_event_id while the request is in flight (gcal_push runs every five
    # minutes) is therefore not a deletion candidate — otherwise a freshly
    # pushed meeting, absent from a response that predates it, would be
    # cancelled seconds after being created.
    known_before = {
        gid
        for gid in await session.scalars(
            sa.select(Event.gcal_event_id)
            .where(Event.gcal_event_id.isnot(None))
            .where(Event.status == EventStatus.planned)
            .where(Event.start_at >= now, Event.start_at < window_end)
        )
        if gid
    }

    items = await api.list_events(now, window_end)

    seen_ids: set[str] = set()
    changed = 0
    for item in items:
        gcal_id = item.get("id")
        if not gcal_id:
            continue
        # Recorded before the start-time check: an event Google still holds is
        # not a deletion just because MIYA could not parse its start.
        seen_ids.add(gcal_id)
        start_at = _parse_gcal_time(item.get("start"))
        if start_at is None:
            continue
        title = (item.get("summary") or "").strip() or "(nomsiz)"
        end_at = _parse_gcal_time(item.get("end"))
        location = (item.get("location") or "").strip() or None
        description = (item.get("description") or "").strip() or None
        status = _GCAL_STATUS.get(item.get("status", "confirmed"), EventStatus.planned)
        attendees = [
            a.get("displayName") or a.get("email")
            for a in item.get("attendees", [])
            if a.get("displayName") or a.get("email")
        ] or None

        event = await session.scalar(
            sa.select(Event).where(Event.gcal_event_id == gcal_id)
        )
        if event is None:
            session.add(
                Event(
                    title=title,
                    start_at=start_at,
                    end_at=end_at,
                    location=location,
                    description=description,
                    attendees=attendees,
                    source=EventSource.gcal,
                    gcal_event_id=gcal_id,
                    status=status,
                )
            )
            changed += 1
            continue

        # An event we pushed keeps source=extracted; only its details update.
        updates = {
            "title": title,
            "start_at": start_at,
            "end_at": end_at,
            "location": location,
            "status": status,
        }
        dirty = False
        for attr, value in updates.items():
            if getattr(event, attr) != value:
                setattr(event, attr, value)
                dirty = True
        if dirty:
            changed += 1

    # An event the owner deletes in Google simply vanishes from the listing
    # (the default list call excludes cancelled instances), so a row that was
    # in Google before this request and is missing from the response is a
    # deletion. Without this, the planner and the 60-minute reminder keep
    # resurrecting meetings that no longer exist. Events MIYA pushed itself
    # count too: they live in the same calendar and are deleted the same way.
    missing = known_before - seen_ids
    if missing:
        stale = await session.scalars(
            sa.select(Event)
            .where(Event.gcal_event_id.in_(missing))
            .where(Event.status == EventStatus.planned)
        )
        for event in stale:
            event.status = EventStatus.cancelled
            changed += 1

    await session.flush()
    return changed


async def push_pending(session: AsyncSession, api: CalendarAPI) -> int:
    """Create Google events for extracted ones with a concrete future time.

    Date-only extractions land at local midnight — those stay local, pushing
    them would spam the calendar with 00:00 entries. The [MIYA] marker plus the
    stored gcal_event_id keep the next pull from importing our own events back.
    """
    now = datetime.now(settings.tz)
    events = list(
        await session.scalars(
            sa.select(Event)
            .where(Event.source == EventSource.extracted)
            .where(Event.gcal_event_id.is_(None))
            .where(Event.status == EventStatus.planned)
            .where(Event.start_at > now)
            .order_by(Event.start_at)
        )
    )

    pushed = 0
    for event in events:
        start_local = event.start_at.astimezone(settings.tz)
        if start_local.time() == start_local.min.time():
            continue
        end_local = (event.end_at or event.start_at + timedelta(hours=1)).astimezone(
            settings.tz
        )
        description = f"{event.description or ''}\n\n{MIYA_MARKER}".strip()
        # timestamptz reads back as UTC; send local Tashkent wall time so the
        # calendar entry is readable at a glance.
        body = {
            "summary": event.title,
            "location": event.location,
            "description": description,
            "start": {"dateTime": start_local.isoformat()},
            "end": {"dateTime": end_local.isoformat()},
        }
        try:
            created = await api.insert_event(body)
        except Exception:
            # Leave gcal_event_id NULL — the next push run retries this event.
            log.exception("could not push event %r to Google Calendar", event.title)
            continue
        if created.get("id"):
            event.gcal_event_id = created["id"]
            pushed += 1

    await session.flush()
    return pushed
