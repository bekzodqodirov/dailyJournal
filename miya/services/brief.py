"""The morning brief (build step 2, part B): one message at MORNING_BRIEF_TIME.

Everything in it is SQL and the open-loops engine — no model call, so the
brief arrives even when Anthropic is down. It says, in this order: today's
meetings, what is due today or overdue (the reminder sweep's own selection,
not a second one), questions nobody answered, missed calls, undated
commitments that have been sitting, counterparties who went quiet with
something open, and one line counting what still waits for the owner's tap
(WP-19: the brief tells; the numbered question batch after it asks).

Rendering is replies.morning_brief; this module only gathers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Event
from miya.services import codes, nudges, queries
from miya.services.loops import OpenLoops

# The reminder_log kind the morning brief is logged under, one row per day.
BRIEF_KIND = "brief"


@dataclass(slots=True)
class MorningBrief:
    now: datetime
    events: list[Event] = field(default_factory=list)
    # queries.due_items(horizon_days=0): overdue and due today, the same
    # selection reminders.collect_due pings from.
    due: dict = field(default_factory=dict)
    loops: OpenLoops | None = None
    # Everything that waits for the owner's tap (WP-19): only counted here —
    # the brief tells, the question batch after it asks.
    queue: Any = None
    # Money texts waiting in /tekshir (WP-14): one count line, never a push.
    money_review: int = 0
    # Code suggestions waiting in /kodlar (WP-33): one count line, never a
    # push, and on its own not a reason to send the brief.
    code_suggestions: int = 0

    @property
    def day(self) -> date:
        return self.now.astimezone(settings.tz).date()

    def is_empty(self) -> bool:
        return not (
            self.events
            or any(self.due.values())
            or (self.loops is not None and not self.loops.is_empty())
            or (self.queue is not None and self.queue.waiting > 0)
            or self.money_review
        )


async def gather(session: AsyncSession, *, now: datetime | None = None) -> MorningBrief:
    now = now or datetime.now(settings.tz)
    start, end = queries.day_bounds(now.astimezone(settings.tz).date())
    return MorningBrief(
        now=now,
        events=await queries.events_between(session, start, end),
        due=await queries.due_items(session, horizon_days=0),
        loops=await nudges.open_loops(session, now=now),
        money_review=await queries.money_review_count(session),
        code_suggestions=await codes.pending_suggestion_count(session),
    )
