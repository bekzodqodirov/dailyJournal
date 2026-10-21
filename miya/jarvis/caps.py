"""Daily and per-chat caps, counted in SQL (docs/jarvis-plan.md §5.7, rule 22).

Every count starts at ``reminders.day_start`` (midnight on the owner's
clock) and reads only ``outbox_messages`` and ``escalations``: nothing is
kept in memory, so a restart never resets a cap. Jarvis never writes
``question_log`` and never spends ``QUESTION_BUDGET_PER_DAY``.

A row counts as sent from the moment it is claimed: ``sending`` and
``unknown`` rows may already be on the wire, so they count like ``sent``
ones (a crash never makes room for one more message). Their moment is the
claim, ``lease_until - SEND_LEASE``, until ``sent_at`` is known.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Escalation, OutboxMessage
from miya.services.reminders import day_start

# How long a claimed row is the adapter's before reconcile() may look at it.
SEND_LEASE: Final = timedelta(minutes=5)

# The states a row is (possibly) on the wire in.
WIRE_STATES: Final = ("sending", "sent", "unknown")
AUTO_APPROVER: Final = "auto:policy"
AI: Final = "ai"
DRAFT_STATE: Final = "draft"
SHADOW_STATE: Final = "shadow"
# A draft created over the daily cap carries this last_error: it is kept,
# never pushed as a card, and listed in the morning brief instead (JV-18).
DRAFT_CAP_CODE: Final = "draft_cap"


def _wire_moment() -> sa.ColumnElement[datetime]:
    return sa.func.coalesce(
        OutboxMessage.sent_at,
        OutboxMessage.lease_until - sa.literal(SEND_LEASE, sa.Interval()),
    )


def _on_wire_since(start: datetime) -> sa.ColumnElement[bool]:
    return sa.and_(OutboxMessage.state.in_(WIRE_STATES), _wire_moment() >= start)


async def _count(session: AsyncSession, *where: sa.ColumnElement[bool]) -> int:
    value = await session.scalar(
        sa.select(sa.func.count()).select_from(OutboxMessage).where(*where)
    )
    return int(value or 0)


@dataclass(frozen=True, slots=True)
class DayCounts:
    """Today's Jarvis activity, the numbers behind every cap and /jarvis."""

    # AI drafts stored today, shown or not: rows that were (or became) a
    # draft or a shadow draft. Blocked-at-create and auto-approved rows are
    # not drafts.
    drafts: int
    # AI draft cards that count towards JARVIS_DRAFT_DAILY_CAP.
    draft_cards: int
    # Rows sent or on the wire today, whoever wrote them.
    sent: int
    # AI rows sent on their own today (approved_by='auto:policy').
    auto_sent: int
    escalations: int


async def day_counts(session: AsyncSession, now: datetime) -> DayCounts:
    start = day_start(now)
    ai_today = sa.and_(OutboxMessage.authored_by == AI, OutboxMessage.created_at >= start)
    escalations = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Escalation)
        .where(Escalation.created_at >= start)
    )
    return DayCounts(
        drafts=await _count(
            session, ai_today, sa.or_(_became(DRAFT_STATE), _became(SHADOW_STATE))
        ),
        draft_cards=await _count(session, ai_today, _is_card()),
        sent=await _count(session, _on_wire_since(start)),
        auto_sent=await _count(
            session, _on_wire_since(start), OutboxMessage.approved_by == AUTO_APPROVER
        ),
        escalations=int(escalations or 0),
    )


def _became(state: str) -> sa.ColumnElement[bool]:
    """The row's history has a transition into ``state``."""
    return OutboxMessage.history.op("@>")(
        sa.cast(sa.literal([{"field": "state", "new": state}], JSONB), JSONB)
    )


def _is_card() -> sa.ColumnElement[bool]:
    """A draft card the owner is (or was) shown: the row was, or became, a
    draft, and was not created over the cap. Read from the history, which
    never changes, not from the mutable ``last_error``; ``create`` always
    writes the creation entry first."""
    return sa.and_(
        _became(DRAFT_STATE),
        sa.func.coalesce(OutboxMessage.history[0]["code"].astext, "") != DRAFT_CAP_CODE,
    )


async def draft_cap_reached(session: AsyncSession, now: datetime) -> bool:
    """JARVIS_DRAFT_DAILY_CAP draft cards were already made today."""
    counts = await day_counts(session, now)
    return counts.draft_cards >= settings.jarvis_draft_daily_cap


async def chat_ai_sent_today(session: AsyncSession, chat_key: str, now: datetime) -> int:
    """AI rows sent (or on the wire) to one chat today."""
    return await _count(
        session,
        _on_wire_since(day_start(now)),
        OutboxMessage.chat_key == chat_key,
        OutboxMessage.authored_by == AI,
    )


async def channel_sent_today(session: AsyncSession, channel: str, now: datetime) -> int:
    """Every row sent on a channel today: the platform's own daily cap."""
    return await _count(
        session, _on_wire_since(day_start(now)), OutboxMessage.channel == channel
    )


async def last_send_at(
    session: AsyncSession, channel: str, account: str | None = None
) -> datetime | None:
    """The latest send (or claim) from one account: the anti-ban gap.

    Owner-authored rows count too. Until the channel accounts table exists
    (0029) each channel has one account, so ``account`` narrows only when the
    adapter names one in ``claimed_by``.
    """
    query = sa.select(sa.func.max(_wire_moment())).where(
        OutboxMessage.state.in_(WIRE_STATES), OutboxMessage.channel == channel
    )
    if account is not None:
        query = query.where(OutboxMessage.claimed_by == account)
    value: datetime | None = await session.scalar(query)
    return value


def gap_until(last: datetime | None, now: datetime, gap_seconds: int) -> datetime | None:
    """When the next send may leave, or None when it may leave now."""
    if last is None or gap_seconds <= 0:
        return None
    ready = last + timedelta(seconds=gap_seconds)
    return ready if ready > now else None


def next_day(now: datetime) -> datetime:
    """Midnight after ``now`` on the owner's clock: when daily caps reset."""
    return day_start(day_start(now) + timedelta(hours=36))


__all__ = [
    "AUTO_APPROVER",
    "DRAFT_CAP_CODE",
    "SEND_LEASE",
    "WIRE_STATES",
    "DayCounts",
    "channel_sent_today",
    "chat_ai_sent_today",
    "day_counts",
    "draft_cap_reached",
    "gap_until",
    "last_send_at",
    "next_day",
]
