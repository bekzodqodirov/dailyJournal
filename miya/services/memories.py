"""The RAG store: embedding backfill and semantic search (spec §8).

Facts land in ``memories`` with ``embedding = NULL`` at extraction time (the
extraction path must never wait on a 2 GB model). A worker job calls
``embed_pending`` shortly after; ``search`` serves ``/qidir`` and the RAG chat.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Memory
from miya.services.embeddings import Embedder
from miya.services.text import normalise_for_search

log = logging.getLogger(__name__)

# One worker tick embeds at most this many memories; the rest wait a tick.
BACKFILL_BATCH = 128


@dataclass(slots=True)
class MemoryHit:
    memory: Memory
    similarity: float  # 1.0 = identical direction, 0.0 = orthogonal


async def pending_count(session: AsyncSession) -> int:
    return (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Memory)
            .where(Memory.embedding.is_(None))
        )
        or 0
    )


async def embed_pending(
    session: AsyncSession, embedder: Embedder, *, batch: int = BACKFILL_BATCH
) -> int:
    """Embed up to ``batch`` memories that don't have a vector yet.

    Returns how many were embedded. Raises EmbeddingError upward — the caller
    (worker job) logs and retries next tick; rows stay NULL, nothing is lost.
    """
    rows = list(
        await session.scalars(
            sa.select(Memory)
            .where(Memory.embedding.is_(None))
            .order_by(Memory.id)
            .limit(batch)
        )
    )
    if not rows:
        return 0

    vectors = await embedder.embed([m.content for m in rows])
    for memory, vector in zip(rows, vectors, strict=True):
        memory.embedding = vector
    await session.flush()
    return len(rows)


async def remember(
    session: AsyncSession,
    content: str,
    *,
    person_id: int | None = None,
    occurred_at: datetime | None = None,
    source_interaction_id: int | None = None,
    tags: Iterable[str] = (),
) -> Memory:
    """The one place a memory row is built (extraction facts, summaries, /eslab).

    The embedding is left NULL so ``embed_pending`` picks the row up on the
    next worker tick; nothing here waits on the model.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("a memory needs some content")
    memory = Memory(
        content=text,
        embedding=None,
        person_id=person_id,
        occurred_at=occurred_at or datetime.now(settings.tz),
        tags=list(tags),
        source_interaction_id=source_interaction_id,
        search_norm=normalise_for_search(text),
    )
    session.add(memory)
    return memory


async def facts_for(
    session: AsyncSession, person_id: int, *, limit: int = 20
) -> list[Memory]:
    """What is remembered about one person, newest first — no embedding needed."""
    return list(
        await session.scalars(
            sa.select(Memory)
            .where(Memory.person_id == person_id)
            .order_by(Memory.occurred_at.desc(), Memory.id.desc())
            .limit(limit)
        )
    )


async def search(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    k: int = 8,
    since: datetime | None = None,
    person_id: int | None = None,
    until: datetime | None = None,
    person_ids: list[int] | None = None,
) -> list[MemoryHit]:
    """Top-k memories by cosine similarity to the query.

    ``person_id`` narrows the ranking to that person's facts — a fact about
    Akmal is never an answer about Sardor, however similar the words.
    """
    query = query.strip()
    if not query:
        return []
    [vector] = await embedder.embed([query])

    distance = Memory.embedding.cosine_distance(vector)
    stmt = (
        sa.select(Memory, distance.label("distance"))
        .where(Memory.embedding.isnot(None))
        .order_by(distance)
        .limit(k)
    )
    if since is not None:
        stmt = stmt.where(Memory.occurred_at >= since)
    if person_id is not None:
        stmt = stmt.where(Memory.person_id == person_id)
    if until is not None:
        stmt = stmt.where(Memory.occurred_at < until)
    if person_ids:
        stmt = stmt.where(Memory.person_id.in_(person_ids))

    return [
        MemoryHit(memory=row[0], similarity=1.0 - float(row[1]))
        for row in (await session.execute(stmt)).all()
    ]
