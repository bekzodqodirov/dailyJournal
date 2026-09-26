"""WP-62: paraphrase recall with the real bge-m3 embedder.

Needs the model weights (~3 GB); runs only with MIYA_EVAL_REAL_EMBEDDINGS=1.
Prints the similarities used to tune RECALL_MIN_SIMILARITY.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from miya.db import models as m
from miya.services import recall
from tests.recall_fixture import FIXTURE_NOW, build
from tests.test_recall_eval import _hits

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("MIYA_EVAL_REAL_EMBEDDINGS") != "1",
        reason="MIYA_EVAL_REAL_EMBEDDINGS=1",
    ),
]

CASES = [
    ("R1", "yuk kechikishi haqida Akmal nima degan", {"a3"}),
    ("R2", "to'lov qachon bo'ladi Wang", {"w2"}),
    ("R3", "soliq hisoboti", {"d1"}),
]


async def test_paraphrases_are_found_with_real_embeddings(session):
    from miya.services.embeddings import LocalEmbedder

    embedder = LocalEmbedder()
    ids = await build(session, embedder)
    found = 0
    for case, question, expected in CASES:
        result = await recall.search(session, embedder, question, now=FIXTURE_NOW)
        top = _hits(result, ids)[:5]
        found += len(expected & set(top))
        [vector] = await embedder.embed([question])
        distance = m.Passage.embedding.cosine_distance(vector)
        rows = (
            await session.execute(
                sa.select(m.Passage.interaction_id, 1 - distance)
                .where(m.Passage.embedding.isnot(None))
                .order_by(distance)
                .limit(5)
            )
        ).all()
        by_id = {v: k for k, v in ids.items() if isinstance(v, int)}
        sims = ", ".join(f"{by_id.get(i, i)}={s:.3f}" for i, s in rows)
        print(f"{case}: top5={top} similarities: {sims}")
    total = sum(len(expected) for _, _, expected in CASES)
    print(f"recall@5 real: {found / total:.2f}")
    assert found / total >= 0.8
