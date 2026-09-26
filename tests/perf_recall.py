"""WP-56: the recall retrievers use their indexes at scale.

Seeds 100k synthetic passages with random unit vectors (no model download)
and checks the plans. Prints timings; asserts no latency. Runs only with
MIYA_RUN_SLOW=1 (``pytest tests/perf_recall.py``).
"""

from __future__ import annotations

import os
import random
import time

import pytest
import sqlalchemy as sa

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("MIYA_RUN_SLOW") != "1", reason="MIYA_RUN_SLOW=1"),
]

ROWS = 100_000
DIM = 1024


def _vector() -> str:
    raw = [random.gauss(0, 1) for _ in range(DIM)]
    norm = sum(x * x for x in raw) ** 0.5
    return "[" + ",".join(f"{x / norm:.5f}" for x in raw) + "]"


async def test_the_retrievers_use_their_indexes(session):
    await session.execute(
        sa.text(
            "INSERT INTO interactions (source, direction, occurred_at, raw_text,"
            " processed, needs_review, search_indexed_at)"
            " VALUES ('assistant_bot', 'in', now(), 'x', true, false, now())"
        )
    )
    interaction_id = await session.scalar(sa.text("SELECT max(id) FROM interactions"))
    words = ["konteyner", "bojxona", "narx", "yuk", "invoys", "tolov", "mashina"]
    started = time.monotonic()
    for batch in range(ROWS // 1000):
        values = ",".join(
            f"({interaction_id}, {batch * 1000 + i}, 'assistant_bot', now(), 'b', 'e',"
            f" '{random.choice(words)} {random.choice(words)}', '{_vector()}')"
            for i in range(1000)
        )
        await session.execute(
            sa.text(
                "INSERT INTO passages (interaction_id, chunk_no, source, occurred_at,"
                f" body, embed_text, search_norm, embedding) VALUES {values}"
            )
        )
    await session.execute(sa.text("ANALYZE passages"))
    print(f"seeded {ROWS} passages in {time.monotonic() - started:.1f}s")

    lexical = (
        await session.execute(
            sa.text(
                "EXPLAIN SELECT id FROM passages WHERE search_tsv @@"
                " to_tsquery('simple', 'konteyner:*') LIMIT 40"
            )
        )
    ).all()
    assert "ix_passages_tsv" in " ".join(r[0] for r in lexical)

    await session.execute(sa.text("SET LOCAL hnsw.ef_search = 100"))
    semantic = (
        await session.execute(
            sa.text(
                "EXPLAIN SELECT id FROM passages WHERE embedding IS NOT NULL"
                f" ORDER BY embedding <=> '{_vector()}' LIMIT 40"
            )
        )
    ).all()
    assert "ix_passages_embedding_hnsw" in " ".join(r[0] for r in semantic)
