"""Due reminders (spec §8).

Runs hourly. Anything due today or tomorrow — and anything already overdue —
is worth one ping, but only one per item per 24 hours, and never during quiet
hours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import ReminderLog
from miya.services import queries

log = logging.getLogger(__name__)

DEDUPE_WINDOW = timedelta(hours=24)


@dataclass(slots=True)
class DueBundle:
    debts: list = field(default_factory=list)
    promises: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    events: list = field(default_factory=list)
    keys: list[tuple[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.debts or self.promises or self.tasks or self.events)


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True inside QUIET_HOURS, which normally wraps midnight (23:30–07:30)."""
    now = now or datetime.now(settings.tz)
    start, end = settings.quiet_hours_parsed
    current = now.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


async def _already_sent(
    session: AsyncSession, kind: str, ref: str, now: datetime
) -> bool:
    cutoff = now - DEDUPE_WINDOW
    found = await session.scalar(
        sa.select(ReminderLog.id)
        .where(ReminderLog.kind == kind, ReminderLog.ref == ref)
        .where(ReminderLog.sent_at >= cutoff)
        .limit(1)
    )
    return found is not None


async def collect_due(
    session: AsyncSession, *, now: datetime | None = None, horizon_days: int = 1
) -> DueBundle:
    """Everything worth reminding about that has not been pinged in 24h."""
    now = now or datetime.now(settings.tz)
    bundle = DueBundle()
    due = await queries.due_items(session, horizon_days=horizon_days)

    for balance in due["debts"]:
        # A balance spans several debt rows, so key on person + direction +
        # currency. Direction matters: "they owe me" and "I owe them" are two
        # different reminders, and one must not suppress the other for 24h.
        ref = f"{balance.person.id}:{balance.direction.value}:{balance.currency.value}"
        if await _already_sent(session, "debt", ref, now):
            continue
        bundle.debts.append(balance)
        bundle.keys.append(("debt", ref))

    for promise, person in due["promises"]:
        ref = str(promise.id)
        if await _already_sent(session, "promise", ref, now):
            continue
        bundle.promises.append((promise, person))
        bundle.keys.append(("promise", ref))

    for task in due["tasks"]:
        ref = str(task.id)
        if await _already_sent(session, "task", ref, now):
            continue
        bundle.tasks.append(task)
        bundle.keys.append(("task", ref))

    for event in await queries.upcoming_events(session, within_minutes=60):
        ref = str(event.id)
        if await _already_sent(session, "event", ref, now):
            continue
        bundle.events.append(event)
        bundle.keys.append(("event", ref))

    return bundle


async def mark_sent(session: AsyncSession, bundle: DueBundle) -> None:
    """Record the ping only after it actually went out."""
    for kind, ref in bundle.keys:
        session.add(ReminderLog(kind=kind, ref=ref))
