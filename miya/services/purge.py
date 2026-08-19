"""Purge tooling (spec §10): `/unut <odam | chat | sana oralig'i>`.

Deleting is the one operation MIYA can do that the owner cannot undo, so the
flow is deliberately two-step: build a **plan** that says exactly what would
disappear, show it, and only then execute.

Everything derived from an interaction hangs off ``source_interaction_id`` with
``ON DELETE CASCADE``, so removing the interactions removes the debts,
promises, transactions, events, tasks and memories that came out of them. Media
files on disk are unlinked in the same pass — a purge that left the audio
behind would not be a purge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import (
    ChatMonitor,
    ConversationWindow,
    Debt,
    Event,
    Interaction,
    Memory,
    Person,
    Promise,
    Task,
    Transaction,
)
from miya.services.queries import day_bounds

log = logging.getLogger(__name__)

# Media keys that hold a filesystem path.
_PATH_KEYS = ("path", "audio_path")


@dataclass(slots=True)
class PurgePlan:
    """What a purge would remove. Nothing is deleted until `execute` runs."""

    kind: str  # person | chat | range
    label: str  # human description, already safe to show
    interaction_ids: list[int] = field(default_factory=list)
    person_id: int | None = None
    tg_chat_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    counts: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    # Windows holding the rendered transcript of the purged messages. They must
    # go too: a pending one would be re-extracted hours later and recreate the
    # person and debts the owner just deleted.
    window_ids: list[int] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.interaction_ids and not self.counts.get("debts")


@dataclass(slots=True)
class PurgeResult:
    interactions: int = 0
    files_deleted: int = 0
    person_deleted: bool = False


async def _count(session: AsyncSession, model, column, ids: list[int]) -> int:
    if not ids:
        return 0
    return (
        await session.scalar(
            sa.select(sa.func.count()).select_from(model).where(column.in_(ids))
        )
        or 0
    )


async def _collect(session: AsyncSession, plan: PurgePlan) -> PurgePlan:
    """Fill in the derived-row counts and media paths for a plan's interactions."""
    ids = plan.interaction_ids
    plan.counts = {
        "interactions": len(ids),
        "debts": await _count(session, Debt, Debt.source_interaction_id, ids),
        "promises": await _count(session, Promise, Promise.source_interaction_id, ids),
        "transactions": await _count(
            session, Transaction, Transaction.source_interaction_id, ids
        ),
        "events": await _count(session, Event, Event.source_interaction_id, ids),
        "tasks": await _count(session, Task, Task.source_interaction_id, ids),
        "memories": await _count(session, Memory, Memory.source_interaction_id, ids),
    }

    if ids:
        media_rows = await session.scalars(
            sa.select(Interaction.media).where(
                Interaction.id.in_(ids), Interaction.media.isnot(None)
            )
        )
        files: list[str] = []
        for media in media_rows:
            for key in _PATH_KEYS:
                value = (media or {}).get(key)
                if value:
                    files.append(value)
        plan.files = files

        window_ids = await session.scalars(
            sa.select(Interaction.window_id)
            .where(Interaction.id.in_(ids), Interaction.window_id.isnot(None))
            .distinct()
        )
        plan.window_ids = [w for w in window_ids if w]
    return plan


async def plan_person(session: AsyncSession, person: Person) -> PurgePlan:
    """Forget a person: their messages, their debts, everything derived."""
    ids = list(
        await session.scalars(
            sa.select(Interaction.id).where(Interaction.person_id == person.id)
        )
    )
    plan = PurgePlan(
        kind="person",
        label=person.display_name,
        interaction_ids=ids,
        person_id=person.id,
    )
    await _collect(session, plan)
    # Windows attributed to this person even if their messages were already
    # purged: the window still holds the rendered transcript verbatim.
    person_windows = await session.scalars(
        sa.select(ConversationWindow.id).where(ConversationWindow.person_id == person.id)
    )
    plan.window_ids = sorted({*plan.window_ids, *person_windows})
    # Debts and promises hang off the person too, not only off an interaction.
    plan.counts["debts"] = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Debt)
            .where(Debt.person_id == person.id)
        )
        or 0
    )
    plan.counts["promises"] = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Promise)
            .where(Promise.person_id == person.id)
        )
        or 0
    )
    return plan


