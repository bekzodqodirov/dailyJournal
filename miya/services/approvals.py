"""Owner approval for media too big or too costly to fetch unasked (spec §6).

Three processes share this state and none of them can call the others: the
userbot sees the message and records the question, the worker asks it over the
assistant bot, and the bot records the answer. The database is the only thing
they have in common, so the state machine lives here rather than in any one of
them.

    pending ──asked──▶ asked ──yes──▶ approved ──fetched──▶ done
                          │  └──no───▶ declined
                          └──stale───▶ expired

It is kept inside ``interactions.media`` next to ``processed`` rather than in a
table of its own: it is a property of one piece of media, it is written by
whoever last touched that row, and a single owner never accumulates enough of
them to need an index.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Interaction

PENDING = "pending"
ASKED = "asked"
APPROVED = "approved"
DECLINED = "declined"
EXPIRED = "expired"
DONE = "done"


def state_of(interaction: Interaction) -> str | None:
    media = interaction.media
    if not isinstance(media, dict):
        return None
    approval = media.get("approval")
    if not isinstance(approval, dict):
        return None
    state = approval.get("state")
    return state if isinstance(state, str) else None


def reason_of(interaction: Interaction) -> str:
    media = interaction.media or {}
    approval = media.get("approval") if isinstance(media, dict) else None
    if isinstance(approval, dict):
        return str(approval.get("reason") or "")
    return ""


def set_state(interaction: Interaction, state: str, **fields) -> None:
    """Move one approval along. Rebinds ``media`` so SQLAlchemy sees the change.

    JSONB columns are mutated in place invisibly to the ORM — a nested
    ``media["approval"]["state"] = ...`` would never reach the database.
    """
    media = dict(interaction.media or {})
    approval = dict(media.get("approval") or {})
    approval["state"] = state
    approval.update(fields)
    media["approval"] = approval
    interaction.media = media


def _state_matches(*states: str):
    """A SQL predicate on the nested approval state.

    ``.astext`` rather than a dict comparison: the column holds JSON null for
    rows with no media at all, and a missing key must simply not match instead
    of raising.
    """
    return Interaction.media["approval"]["state"].astext.in_(states)


async def awaiting_question(
    session: AsyncSession, *, limit: int = 20
) -> list[Interaction]:
    """Media the owner has not been asked about yet, oldest first."""
    return list(
        await session.scalars(
            sa.select(Interaction)
            .where(_state_matches(PENDING))
            .order_by(Interaction.occurred_at)
            .limit(limit)
        )
    )


async def approved_for_fetch(
    session: AsyncSession, *, limit: int = 10
) -> list[Interaction]:
    """Media the owner said yes to and the userbot has not fetched yet."""
    return list(
        await session.scalars(
            sa.select(Interaction)
            .where(_state_matches(APPROVED))
            .order_by(Interaction.occurred_at)
            .limit(limit)
        )
    )


async def expire_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Retire questions the owner never answered.

    An unanswered question is an answer of its own after long enough, and
    leaving the buttons live means a tap weeks later starts a download for a
    file whose context is long gone.
    """
    now = now or datetime.now(settings.tz)
    cutoff = now - timedelta(hours=settings.media_ask_expiry_hours)
    stale = list(
        await session.scalars(
            sa.select(Interaction)
            .where(_state_matches(PENDING, ASKED))
            .where(Interaction.occurred_at < cutoff)
        )
    )
    for interaction in stale:
        set_state(interaction, EXPIRED)
    return len(stale)
