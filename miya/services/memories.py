"""The RAG store: embedding backfill and semantic search (spec §8).

Facts land in ``memories`` with ``embedding = NULL`` at extraction time (the
extraction path must never wait on a 2 GB model). A worker job calls
``embed_pending`` shortly after; ``search`` serves ``/qidir`` and the RAG chat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.models import Memory
from miya.services.embeddings import Embedder

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


async def search(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    k: int = 8,
    since: datetime | None = None,
) -> list[MemoryHit]:
    """Top-k memories by cosine similarity to the query."""
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

    return [
        MemoryHit(memory=row[0], similarity=1.0 - float(row[1]))
        for row in (await session.execute(stmt)).all()
    ]
