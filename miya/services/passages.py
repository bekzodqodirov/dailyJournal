"""Passages: what was actually said, indexed for recall (WP-55).

Every stored message, transcript, document and note becomes one or more
passages — the words verbatim, a normalised form for full-text and trigram
matching, and (for anything long enough) a vector. Each passage knows who
said it and in whose chat, so recall can filter by person without guessing.
Rows are (re)indexed by the worker; the WP-39 listener resets a row whose
words changed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import ChatMonitor, Interaction, Memory, Passage, Person
from miya.services.embeddings import Embedder
from miya.services.ingest import text_for_extraction
from miya.services.text import normalise_for_search

log = logging.getLogger(__name__)

NOT_INDEXED_KINDS = ("window", "question", "client_import")
# Phone money texts that must never reach a search result: one-time codes
# (never egress to the model), adverts, re-posts, other apps' pushes.
NOT_INDEXED_MONEY = ("otp", "advert", "repeat", "not_payment_app")
CONTEXT_LINES = 2
CONTEXT_CHARS = 300
PHONE_SOURCES = (
    InteractionSource.phone_call,
    InteractionSource.phone_sms,
    InteractionSource.phone_notification,
)
OWNER_SOURCES = (
    InteractionSource.assistant_bot,
    InteractionSource.manual,
    InteractionSource.receipt_photo,
)


def _pending_media(interaction: Interaction, now: datetime) -> bool:
    media = interaction.media
    return (
        isinstance(media, dict)
        and media.get("processed") is False
        and interaction.occurred_at > now - timedelta(hours=1)
    )


def should_index(interaction: Interaction) -> bool:
    if (interaction.meta or {}).get("kind") in NOT_INDEXED_KINDS:
        return False
    if interaction.source is InteractionSource.calendar:
        return False
    money = ((interaction.media or {}).get("money") or {}) if interaction.media else {}
    if money.get("reason") in NOT_INDEXED_MONEY:
        return False
    return bool(full_text(interaction).strip())


def full_text(interaction: Interaction) -> str:
    return text_for_extraction(interaction)


def chunk(text: str) -> list[str]:
    """One chunk up to PASSAGE_CHUNK_CHARS; longer text cut at a line or
    sentence end, each chunk overlapping the last by PASSAGE_CHUNK_OVERLAP."""
    size = settings.passage_chunk_chars
    overlap = min(settings.passage_chunk_overlap, size // 2)
    text = text.strip()
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n"), window.rfind(". "), window.rfind("? "))
            if cut > size // 2:
                end = start + cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def attribution(
    interaction: Interaction, chat_type: ChatType | None
) -> tuple[int | None, int | None, bool]:
    """(speaker_person_id, chat_person_id, from_owner)."""
    source = interaction.source
    out = interaction.direction is Direction.out
    if source is InteractionSource.telegram_userbot:
        if chat_type is ChatType.private:
            if out:
                return None, interaction.person_id, True
            return interaction.person_id, interaction.person_id, False
        if out:
            return None, None, True
        return interaction.person_id, None, False
    if source in PHONE_SOURCES:
        return None, interaction.person_id, False
    if source in OWNER_SOURCES:
        return None, None, True
    return None, interaction.person_id, False


def _line(who: str, text: str) -> str:
    return f"{who}: {' '.join(text.split())[:CONTEXT_CHARS]}"


async def _context(session: AsyncSession, interaction: Interaction, names) -> list[str]:
    """Up to two lines before this one in the same chat."""
    if interaction.source is not InteractionSource.telegram_userbot or not (
        interaction.tg_chat_id
    ):
        return []
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(
                Interaction.tg_chat_id == interaction.tg_chat_id,
                Interaction.source == InteractionSource.telegram_userbot,
                Interaction.occurred_at < interaction.occurred_at,
                sa.func.coalesce(Interaction.meta["kind"].astext, "") != "window",
            )
            .order_by(Interaction.occurred_at.desc())
            .limit(CONTEXT_LINES)
        )
    )
    return [
        _line(_who(row, names), row.raw_text or row.transcript or "")
        for row in reversed(rows)
        if (row.raw_text or row.transcript)
    ]


def _who(interaction: Interaction, names: dict[int, str]) -> str:
    if interaction.direction is Direction.out or interaction.source in OWNER_SOURCES:
        return "ME"
    return names.get(interaction.person_id or 0, "THEM")


async def index_pending(session: AsyncSession, *, limit: int | None = None) -> int:
    """(Re)write passages for interactions not indexed yet; returns how many
    interactions were handled."""
    limit = limit or settings.passage_index_batch
    now = datetime.now(settings.tz)
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(Interaction.search_indexed_at.is_(None))
            .order_by(Interaction.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    chats = {r.tg_chat_id for r in rows if r.tg_chat_id}
    chat_types = (
        dict(
            (
                await session.execute(
                    sa.select(ChatMonitor.tg_chat_id, ChatMonitor.chat_type).where(
                        ChatMonitor.tg_chat_id.in_(chats)
                    )
                )
            ).all()
        )
        if chats
        else {}
    )
    titles = (
        dict(
            (
                await session.execute(
                    sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title).where(
                        ChatMonitor.tg_chat_id.in_(chats)
                    )
                )
            ).all()
        )
        if chats
        else {}
    )
    person_ids = {r.person_id for r in rows if r.person_id}
    names = (
        dict(
            (
                await session.execute(
                    sa.select(Person.id, Person.display_name).where(
                        Person.id.in_(person_ids)
                    )
                )
            ).all()
        )
        if person_ids
        else {}
    )

    handled = 0
    for interaction in rows:
        if _pending_media(interaction, now):
            continue  # the transcript is still on its way
        if not should_index(interaction):
            interaction.search_indexed_at = now
            handled += 1
            continue
        await session.execute(
            sa.delete(Passage).where(Passage.interaction_id == interaction.id)
        )
        speaker, chat_person, from_owner = attribution(
            interaction, chat_types.get(interaction.tg_chat_id)
        )
        where = titles.get(interaction.tg_chat_id) or interaction.source.value
        who = _who(interaction, names)
        context = await _context(session, interaction, names)
        for number, body in enumerate(chunk(full_text(interaction))):
            embed_text = "\n".join([*context, f"{where}: {who}: {body}"])
            session.add(
                Passage(
                    interaction_id=interaction.id,
                    chunk_no=number,
                    source=interaction.source,
                    tg_chat_id=interaction.tg_chat_id,
                    chat_person_id=chat_person,
                    speaker_person_id=speaker,
                    from_owner=from_owner,
                    media_kind=(interaction.media or {}).get("type"),
                    occurred_at=interaction.occurred_at,
                    body=body,
                    embed_text=embed_text,
                    search_norm=normalise_for_search(body),
                )
            )
        interaction.search_indexed_at = now
        handled += 1
    for memory in await session.scalars(
        sa.select(Memory)
        .where(Memory.search_norm.is_(None))
        .order_by(Memory.id)
        .limit(limit)
    ):
        memory.search_norm = normalise_for_search(memory.content)
    await session.flush()
    return handled


async def embed_pending(
    session: AsyncSession, embedder: Embedder, *, batch: int | None = None
) -> int:
    """Vectors for passages long enough to be worth one. EmbeddingError
    propagates: the rows stay NULL and the next tick retries."""
    batch = batch or settings.passage_embed_batch
    rows = list(
        await session.scalars(
            sa.select(Passage)
            .where(Passage.embedding.is_(None))
            .where(
                sa.func.length(Passage.search_norm) >= settings.passage_embed_min_chars
            )
            .order_by(Passage.id)
            .limit(batch)
        )
    )
    if not rows:
        return 0
    vectors = await embedder.embed([p.embed_text for p in rows])
    for passage, vector in zip(rows, vectors, strict=True):
        passage.embedding = vector
    await session.flush()
    return len(rows)