async def plan_chat(session: AsyncSession, tg_chat_id: int) -> PurgePlan:
    ids = list(
        await session.scalars(
            sa.select(Interaction.id).where(Interaction.tg_chat_id == tg_chat_id)
        )
    )
    monitor = await session.scalar(
        sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == tg_chat_id)
    )
    plan = PurgePlan(
        kind="chat",
        label=(monitor.title if monitor and monitor.title else str(tg_chat_id)),
        interaction_ids=ids,
        tg_chat_id=tg_chat_id,
    )
    return await _collect(session, plan)


async def plan_range(session: AsyncSession, date_from: date, date_to: date) -> PurgePlan:
    start, _ = day_bounds(date_from)
    _, end = day_bounds(date_to)
    ids = list(
        await session.scalars(
            sa.select(Interaction.id).where(
                Interaction.occurred_at >= start, Interaction.occurred_at < end
            )
        )
    )
    plan = PurgePlan(
        kind="range",
        label=f"{date_from.isoformat()} … {date_to.isoformat()}",
        interaction_ids=ids,
        date_from=date_from,
        date_to=date_to,
    )
    return await _collect(session, plan)


def _unlink(paths: list[str]) -> int:
    """Remove media files. A missing file is already in the desired state."""
    deleted = 0
    for raw in paths:
        path = Path(raw)
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("could not delete media file %s", path, exc_info=True)
    return deleted


async def execute(session: AsyncSession, plan: PurgePlan) -> PurgeResult:
    """Carry out a plan. Rows go first; files only after the delete succeeds."""
    result = PurgeResult(interactions=len(plan.interaction_ids))

    if plan.interaction_ids:
        # Cascades take the derived rows (debts, promises, memories, …).
        await session.execute(
            sa.delete(Interaction).where(Interaction.id.in_(plan.interaction_ids))
        )

    if plan.window_ids:
        # A surviving window is a second copy of the conversation, and a
        # pending one would be extracted hours later — recreating the person
        # and the debts the owner just asked to forget, and billing him for it.
        await session.execute(
            sa.delete(ConversationWindow).where(
                ConversationWindow.id.in_(plan.window_ids)
            )
        )

    if plan.kind == "chat" and plan.tg_chat_id is not None:
        await session.execute(
            sa.delete(ConversationWindow).where(
                ConversationWindow.tg_chat_id == plan.tg_chat_id
            )
        )
    elif plan.kind == "range" and plan.date_from and plan.date_to:
        start, _ = day_bounds(plan.date_from)
        _, end = day_bounds(plan.date_to)
        await session.execute(
            sa.delete(ConversationWindow).where(
                ConversationWindow.ended_at >= start,
                ConversationWindow.ended_at < end,
            )
        )
    elif plan.kind == "person" and plan.person_id is not None:
        # Deleting the person cascades to their debts and promises, including
        # ones that came from a call the owner is otherwise keeping.
        await session.execute(sa.delete(Person).where(Person.id == plan.person_id))
        result.person_deleted = True

    await session.flush()
    result.files_deleted = _unlink(plan.files)
    log.warning(
        "purged %s %r: %d interactions, %d files",
        plan.kind,
        plan.label,
        result.interactions,
        result.files_deleted,
    )
    return result


def parse_range(text: str) -> tuple[date, date] | None:
    """Accept `2026-08-01..2026-08-15` or a single `2026-08-01`."""
    text = text.strip()
    if ".." in text:
        left, _, right = text.partition("..")
    else:
        left = right = text
    try:
        start = date.fromisoformat(left.strip())
        end = date.fromisoformat(right.strip())
    except ValueError:
        return None
    if end < start:
        start, end = end, start
    return start, end


def today() -> date:
    return datetime.now(settings.tz).date()
