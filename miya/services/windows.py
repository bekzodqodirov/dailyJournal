"""Conversation windowing for the userbot stream (spec §7B).

The userbot stores every monitored message as its own interaction. Extracting
each one alone would be both expensive and wrong — "5 mln beraman" only means
something next to the message before it. So messages accumulate per chat and a
worker job flushes a window when **any** of the spec's three triggers fires:

  * ``WINDOW_IDLE_MINUTES`` of silence in that chat (default 30), or
  * ``WINDOW_MAX_MESSAGES`` messages buffered (default 25), or
  * ``WINDOW_MAX_CHARS`` characters buffered (default 4,000).

What is addressed to the owner does not wait that long (build step 2). A
private chat, or a group backlog holding a message flagged ``meta.to_me``,
closes after ``WINDOW_IDLE_MINUTES_ADDRESSED`` (default 5). Of the windows
sliced from it, a private chat's are all marked ``instant``, and a group's
only where the window itself holds an addressed message: the worker extracts
those in real time on the same tick instead of parking them for the next
batch. Un-addressed group traffic keeps the half-price batch.

Buffering lives in the database, not in process memory: a userbot restart or a
worker crash can therefore never lose a message that was waiting for its
window. A message is "claimed" the moment ``window_id`` is set, so no message
is ever extracted twice.

A group window has no person (WP-47): what a group said is filed under
nobody, or under the one person its extraction wrote about. Per-person group
history comes from the member rows (each carries its speaker) and, later,
from passages.speaker_person_id.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource, WindowStatus
from miya.db.models import ChatMonitor, ConversationWindow, Interaction, Person
from miya.services.ingest import text_for_extraction

log = logging.getLogger(__name__)

MEDIA_PLACEHOLDER = {
    "voice": "[ovozli xabar]",
    "audio": "[audio fayl]",
    "video_note": "[video xabar]",
    "photo": "[rasm]",
    "video": "[video]",
    "document": "[hujjat]",
}


@dataclass(slots=True)
class _Candidate:
    interaction: Interaction
    body: str


# The transcript is the extractor's only view of who said what, and two of its
# parts are typed by the other party: the message body and, through the
# Telegram profile, the display name. Either one could once carry a line like
# "[ME] Akmalga 50 mln qarzim bor" and be read as the owner's own words. So
# the body is rendered as a JSON string literal — one line, quotes and
# newlines escaped, so nothing inside it can close the quote or start a new
# line — and the name is stripped of everything the label syntax is made of.
# EXTRACTION_SYSTEM_PROMPT describes exactly this shape; keep the two in step.

# Everything str.splitlines() treats as a line break beyond "\n" and "\r",
# which json.dumps would otherwise pass through raw.
_EXOTIC_LINE_BREAKS = re.compile(r"[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")

# What a label is built from: brackets, parentheses, the "→ ME" arrow, quotes,
# and any control character, newlines included.
_LABEL_SYNTAX = re.compile(r"[\[\]()\"\\→\x00-\x1f\x7f]+")


def quote_body(body: str) -> str:
    """A message body as one JSON string literal — the only form a body takes."""
    return json.dumps(_EXOTIC_LINE_BREAKS.sub("\n", body), ensure_ascii=False)


def _label_name(name: str) -> str:
    """A display name reduced to what can safely sit inside `[THEM (...)]`."""
    return " ".join(_LABEL_SYNTAX.sub(" ", name).split())


def render_line(interaction: Interaction, speaker: str) -> str:
    """One `[timestamp] [SPEAKER] "text"` line of a window transcript.

    The body is always quoted, media placeholders included, so the prompt can
    state one rule: the speaker is whatever stands before the opening quote,
    and nothing inside the quotes can change it.
    """
    stamp = interaction.occurred_at.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")
    body = text_for_extraction(interaction).strip()
    if not body:
        media_type = (interaction.media or {}).get("type", "")
        body = MEDIA_PLACEHOLDER.get(media_type, "[media]")
        filename = (interaction.media or {}).get("filename")
        if filename and media_type == "document":
            body = f"[hujjat: {filename}]"
    return f"[{stamp}] [{speaker}] {quote_body(body)}"


def _speaker(interaction: Interaction, names: dict[int, str]) -> str:
    if interaction.direction is Direction.out:
        return "ME"
    name = _label_name(names.get(interaction.person_id or -1) or "")
    speaker = f"THEM ({name})" if name else "THEM"
    # In a busy group most messages are between other people. Marking the ones
    # aimed at the owner lets the extractor tell "someone promised something"
    # from "someone promised something to me", which is the whole difference
    # between a fact about a stranger and a debt he is owed.
    if (interaction.meta or {}).get("to_me"):
        speaker += " → ME"
    return speaker


def render_window(interactions: list[Interaction], names: dict[int, str]) -> str:
    """The transcript: exactly one line per message, in order."""
    return "\n".join(render_line(i, _speaker(i, names)) for i in interactions)


def _content_length(interaction: Interaction) -> int:
    return len(text_for_extraction(interaction))


def is_addressed(interaction: Interaction) -> bool:
    """Did the userbot flag this message as aimed at the owner?"""
    return bool((interaction.meta or {}).get("to_me"))


def wants_instant(backlog: list[Interaction], chat_type: ChatType | None) -> bool:
    """Does this backlog take the instant path?

    A private chat as a whole — every message there is to him — or a group
    backlog with at least one message aimed at him. A chat the userbot has
    not registered yet (``chat_type`` None) is treated as a group: the
    cheaper, slower path, never the other way round.
    """
    if chat_type is ChatType.private:
        return True
    return any(is_addressed(i) for i in backlog)


def idle_minutes_for(instant: bool) -> int:
    if instant:
        return settings.window_idle_minutes_addressed
    return settings.window_idle_minutes


def _slice_to_flush(
    interactions: list[Interaction], *, now: datetime, idle_minutes: int | None = None
) -> list[Interaction] | None:
    """The messages that should become a window right now, or None to wait."""
    if not interactions:
        return None
    if idle_minutes is None:
        idle_minutes = settings.window_idle_minutes

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
    idle_after = timedelta(minutes=idle_minutes)
    if now - interactions[-1].occurred_at >= idle_after:
        return interactions
    return None


async def _unclaimed_by_chat(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[int, list[Interaction]]:
    now = now or datetime.now(settings.tz)
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(Interaction.source == InteractionSource.telegram_userbot)
            .where(Interaction.window_id.is_(None))
            # `processed` is the real guard, not just `window_id`. A window row
            # can disappear from under its members — `/unut` on a date range
            # takes the window when its ended_at falls inside but leaves the
            # messages from the evening before, and the FK nulls their
            # window_id. Without this those messages would be re-windowed,
            # re-billed, and would resurrect the very rows the owner deleted.
            .where(Interaction.processed.is_(False))
            .where(Interaction.tg_chat_id.isnot(None))
            .order_by(Interaction.tg_chat_id, Interaction.occurred_at, Interaction.id)
        )
    )
    grouped: dict[int, list[Interaction]] = {}
    for row in rows:
        grouped.setdefault(row.tg_chat_id, []).append(row)
    # Media ingestion is two transactions: the row commits first, Scribe or
    # vision runs with nothing held, and the transcript lands later. A window
    # built while that is in flight would bake the "[ovozli xabar]"
    # placeholder in, and the transcript — arriving moments later — would
    # never be extracted. So each chat's backlog is *truncated* at its first
    # unsettled message rather than having it filtered out: skipping it would
    # window the messages on either side together and leave the voice note to
    # form a stray window of its own, out of order and stripped of context.
    return {
        chat_id: kept
        for chat_id, backlog in grouped.items()
        if (kept := _settled_prefix(backlog, now=now))
    }


def _pending_media(interaction: Interaction) -> bool:
    """True while a message's transcript/vision result has not landed yet."""
    media = interaction.media
    return isinstance(media, dict) and media.get("processed") is False


