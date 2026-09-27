"""WP-62: the recall eval — fixed questions, expected message ids, in two
modes: words only, and words plus a deterministic bag-of-words embedder."""

from __future__ import annotations

import hashlib
import math

import pytest

from miya.db import models as m
from miya.services import purge, recall
from miya.services.embeddings import Embedder
from miya.services.text import normalise_for_search
from tests.recall_fixture import FIXTURE_NOW, build

DIM = 1024


class BowEmbedder(Embedder):
    """Hashes normalised tokens into DIM buckets, L2-normalised."""

    name = "bow"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * DIM
        for token in normalise_for_search(text).split():
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]


def _hits(result, ids) -> list[str]:
    """Hit keys in rank order (episodes are ranked; lines within in time)."""
    by_id = {v: k for k, v in ids.items() if isinstance(v, int)}
    keys = []
    for episode in result.episodes:
        for line in episode.lines:
            if line.hit:
                key = by_id.get(int(line.ref[1:].split("#")[0]))
                if key and key not in keys:
                    keys.append(key)
    return keys


def _all_lines(result, ids) -> list[str]:
    by_id = {v: k for k, v in ids.items() if isinstance(v, int)}
    return [
        by_id.get(int(line.ref[1:].split("#")[0]), "?")
        for episode in result.episodes
        for line in episode.lines
    ]


def _episode_of(result, ids, key):
    target = f"m{ids[key]}"
    for episode in result.episodes:
        if any(line.ref.split("#")[0] == target and line.hit for line in episode.lines):
            return episode
    return None


def _c01(result, ids, hits):
    episode = _episode_of(result, ids, "a3")
    assert episode is not None
    assert f"m{ids['a5']}" in [line.ref for line in episode.lines]


def _c10(result, ids, hits):
    assert "w3" in hits
    assert "w1" not in hits or hits.index("w3") < hits.index("w1")


def _c13(result, ids, hits):
    assert f"f{ids['f2']}" in [fact.ref for fact in result.facts]


# (id, question, kwargs, expected ⊆ hits, forbidden, top-n or None, extra)
CASES = [
    (
        "C01",
        "Akmal bilan konteyner masalasi bo'lgandi, nima deb o'ylaysan?",
        {},
        {"a2", "a3"},
        {"t1", "t2", "q1", "win1"},
        None,
        _c01,
    ),
    ("C02", "Акмал контейнер", {}, {"a2", "a3"}, set(), None, None),
    ("C03", "bojxonada ushlab qolgan yuk", {}, {"g2", "call1"}, set(), 5, None),
    ("C04", "GS367 nima bo'ldi", {}, {"g6", "b1"}, set(), None, None),
    ("C05", "YW26-004715", {}, {"g6"}, set(), None, None),
    ("C06", "Ван оплата когда", {}, {"w2"}, set(), None, None),
    (
        "C07",
        "o'tgan hafta Sardor nima degan edi",
        {},
        {"g6", "call1"},
        {"g0", "g2"},
        None,
        None,
    ),
    ("C08", "Dilnoza bilan Dubay safari haqida", {}, set(), None, None, None),
    ("C09", "Akmal ovozli xabarda nima degandi", {}, {"a3"}, set(), None, None),
    ("C10", "Ван цена", {}, {"w3"}, set(), None, _c10),
    (
        "C12",
        "Akmal konteyner nima bo'ldi",
        {},
        set(),
        {"q1"},
        None,
        None,
    ),
    (
        "C13",
        "sertifikat",
        {},
        {"g2", "g3", "g5", "call1", "note1"},
        set(),
        6,
        _c13,
    ),
    ("C14", "kontener", {}, {"a2"}, set(), None, None),
]

MODES = ["lexical", "bow"]
_scores: dict[str, list[float]] = {mode: [] for mode in MODES}


@pytest.fixture(params=MODES)
async def seeded(request, session):
    embedder = BowEmbedder() if request.param == "bow" else None
    ids = await build(session, embedder)
    return request.param, embedder, ids


@pytest.mark.parametrize(
    ("case", "question", "kwargs", "expected", "forbidden", "top", "extra"),
    CASES,
    ids=[c[0] for c in CASES],
)
async def test_recall_case(
    session, seeded, case, question, kwargs, expected, forbidden, top, extra
):
    mode, embedder, ids = seeded
    result = await recall.search(session, embedder, question, now=FIXTURE_NOW, **kwargs)
    hits = _hits(result, ids)
    ranked = hits[:top] if top else hits
    found = expected & set(ranked)
    if expected:
        _scores[mode].append(len(found) / len(expected))
        print(f"{mode} {case}: recall {len(found)}/{len(expected)} hits={hits}")
    assert expected <= set(ranked), (mode, case, hits)
    if forbidden is None:
        assert result.is_empty(), (mode, case, hits, [f.text for f in result.facts])
    else:
        assert not (forbidden & set(_all_lines(result, ids))), (mode, case, hits)
    if case == "C09":
        media = {e.where for e in result.episodes}
        assert all("ovozli xabar" in where for where in media), media
    if extra is not None:
        extra(result, ids, hits)


async def test_c11_after_forgetting_akmal_karimov(session, seeded):
    mode, embedder, ids = seeded
    person = await session.get(m.Person, ids["people"]["AkmalK"])
    plan = await purge.plan_person(session, person)
    await purge.execute(session, plan)

    result = await recall.search(session, embedder, "konteyner", now=FIXTURE_NOW)
    lines = set(_all_lines(result, ids))

    assert not {k for k in lines if k.startswith("a")}
    assert not lines & {"g3", "g5", "g7"}
    assert "g2" in _hits(result, ids)
    window = await session.get(m.ConversationWindow, ids["window"])
    await session.refresh(window)
    assert "Akmal" not in window.text


def teardown_module(module):
    for mode, values in _scores.items():
        if values:
            print(f"recall@k {mode}: {sum(values) / len(values):.2f} over {len(values)}")
