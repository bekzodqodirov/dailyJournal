"""Embedding backfill and semantic search (Phase 3).

The embedder is stubbed with deterministic vectors; pgvector's cosine ranking
and the NULL-embedding backfill queue are real.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.services import memories
from miya.services.embeddings import Embedder

TZ = settings.tz
DIM = settings.embed_dim


class FakeEmbedder(Embedder):
    """Deterministic unit vectors; specific texts can be pinned via mapping."""

    name = "fake"

    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        if text in self.mapping:
            return self.mapping[text]
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b - 127.5 for b in digest] * (DIM // len(digest))
        norm = sum(x * x for x in raw) ** 0.5
        return [x / norm for x in raw]


def onehot(i: int) -> list[float]:
    vector = [0.0] * DIM
    vector[i] = 1.0
    return vector


def _memory(content: str) -> m.Memory:
    return m.Memory(content=content, occurred_at=datetime.now(TZ), tags=[])


async def test_embed_pending_fills_the_null_queue(session):
    session.add_all([_memory("a"), _memory("b"), _memory("c")])
    await session.flush()
    embedder = FakeEmbedder()

    assert await memories.pending_count(session) == 3
    assert await memories.embed_pending(session, embedder) == 3
    assert await memories.pending_count(session) == 0

    vectors = list(await session.scalars(sa.select(m.Memory.embedding)))
    assert all(v is not None and len(v) == DIM for v in vectors)


async def test_embed_pending_with_nothing_queued_never_calls_the_model(session):
    embedder = FakeEmbedder()
    assert await memories.embed_pending(session, embedder) == 0
    assert embedder.calls == 0


async def test_search_ranks_by_cosine_similarity(session):
    embedder = FakeEmbedder(
        mapping={
            "bojxona to'lovi": onehot(0),
            "Akmal yangi mashina oldi": onehot(1),
            "Guangzhou yuk jo'natildi": onehot(2),
            # The query points mostly at index 1, slightly at index 2.
            "mashina": [0.9 if i == 1 else (0.4 if i == 2 else 0.0) for i in range(DIM)],
        }
    )
    session.add_all(
        [
            _memory("bojxona to'lovi"),
            _memory("Akmal yangi mashina oldi"),
            _memory("Guangzhou yuk jo'natildi"),
        ]
    )
    await session.flush()
    await memories.embed_pending(session, embedder)

    hits = await memories.search(session, embedder, "mashina", k=2)

    assert [h.memory.content for h in hits] == [
        "Akmal yangi mashina oldi",
        "Guangzhou yuk jo'natildi",
    ]
    assert hits[0].similarity > hits[1].similarity > 0


async def test_search_ignores_unembedded_rows(session):
    session.add(_memory("hali embedding yo'q"))
    await session.flush()
    hits = await memories.search(session, FakeEmbedder(), "nimadir", k=5)
    assert hits == []


async def test_search_with_a_blank_query_is_a_noop(session):
    embedder = FakeEmbedder()
    assert await memories.search(session, embedder, "   ") == []
    assert embedder.calls == 0
