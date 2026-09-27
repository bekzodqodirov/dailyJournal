"""Nudges for questions nobody answered (build step 2, part B).

The open-loops engine (services/loops.py) finds a question in a chat with
nothing outgoing after it. This module turns that into something the owner
can act on without opening the chat:

  * every 30 minutes the worker sends one short message per question older
    than LOOP_QUESTION_HOURS — at most MAX_PER_SWEEP of them, the rest folded
    into one line and sent on a later sweep. Quiet-hours aware: the sweep
    skips, the question keeps.
  * a question is nudged ONCE. From then on the morning brief carries it
    (and the evening report), until it is answered. The one exception is
    "⏰ Ertaga": a snoozed question is nudged once more when the snooze
    expires, and then never again.
  * "✅ Javob berdim" marks the question answered for good, on the
    interaction's own metadata, so a restart or a re-sync cannot bring it
    back. "⏰ Ertaga" snoozes it until the next morning brief.

State lives in two places the rest of the system already uses: ``reminder_log``
rows of kind ``nudge:q`` (ref = interaction id) say when a nudge went out, and
``interactions.metadata`` carries ``answered`` and ``nudge_snoozed_until``.

The engine itself honours the ``answered`` mark (loops.is_answered), so the
brief, the report and the sweep all read questions through the same walk;
``unanswered_questions`` and ``open_loops`` here are kept as names for the
callers and simply delegate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Interaction, ReminderLog
from miya.services import loops
from miya.services.loops import (
    ANSWERED_KEY,
    MissedCall,
    OpenLoops,
    UnansweredQuestion,
    is_answered,
)

__all__ = [
    "ANSWERED_KEY",
    "MAX_PER_SWEEP",
    "MISSED_KIND",
    "NUDGE_KIND",
    "SNOOZED_KEY",
    "collect",
    "collect_missed",
    "is_answered",
    "mark_answered",
    "mark_missed_nudged",
    "mark_nudged",
    "next_morning",
    "open_loops",
    "snooze",
    "snoozed_until",
    "unanswered_questions",
]

log = logging.getLogger(__name__)

# reminder_log kind for one nudge sent; the ref is the interaction id.
NUDGE_KIND = "nudge:q"
# The same, for a missed-call nudge (build step 6); same ref, same cadence.
MISSED_KIND = "nudge:m"
# One sweep sends this many nudges; the rest is one summary line and comes
# on a later sweep — the owner tolerates twenty-odd pings a day, not a burst.
MAX_PER_SWEEP = 5

# interactions.metadata key for "⏰ Ertaga" (ANSWERED_KEY lives in loops.py).
SNOOZED_KEY = "nudge_snoozed_until"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(settings.tz)


# --- the two buttons ------------------------------------------------------------


def mark_answered(
    interaction: Interaction, *, by: str = "button", now: datetime | None = None
) -> None:
    """The owner says this one is handled. Rebinds ``meta`` so the ORM sees it."""
    interaction.meta = {
        **(interaction.meta or {}),
        ANSWERED_KEY: {"at": _now(now).isoformat(), "by": by},
    }


def snoozed_until(interaction: Interaction) -> datetime | None:
    raw = (interaction.meta or {}).get(SNOOZED_KEY)
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return until if until.tzinfo else until.replace(tzinfo=settings.tz)


def snooze(interaction: Interaction, *, until: datetime) -> None:
    interaction.meta = {**(interaction.meta or {}), SNOOZED_KEY: until.isoformat()}


def next_morning(now: datetime | None = None) -> datetime:
    """The next MORNING_BRIEF_TIME, owner's timezone — where "Ertaga" lands.

    Today's, if the brief has not gone out yet (a tap at 00:30 means "at
    09:00", not the day after tomorrow); otherwise tomorrow's.
    """
    now = _now(now).astimezone(settings.tz)
    at = settings.morning_brief_time_parsed
    day = now.date()
    if now.timetz().replace(tzinfo=None) >= at:
        day += timedelta(days=1)
    return datetime.combine(day, at, tzinfo=settings.tz)


async def unanswered_questions(
    session: AsyncSession, *, now: datetime | None = None, hours: int | None = None
) -> list[UnansweredQuestion]:
    """The engine's detector; kept as a name for the brief and the report."""
    return await loops.unanswered_questions(session, now=now, hours=hours)


