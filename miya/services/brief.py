"""The morning brief (build step 2, part B): one message at MORNING_BRIEF_TIME.

Everything in it is SQL and the open-loops engine — no model call, so the
brief arrives even when Anthropic is down. It says, in this order: today's
meetings, today's deadlines that name a time of day by that time (JV-08),
what else is due today or overdue (the reminder sweep's own selection,
not a second one), questions nobody answered, missed calls, undated
commitments that have been sitting, counterparties who went quiet with
something open, and one line counting what still waits for the owner's tap
(WP-19: the brief tells; the numbered question batch after it asks).

Rendering is replies.morning_brief; this module only gathers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Event
from miya.services import approvals, codes, nudges, queries
from miya.services.loops import OpenLoops

# The reminder_log kind the morning brief is logged under, one row per day.
BRIEF_KIND = "brief"

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MorningBrief:
    now: datetime
    events: list[Event] = field(default_factory=list)
    # queries.due_items(horizon_days=0): overdue and due today, the same
    # selection reminders.collect_due pings from.
    due: dict = field(default_factory=dict)
    # «Bugungi muddatlar» (JV-08): today's timed promises and tasks as
    # ``(kind, record, person)``, ordered by ``due_at``. The due section
    # leaves these rows out, so each deadline is said once.
    timed: list[tuple[str, Any, Any]] = field(default_factory=list)
    loops: OpenLoops | None = None
    # Everything that waits for the owner's tap (WP-19): only counted here —
    # the brief tells, the question batch after it asks.
    queue: Any = None
    # Money texts waiting in /tekshir (WP-14): one count line, never a push.
    money_review: int = 0
    # Code suggestions waiting in /kodlar (WP-33): one count line, never a
    # push, and on its own not a reason to send the brief.
    code_suggestions: int = 0
    # File questions that expired in the last 24 h (WP-46): a count line.
    media_expired: int = 0
    # Open /holat problems (WP-80): one "🩺 Tizim" line; None when unknown.
    system_problems: int | None = None

    @property
    def day(self) -> date:
        return self.now.astimezone(settings.tz).date()

    def is_empty(self) -> bool:
        return not (
            self.events
            or self.timed
            or any(self.due.values())
            or (self.loops is not None and not self.loops.is_empty())
            or (self.queue is not None and self.queue.waiting > 0)
            or self.money_review
        )


async def gather(session: AsyncSession, *, now: datetime | None = None) -> MorningBrief:
    now = now or datetime.now(settings.tz)
    day = now.astimezone(settings.tz).date()
    start, end = queries.day_bounds(day)
    events = await queries.events_between(session, start, end)
    due = await queries.due_items(session, horizon_days=0)
    timed = await _timed_today(session, day)
    await _label_brief(session, due, timed)
    return MorningBrief(
        now=now,
        events=events,
        due=due,
        timed=timed,
        loops=await nudges.open_loops(session, now=now),
        money_review=await queries.money_review_count(session),
        code_suggestions=await codes.pending_suggestion_count(session),
        media_expired=await approvals.expired_since(session, now - timedelta(hours=24)),
        system_problems=await _system_problems(session, now),
    )


async def _timed_today(session: AsyncSession, day: date) -> list[tuple[str, Any, Any]]:
    """Today's timed deadlines, soonest first; an empty list when the query
    fails, so the brief still goes out (the rows stay in the due section)."""
    try:
        async with session.begin_nested():
            found = await queries.timed_due_on(session, day)
    except Exception as exc:
        log.warning(
            "the brief's timed deadlines failed: %s",
            type(exc).__name__,
            extra={"error": type(exc).__name__, "day": day.isoformat()},
            exc_info=True,
        )
        return []
    items: list[tuple[str, Any, Any]] = [
        ("promise", promise, person) for promise, person in found["promises"]
    ]
    items += [("task", task, None) for task in found["tasks"]]
    # Promises and tasks are two queries; one list, by the moment they fall due.
    items.sort(key=lambda item: (item[1].due_at, item[0], item[1].id))
    return items


async def _label_brief(
    session: AsyncSession, due: dict, timed: list[tuple[str, Any, Any]]
) -> None:
    """Label the names of the due and timed sections as one list (WP-78): a
    namesake in one section and the other must read apart in both. When the
    labels fail, due_items' own labels stay and the brief still goes out."""
    if not timed:
        return  # due_items labelled exactly this list already
    people = [b.person for b in due.get("debts", [])]
    people += [person for _, person in due.get("promises", [])]
    people += [person for _, _, person in timed if person is not None]
    try:
        async with session.begin_nested():
            await codes.apply_labels(session, people)
    except Exception as exc:
        log.warning(
            "the brief's name labels failed: %s",
            type(exc).__name__,
            extra={
                "error": type(exc).__name__,
                "person_ids": sorted({p.id for p in people if p is not None}),
            },
            exc_info=True,
        )


async def _system_problems(session: AsyncSession, now: datetime) -> int | None:
    """How many /holat problems are open; None when the check itself fails,
    so the brief is never lost to it."""
    from miya.services import health

    try:
        return len(health.problems(await health.gather(session, now=now)))
    except Exception:
        log.warning("the brief's system line failed", exc_info=True)
        return None