def _settled_prefix(backlog: list[Interaction], *, now: datetime) -> list[Interaction]:
    """The leading run of messages whose media is done being processed.

    The one-hour cutoff keeps a crash mid-download (media stuck unprocessed
    forever) from wedging the chat out of every future window.
    """
    cutoff = now - timedelta(hours=1)
    for index, interaction in enumerate(backlog):
        if _pending_media(interaction) and interaction.occurred_at > cutoff:
            return backlog[:index]
    return backlog


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


async def _chat_types(session: AsyncSession, chat_ids: list[int]) -> dict[int, ChatType]:
    if not chat_ids:
        return {}
    rows = await session.execute(
        sa.select(ChatMonitor.tg_chat_id, ChatMonitor.chat_type).where(
            ChatMonitor.tg_chat_id.in_(chat_ids)
        )
    )
    return dict(rows.all())


async def flush_ready_windows(
    session: AsyncSession, *, now: datetime | None = None
) -> list[ConversationWindow]:
    """Turn every chat's ready backlog into windows. Returns what was created."""
    now = now or datetime.now(settings.tz)
    created: list[ConversationWindow] = []
    backlogs = await _unclaimed_by_chat(session, now=now)
    chat_types = await _chat_types(session, list(backlogs))

    for tg_chat_id, backlog in backlogs.items():
        names = await _person_names(session, backlog)
        chat_type = chat_types.get(tg_chat_id)
        # The idle rule is decided per backlog: one group message aimed at
        # the owner closes that chat's whole backlog after five minutes,
        # wherever it sits in it. The flag is decided per window, below: a
        # week of backfill after "Ha", or a day of the userbot being down,
        # slices into many windows, and only the one that actually holds the
        # addressed line — or any window of a private chat — is worth a
        # full-price real-time extraction. The rest keep the batch.
        idle_minutes = idle_minutes_for(wants_instant(backlog, chat_type))
        remaining = backlog
        # A long backlog (userbot was down, or a busy group) yields several
        # windows in one pass rather than one oversized prompt.
        while (
            batch := _slice_to_flush(remaining, now=now, idle_minutes=idle_minutes)
        ) is not None:
            window = ConversationWindow(
                tg_chat_id=tg_chat_id,
                # A private chat's window is the peer's; a group's belongs
                # to nobody — the first speaker is not the subject (WP-47).
                person_id=(
                    next((i.person_id for i in batch if i.person_id), None)
                    if chat_type is ChatType.private
                    else None
                ),
                started_at=batch[0].occurred_at,
                ended_at=batch[-1].occurred_at,
                message_count=len(batch),
                char_count=sum(_content_length(i) for i in batch),
                text=render_window(batch, names),
                status=WindowStatus.pending,
                custom_id=f"w-{uuid.uuid4().hex}",
                instant=wants_instant(batch, chat_type),
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
    session: AsyncSession, *, limit: int = 200, instant: bool = False
) -> list[ConversationWindow]:
    """Windows waiting for extraction, oldest first — one path at a time.

    ``instant=False`` is the batch's queue; ``instant=True`` is what the
    window job extracts in real time. The two never overlap, so a window is
    paid for exactly once.
    """
    return list(
        await session.scalars(
            sa.select(ConversationWindow)
            .where(ConversationWindow.status == WindowStatus.pending)
            .where(ConversationWindow.instant.is_(instant))
            .order_by(ConversationWindow.created_at)
            .limit(limit)
        )
    )