async def open_loops(session: AsyncSession, now: datetime | None = None) -> OpenLoops:
    """The engine's aggregate; kept as a name for the brief."""
    return await loops.open_loops(session, now)


# --- the sweep ---------------------------------------------------------------


async def _last_nudged(
    session: AsyncSession, ids: list[int], *, kind: str = NUDGE_KIND
) -> dict[int, datetime]:
    """When each interaction was last nudged under ``kind``, if ever."""
    if not ids:
        return {}
    rows = await session.execute(
        sa.select(ReminderLog.ref, sa.func.max(ReminderLog.sent_at))
        .where(ReminderLog.kind == kind)
        .where(ReminderLog.ref.in_([str(i) for i in ids]))
        .group_by(ReminderLog.ref)
    )
    return {int(ref): sent for ref, sent in rows.all()}


def is_due(last_nudge: datetime | None, until: datetime | None, *, now: datetime) -> bool:
    """Pure: one nudge per question, plus one more after a snooze expires.

    A prior nudge counts only if it went out after the snooze ended (or there
    was no snooze): the nudge before "⏰ Ertaga" is spent, the one after it
    is the last.
    """
    if until is not None and until > now:
        return False
    if last_nudge is None:
        return True
    return until is not None and last_nudge <= until


async def collect(
    session: AsyncSession, *, now: datetime | None = None
) -> list[UnansweredQuestion]:
    """Questions due a nudge this sweep, oldest first: unanswered, past the
    threshold, not snoozed, and never nudged since the last snooze ended."""
    now = _now(now)
    questions = await unanswered_questions(session, now=now)
    if not questions:
        return []
    interactions = {
        row.id: row
        for row in await session.scalars(
            sa.select(Interaction).where(
                Interaction.id.in_([q.interaction_id for q in questions])
            )
        )
    }
    nudged = await _last_nudged(session, list(interactions))
    return [
        question
        for question in questions
        if is_due(
            nudged.get(question.interaction_id),
            snoozed_until(interactions[question.interaction_id]),
            now=now,
        )
    ]


def mark_nudged(session: AsyncSession, questions: list[UnansweredQuestion]) -> None:
    """Only what actually went out — the summarised tail qualifies next sweep."""
    for question in questions:
        session.add(ReminderLog(kind=NUDGE_KIND, ref=str(question.interaction_id)))


# --- the missed-call sweep (build step 6) ------------------------------------


async def collect_missed(
    session: AsyncSession, *, now: datetime | None = None
) -> list[MissedCall]:
    """Missed calls due a nudge this sweep, oldest first.

    The same contract as questions, through the same ``is_due``: one nudge
    per missed-call loop ever, plus one more after a "⏰ Ertaga" snooze
    expires. The ✅/snooze marks live on the loop's own interaction, exactly
    as they do for a question.
    """
    now = _now(now)
    missed = await loops.missed_calls(session, now=now)
    if not missed:
        return []
    interactions = {
        row.id: row
        for row in await session.scalars(
            sa.select(Interaction).where(
                Interaction.id.in_([m.interaction_id for m in missed])
            )
        )
    }
    nudged = await _last_nudged(session, list(interactions), kind=MISSED_KIND)
    return [
        item
        for item in missed
        if is_due(
            nudged.get(item.interaction_id),
            snoozed_until(interactions[item.interaction_id]),
            now=now,
        )
    ]


def mark_missed_nudged(session: AsyncSession, missed: list[MissedCall]) -> None:
    """Only what notify() actually delivered, as everywhere else."""
    for item in missed:
        session.add(ReminderLog(kind=MISSED_KIND, ref=str(item.interaction_id)))
