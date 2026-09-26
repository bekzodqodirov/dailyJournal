"""The worker's missed-call nudge job (build step 6).

The core — the loop detector, collect_missed's once-plus-snooze discipline —
is covered in test_phone_events_core.py. Here: the job itself. It sends one
short message per open missed-call loop with the ✅ Bog'landim / ⏰ Ertaga
keyboard, nudges each loop once ever (plus once more after a snooze
expires), holds inside quiet hours, marks only what notify() actually
delivered, caps a sweep at MAX_PER_SWEEP, and is registered after the
nudges job with the heartbeat/health pair still last. Telegram is stubbed;
the database is real.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.services import nudges, phone_events
from miya.worker import main as worker
from tests.test_chat_notices import _Bot

TZ = settings.tz
DEVICE = "phone-1"


class _MarkupBot(_Bot):
    """The chat-notices stub, also keeping each message's keyboard."""

    def __init__(self, *, reachable: bool = True) -> None:
        super().__init__(reachable=reachable)
        self.markups: list = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        await super().send_message(chat_id, text, **kwargs)
        self.markups.append(kwargs.get("reply_markup"))


class _FlakyBot(_Bot):
    """Delivers the first ``deliverable`` messages, then Telegram is down."""

    def __init__(self, deliverable: int) -> None:
        super().__init__()
        self.deliverable = deliverable

    async def send_message(self, chat_id, text, **kwargs) -> None:
        if len(self.sent) >= self.deliverable:
            raise RuntimeError("telegram is down")
        await super().send_message(chat_id, text, **kwargs)


def _quiet(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: value)


def _event(
    call_log_id: int,
    *,
    number: str | None = "+998901234567",
    contact_name: str | None = None,
    minutes_ago: int = 120,
    call_type: str = "missed",
) -> dict:
    started = datetime.now(TZ) - timedelta(minutes=minutes_ago)
    return {
        "call_log_id": call_log_id,
        "started_at": started.isoformat(),
        "duration_seconds": 0,
        "type": call_type,
        "number": number,
        "contact_name": contact_name,
    }


async def _missed(session, *events: dict) -> None:
    outcome = await phone_events.ingest_call_events(session, DEVICE, list(events))
    assert outcome.rejected == []
    assert outcome.accepted == len(events)
    await session.commit()


async def _interaction_id(session, call_log_id: int) -> int:
    key = phone_events.call_event_key(DEVICE, call_log_id)
    found = await session.scalar(
        sa.select(m.Interaction.id).where(m.Interaction.media["event_key"].astext == key)
    )
    assert found is not None
    return found


async def _nudged_refs(session) -> list[str]:
    await session.commit()  # see the job's own committed rows, not a snapshot
    return list(
        await session.scalars(
            sa.select(m.ReminderLog.ref)
            .where(m.ReminderLog.kind == nudges.MISSED_KIND)
            .order_by(m.ReminderLog.id)
        )
    )


# --- one nudge, with the buttons ---------------------------------------------


# --- quiet hours and delivery ------------------------------------------------


async def test_the_morning_brief_keyboard_carries_the_missed_rows(session, monkeypatch):
    """The 09:00 brief tells; the numbered batch right after it asks: the
    missed call arrives with its ✅ Bog'landim / ⏰ row (WP-19)."""
    await _missed(session, _event(1, minutes_ago=180))
    interaction_id = await _interaction_id(session, 1)
    bot = _MarkupBot()

    assert await worker.brief_job(bot) is True

    brief_markup, batch = bot.markups
    assert brief_markup is None
    payloads = [b.callback_data for row in batch.inline_keyboard for b in row]
    assert f"rec:ma:m{interaction_id}" in payloads
    assert f"rec:ms:m{interaction_id}" in payloads


# --- registration ------------------------------------------------------------
