"""Conversation windowing for the userbot stream (spec §7B).

The userbot stores every monitored message as its own interaction. Extracting
each one alone would be both expensive and wrong — "5 mln beraman" only means
something next to the message before it. So messages accumulate per chat and a
worker job flushes a window when **any** of the spec's three triggers fires:

  * ``WINDOW_IDLE_MINUTES`` of silence in that chat (default 30), or
  * ``WINDOW_MAX_MESSAGES`` messages buffered (default 25), or
  * ``WINDOW_MAX_CHARS`` characters buffered (default 4,000).

Buffering lives in the database, not in process memory: a userbot restart or a
worker crash can therefore never lose a message that was waiting for its
window. A message is "claimed" the moment ``window_id`` is set, so no message
is ever extracted twice.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource, WindowStatus
from miya.db.models import ConversationWindow, Interaction, Person
from miya.services.ingest import text_for_extraction

log = logging.getLogger(__name__)

MEDIA_PLACEHOLDER = {
    "voice": "[ovozli xabar]",
    "video_note": "[video xabar]",
    "photo": "[rasm]",
    "video": "[video]",
    "document": "[hujjat]",
}


@dataclass(slots=True)
class _Candidate:
    interaction: Interaction
    body: str


def render_line(interaction: Interaction, speaker: str) -> str:
    """One `[timestamp] [SPEAKER] text` line of a window transcript."""
    stamp = interaction.occurred_at.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")
    body = text_for_extraction(interaction).strip()
    if not body:
        media_type = (interaction.media or {}).get("type", "")
        body = MEDIA_PLACEHOLDER.get(media_type, "[media]")
        filename = (interaction.media or {}).get("filename")
        if filename and media_type == "document":
            body = f"[hujjat: {filename}]"
    return f"[{stamp}] [{speaker}] {body}"


def _speaker(interaction: Interaction, names: dict[int, str]) -> str:
    if interaction.direction is Direction.out:
        return "ME"
    name = names.get(interaction.person_id or -1)
    return f"THEM ({name})" if name else "THEM"


def render_window(interactions: list[Interaction], names: dict[int, str]) -> str:
    return "\n".join(render_line(i, _speaker(i, names)) for i in interactions)


def _content_length(interaction: Interaction) -> int:
    return len(text_for_extraction(interaction))


def _slice_to_flush(
    interactions: list[Interaction], *, now: datetime
) -> list[Interaction] | None:
    """The messages that should become a window right now, or None to wait."""
    if not interactions:
        return None

    # Message-count trigger.
    if len(interactions) >= settings.window_max_messages:
        return interactions[: settings.window_max_messages]

    # Character trigger: cut after the message that crosses the limit, so a
    # single very long message still forms a window of its own.
    running = 0
    for index, interaction in enumerate(interactions):
        running += _content_length(interaction)
        if running >= settings.window_max_chars:
            return interactions[: index + 1]

    # Idle trigger.
    idle_after = timedelta(minutes=settings.window_idle_minutes)
    if now - interactions[-1].occurred_at >= idle_after:
        return interactions
    return None


async def _unclaimed_by_chat(session: AsyncSession) -> dict[int, list[Interaction]]:
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(Interaction.source == InteractionSource.telegram_userbot)
            .where(Interaction.window_id.is_(None))
            .where(Interaction.tg_chat_id.isnot(None))
            .order_by(Interaction.tg_chat_id, Interaction.occurred_at, Interaction.id)
        )
    )
    grouped: dict[int, list[Interaction]] = {}
    for row in rows:
        grouped.setdefault(row.tg_chat_id, []).append(row)
    return grouped


async def _person_names(
    session: AsyncSession, interactions: list[Interaction]
) -> dict[int, str]:
    ids = {i.person_id for i in interactions if i.person_id}
    if not ids:
        return {}
    rows = await session.execute(
        sa.select(Person.id, Person.display_name).where(Person.id.in_(ids))
    )
    return dict(rows.all())


async def flush_ready_windows(
    session: AsyncSession, *, now: datetime | None = None
) -> list[ConversationWindow]:
    """Turn every chat's ready backlog into windows. Returns what was created."""
    now = now or datetime.now(settings.tz)
    created: list[ConversationWindow] = []

    for tg_chat_id, backlog in (await _unclaimed_by_chat(session)).items():
        names = await _person_names(session, backlog)
        remaining = backlog
        # A long backlog (userbot was down, or a busy group) yields several
        # windows in one pass rather than one oversized prompt.
        while (batch := _slice_to_flush(remaining, now=now)) is not None:
            window = ConversationWindow(
                tg_chat_id=tg_chat_id,
                person_id=next((i.person_id for i in batch if i.person_id), None),
                started_at=batch[0].occurred_at,
                ended_at=batch[-1].occurred_at,
                message_count=len(batch),
                char_count=sum(_content_length(i) for i in batch),
                text=render_window(batch, names),
                status=WindowStatus.pending,
                custom_id=f"w-{uuid.uuid4().hex}",
            )
            session.add(window)
            await session.flush()
            for interaction in batch:
                interaction.window_id = window.id
            created.append(window)
            remaining = remaining[len(batch) :]

    if created:
        await session.flush()
        log.info("flushed %d conversation window(s)", len(created))
    return created


async def pending_windows(
    session: AsyncSession, *, limit: int = 200
) -> list[ConversationWindow]:
    return list(
        await session.scalars(
            sa.select(ConversationWindow)
            .where(ConversationWindow.status == WindowStatus.pending)
            .order_by(ConversationWindow.created_at)
            .limit(limit)
        )
    )
