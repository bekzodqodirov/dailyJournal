"""The morning brief (build step 2, part B): one message at MORNING_BRIEF_TIME.

Everything in it is SQL and the open-loops engine — no model call, so the
brief arrives even when Anthropic is down. It says, in this order: today's
meetings, what is due today or overdue (the reminder sweep's own selection,
not a second one), questions nobody answered, undated commitments that have
been sitting, and counterparties who went quiet with something open.

Rendering is replies.morning_brief; this module only gathers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Event
from miya.services import nudges, queries
from miya.services.loops import OpenLoops


@dataclass(slots=True)
class MorningBrief:
    now: datetime
    events: list[Event] = field(default_factory=list)
    # queries.due_items(horizon_days=0): overdue and due today, the same
    # selection reminders.collect_due pings from.
    due: dict = field(default_factory=dict)
    loops: OpenLoops | None = None

    @property
    def day(self) -> date:
        return self.now.astimezone(settings.tz).date()

    def is_empty(self) -> bool:
        return not (
            self.events
            or any(self.due.values())
            or (self.loops is not None and not self.loops.is_empty())
        )


async def gather(session: AsyncSession, *, now: datetime | None = None) -> MorningBrief:
    now = now or datetime.now(settings.tz)
    start, end = queries.day_bounds(now.astimezone(settings.tz).date())
    return MorningBrief(
        now=now,
        events=await queries.events_between(session, start, end),
        due=await queries.due_items(session, horizon_days=0),
        loops=await nudges.open_loops(session, now=now),
    )
