"""Hybrid recall: find what was actually said (WP-56).

Lexical (full-text prefix), trigram (typos) and vector search over the
passages (WP-55) and the memories, fused by reciprocal rank with a recency
factor, filtered by person, date, chat and kind of source, and returned as
cited episodes — the hit lines with the lines around them, dated, with the
chat and the speaker. When the embedder is down, the lexical results stand.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import ChatMonitor, Interaction, Memory, Passage, Person
from miya.services import people as people_mod
from miya.services import queries
from miya.services.embeddings import Embedder, EmbeddingError
from miya.services.text import (
    UZ_SUFFIXES,
    normalise_for_search,
    query_terms,
    stem_query_token,
    to_prefix_tsquery,
)

log = logging.getLogger(__name__)

EPISODE_GAP = timedelta(minutes=15)
MAX_EPISODES = 8
MAX_LINES = 12
CONTEXT_CHARS = 500
HIT_CHARS = 1200
NEIGHBOUR_CHARS = 200
MAX_FACTS = 5
FUZZY_MIN_LEXICAL = 5
FUZZY_MIN_TERM = 5
# pg_trgm's default word-similarity floor (0.6) misses a dropped letter in
# a long word ("kontener" / "konteyner" is 0.58).
FUZZY_THRESHOLD = 0.5
RRF_K = 60

MONTHS = {
    "yanvar": 1,
    "fevral": 2,
    "mart": 3,
    "aprel": 4,
    "may": 5,
    "iyun": 6,
    "iyul": 7,
    "avgust": 8,
    "sentabr": 9,
    "sentyabr": 9,
    "oktabr": 10,
    "oktyabr": 10,
    "noyabr": 11,
    "dekabr": 12,
    # Russian, in the prepositional "в сентябре" form, transliterated.
    "yanvare": 1,
    "fevrale": 2,
    "marte": 3,
    "aprele": 4,
    "mae": 5,
    "iyune": 6,
    "iyule": 7,
    "avguste": 8,
    "sentyabre": 9,
    "oktyabre": 10,
    "noyabre": 11,
    "dekabre": 12,
}
PERIOD_WORDS = {
    "bugun",
    "kecha",
    "otgan",
    "kuni",
    "bu",
    "shu",
    "hafta",
    "oy",
    "yakinda",
    "segodnya",
    "vchera",
    "na",
    "proshloy",
    "nedele",
    "v",
    *MONTHS,
}
# A cue word says which kind of source is meant; the generic nouns that go
# with it ("ovozli xabarda") are not content either.
SOURCE_CUES = {
    "ovozli": "voice",
    "golosov": "voice",
    "golosovoe": "voice",
    "kongirok": "call",
    "zvonok": "call",
    "hujjat": "document",
    "fayl": "document",
    "dokument": "document",
    "sms": "sms",
}
CUE_NOUNS = {"xabar", "soobshchenie", "message"}


@dataclass(slots=True)
class Line:
    ref: str
    when: datetime
    who: str
    text: str
    hit: bool


@dataclass(slots=True)
class Episode:
    ref: str
    when: datetime
    where: str
    source_label: str
    lines: list[Line] = field(default_factory=list)


@dataclass(slots=True)
class Fact:
    ref: str
    when: datetime
    about: str | None
    text: str


@dataclass(slots=True)
class ParsedQuery:
    content_terms: list[str] = field(default_factory=list)
    person: Person | None = None
    person_candidates: list[Person] = field(default_factory=list)
    period: tuple[date, date] | None = None
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecallResult:
    episodes: list[Episode] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    content_terms: list[str] = field(default_factory=list)
    person: Person | None = None
    person_candidates: list[Person] = field(default_factory=list)
    period: tuple[date, date] | None = None
    degraded: str | None = None

    def is_empty(self) -> bool:
        return not (self.episodes or self.facts)


# --- parsing the question ----------------------------------------------------------


def _matches(tokens: list[str], key: list[str]) -> list[int] | None:
    """Positions of ``key`` in ``tokens``; the last token may carry a case
    ending ("Sardorga")."""
    n = len(key)
    for start in range(len(tokens) - n + 1):
        window = tokens[start : start + n]
        if window[:-1] != key[:-1]:
            continue
        last, want = window[-1], key[-1]
        if last == want or (last.startswith(want) and last[len(want) :] in UZ_SUFFIXES):
            return list(range(start, start + n))
    return None


def _person_keys(person: Person) -> list[list[str]]:
    keys = []
    for name in [person.display_name, *(person.aliases or [])]:
        key = normalise_for_search(people_mod.normalise(name or "")).split()
        if key and key not in keys:
            keys.append(key)
    return keys


def _month_range(month: int, today: date) -> tuple[date, date]:
    year = today.year if month <= today.month else today.year - 1
    start = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def _month_of(token: str) -> int | None:
    if token in MONTHS:
        return MONTHS[token]
    for suffix in ("dagi", "da"):
        if token.endswith(suffix) and token[: -len(suffix)] in MONTHS:
            return MONTHS[token[: -len(suffix)]]
    return None


def _period(norm: str, today: date) -> tuple[date, date] | None:
    monday = today - timedelta(days=today.weekday())
    if "otgan kuni" in norm:
        day = today - timedelta(days=2)
        return day, day
    if "otgan hafta" in norm or "na proshloy nedele" in norm:
        start = monday - timedelta(days=7)
        return start, start + timedelta(days=6)
    if re.search(r"\b(bu|shu) hafta", norm):
        return monday, today
    if "otgan oy" in norm:
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if re.search(r"\b(bu|shu) oy\b", norm):
        return today.replace(day=1), today
    tokens = norm.split()
    for token in tokens:
        match = re.fullmatch(r"(\d{1,2})-([a-z]+)", token)
        if match and _month_of(match.group(2)):
            month = _month_of(match.group(2))
            start, _ = _month_range(month, today)
            try:
                day = start.replace(day=int(match.group(1)))
            except ValueError:
                continue
            return day, day
    for index, token in enumerate(tokens):
        if token.isdigit() and index + 1 < len(tokens) and _month_of(tokens[index + 1]):
            start, _ = _month_range(_month_of(tokens[index + 1]), today)
            try:
                day = start.replace(day=int(token))
            except ValueError:
                continue
            return day, day
    for token in tokens:
        month = _month_of(token)
        if month:
            return _month_range(month, today)
    if "bugun" in tokens or "segodnya" in tokens:
        return today, today
    if "kecha" in tokens or "vchera" in tokens:
        day = today - timedelta(days=1)
        return day, day
    if "yakinda" in tokens:
        return today - timedelta(days=14), today
    return None


def parse_question(text: str, people: list[Person], now: datetime) -> ParsedQuery:
    """Who, when, what kind, and which words remain to search for."""
    norm = normalise_for_search(text)
    tokens = norm.split()
    today = now.astimezone(settings.tz).date()
    parsed = ParsedQuery(period=_period(norm, today))

    found: dict[int, Person] = {}
    used: set[int] = set()
    for person in people:
        for key in _person_keys(person):
            positions = _matches(tokens, key)
            if positions is not None:
                found[person.id] = person
                used.update(positions)
    if len(found) == 1:
        parsed.person = next(iter(found.values()))
    elif len(found) > 1:
        parsed.person_candidates = list(found.values())

    removable: set[str] = set()
    for index in used:
        removable.add(tokens[index])
    for token in tokens:
        is_date = re.fullmatch(r"\d{1,2}(-\w+)?", token)
        if token in PERIOD_WORDS or _month_of(token) or is_date:
            removable.add(token)
        cue = next((c for w, c in SOURCE_CUES.items() if token.startswith(w)), None)
        if cue:
            removable.add(token)
            if cue not in parsed.sources:
                parsed.sources.append(cue)
    if parsed.sources:
        removable |= {t for t in tokens if any(t.startswith(n) for n in CUE_NOUNS)}
    stems = {stem_query_token(t) for t in removable} | removable
    parsed.content_terms = [t for t in query_terms(text) if t not in stems]
    return parsed


# --- retrieval ------------------------------------------------------------------------

SOURCE_FILTERS = {
    "voice": Passage.media_kind.in_(("voice", "audio", "video_note")),
    "call": Passage.source == InteractionSource.phone_call,
    "document": Passage.media_kind == "document",
    "sms": Passage.source == InteractionSource.phone_sms,
    "telegram": Passage.source == InteractionSource.telegram_userbot,
    "note": Passage.source.in_(
        (InteractionSource.assistant_bot, InteractionSource.manual)
    ),
    "app": Passage.source == InteractionSource.phone_notification,
}


def _name_tsquery(person: Person) -> str | None:
    terms = []
    for key in _person_keys(person):
        terms.extend(key)
    return to_prefix_tsquery(list(dict.fromkeys(terms)))


def _passage_filters(date_from, date_to, person, chat, sources) -> list:
    filters = []
    if date_from is not None:
        filters.append(Passage.occurred_at >= date_from)
    if date_to is not None:
        filters.append(Passage.occurred_at < date_to)
    if chat is not None:
        filters.append(Passage.tg_chat_id == chat)
    if sources:
        filters.append(
            sa.or_(*(SOURCE_FILTERS[s] for s in sources if s in SOURCE_FILTERS))
        )
    if person is not None:
        mention = _name_tsquery(person)
        who = [
            Passage.speaker_person_id == person.id,
            Passage.chat_person_id == person.id,
        ]
        if mention:
            who.append(Passage.search_tsv.op("@@")(sa.func.to_tsquery("simple", mention)))
        filters.append(sa.or_(*who))
    return filters


async def _ranked(session, stmt) -> list[int]:
    return [row[0] for row in (await session.execute(stmt)).all()]


def _bounds(period: tuple[date, date] | None):
    if period is None:
        return None, None
    start, _ = queries.day_bounds(period[0])
    _, end = queries.day_bounds(period[1])
    return start, end


async def search(
    session: AsyncSession,
    embedder: Embedder | None,
    text: str,
    *,
    now: datetime,
    person: Person | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    chat: int | None = None,
    sources: list[str] | None = None,
    k: int | None = None,
) -> RecallResult:
    """The passages and facts that answer ``text``, as cited episodes."""
    k = k or settings.recall_top_k
    everyone = list(await session.scalars(sa.select(Person)))
    parsed = parse_question(text, everyone, now)
    person = person or parsed.person
    sources = sources if sources is not None else parsed.sources
    result = RecallResult(
        content_terms=parsed.content_terms,
        person=person,
        person_candidates=[] if person else parsed.person_candidates,
        period=parsed.period,
    )
    explicit = date_from is not None or date_to is not None
    if explicit:
        start = queries.day_bounds(date_from)[0] if date_from else None
        end = queries.day_bounds(date_to)[1] if date_to else None
    elif parsed.period is not None and not parsed.content_terms:
        start, end = _bounds(parsed.period)
    else:
        start = end = None
    soft_start, soft_end = (None, None) if explicit else _bounds(parsed.period)
    filters = _passage_filters(start, end, person, chat, sources)
    candidates = settings.recall_candidates
    rankings: list[list[int]] = []
    memory_rankings: list[list[int]] = []
    tsquery = to_prefix_tsquery(parsed.content_terms)

    if parsed.content_terms and tsquery:
        q = sa.func.to_tsquery("simple", tsquery)
        lexical = await _ranked(
            session,
            sa.select(Passage.id)
            .where(Passage.search_tsv.op("@@")(q), *filters)
            .order_by(
                sa.func.ts_rank_cd(Passage.search_tsv, q, 32).desc(),
                Passage.occurred_at.desc(),
            )
            .limit(candidates),
        )
        rankings.append(lexical)
        if len(lexical) < FUZZY_MIN_LEXICAL:
            await session.execute(
                sa.text(
                    f"SET LOCAL pg_trgm.word_similarity_threshold = {FUZZY_THRESHOLD}"
                )
            )
            for term in [t for t in parsed.content_terms if len(t) >= FUZZY_MIN_TERM]:
                rankings.append(
                    await _ranked(
                        session,
                        sa.select(Passage.id)
                        .where(sa.literal(term).op("<%")(Passage.search_norm), *filters)
                        .order_by(
                            sa.func.word_similarity(term, Passage.search_norm).desc()
                        )
                        .limit(candidates),
                    )
                )
        memory_filters = []
        if start is not None:
            memory_filters.append(Memory.occurred_at >= start)
        if end is not None:
            memory_filters.append(Memory.occurred_at < end)
        if person is not None:
            memory_filters.append(Memory.person_id == person.id)
        memory_rankings.append(
            await _ranked(
                session,
                sa.select(Memory.id)
                .where(Memory.search_tsv.op("@@")(q), *memory_filters)
                .order_by(sa.func.ts_rank_cd(Memory.search_tsv, q, 32).desc())
                .limit(candidates),
            )
        )
        if embedder is not None:
            try:
                [vector] = await embedder.embed([text])
            except EmbeddingError as exc:
                log.warning("recall without meaning search: %s", exc)
                result.degraded = "semantic_unavailable"
            else:
                distance = Passage.embedding.cosine_distance(vector)
                if not filters:
                    await session.execute(sa.text("SET LOCAL hnsw.ef_search = 100"))
                rows = (
                    await session.execute(
                        sa.select(Passage.id, distance)
                        .where(Passage.embedding.is_not(None), *filters)
                        .order_by(distance)
                        .limit(candidates)
                    )
                ).all()
                rankings.append(
                    [
                        pid
                        for pid, d in rows
                        if 1 - float(d) >= settings.recall_min_similarity
                    ]
                )
                memory_distance = Memory.embedding.cosine_distance(vector)
                rows = (
                    await session.execute(
                        sa.select(Memory.id, memory_distance)
                        .where(Memory.embedding.is_not(None), *memory_filters)
                        .order_by(memory_distance)
                        .limit(candidates)
                    )
                ).all()
                memory_rankings.append(
                    [
                        mid
                        for mid, d in rows
                        if 1 - float(d) >= settings.recall_min_similarity
                    ]
                )
    elif person or parsed.person_candidates or parsed.period or sources:
        listing = list(filters)
        if not person and parsed.person_candidates:
            ids = [p.id for p in parsed.person_candidates]
            listing.append(
                sa.or_(
                    Passage.speaker_person_id.in_(ids), Passage.chat_person_id.in_(ids)
                )
            )
        if start is None and parsed.period is not None:
            s, e = _bounds(parsed.period)
            listing += [Passage.occurred_at >= s, Passage.occurred_at < e]
        rankings.append(
            await _ranked(
                session,
                sa.select(Passage.id)
                .where(*listing)
                .order_by(Passage.occurred_at.desc())
                .limit(candidates),
            )
        )

    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, pid in enumerate(ranking, start=1):
            scores[pid] += 1 / (RRF_K + rank)
    if not scores and not any(memory_rankings):
        return result
    rows = {
        p.id: p
        for p in await session.scalars(sa.select(Passage).where(Passage.id.in_(scores)))
    }
    candidate_ids = {p.id for p in result.person_candidates}
    best: dict[int, tuple[float, Passage]] = {}
    for pid, score in scores.items():
        passage = rows.get(pid)
        if passage is None:
            continue
        age = max((now - passage.occurred_at).total_seconds() / 86400, 0)
        score *= 1 + settings.recall_recency_weight * math.exp(
            -age / settings.recall_recency_halflife_days
        )
        if soft_start is not None and soft_start <= passage.occurred_at < soft_end:
            score *= 1.5
        if candidate_ids and (
            passage.speaker_person_id in candidate_ids
            or passage.chat_person_id in candidate_ids
        ):
            score *= 1.3
        current = best.get(passage.interaction_id)
        if current is None or score > current[0]:
            best[passage.interaction_id] = (score, passage)
    top = sorted(best.values(), key=lambda item: item[0], reverse=True)[:k]
    result.episodes = await _episodes(session, [p for _, p in top])

    memory_scores: dict[int, float] = defaultdict(float)
    for ranking in memory_rankings:
        for rank, mid in enumerate(ranking, start=1):
            memory_scores[mid] += 1 / (RRF_K + rank)
    if memory_scores:
        facts = list(
            await session.scalars(sa.select(Memory).where(Memory.id.in_(memory_scores)))
        )
        facts.sort(key=lambda f: memory_scores[f.id], reverse=True)
        about = {
            p.id: p.display_name for p in everyone if p.id in {f.person_id for f in facts}
        }
        result.facts = [
            Fact(
                ref=f"f{f.id}",
                when=f.occurred_at,
                about=about.get(f.person_id),
                text=f.content,
            )
            for f in facts[:MAX_FACTS]
        ]
    return result


# --- episodes ---------------------------------------------------------------------------


def _ref(passage: Passage) -> str:
    return f"m{passage.interaction_id}" + (
        f"#{passage.chunk_no}" if passage.chunk_no else ""
    )


async def _labels(session: AsyncSession, interactions: list[Interaction]):
    chats = {i.tg_chat_id for i in interactions if i.tg_chat_id}
    monitors = {
        m.tg_chat_id: m
        for m in await session.scalars(
            sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id.in_(chats))
        )
    }
    ids = {i.person_id for i in interactions if i.person_id}
    names = {
        p.id: p.display_name
        for p in await session.scalars(sa.select(Person).where(Person.id.in_(ids)))
    }
    return monitors, names


def where_label(interaction: Interaction, monitors, names) -> str:
    source = interaction.source
    media = interaction.media or {}
    name = names.get(interaction.person_id or 0)
    if source is InteractionSource.telegram_userbot:
        monitor = monitors.get(interaction.tg_chat_id)
        title = (monitor.title if monitor else None) or name or "chat"
        if monitor is not None and monitor.chat_type is ChatType.group:
            label = f"«{title}» guruhi"
        elif monitor is not None and monitor.chat_type is ChatType.channel:
            label = f"«{title}» kanali"
        else:
            label = f"{name or title} bilan shaxsiy chat"
    elif source is InteractionSource.phone_call:
        label = f"qo'ng'iroq ({name or media.get('number') or media.get('phone') or '?'})"
    elif source is InteractionSource.phone_sms:
        label = f"SMS ({media.get('sender') or '?'})"
    elif source is InteractionSource.phone_notification:
        label = f"ilova xabari ({media.get('package') or '?'})"
    else:
        label = "sizning eslatmangiz"
    kind = media.get("type")
    if kind in ("voice", "audio"):
        label += " · ovozli xabar"
    elif kind in ("video_note", "video"):
        label += " · video xabar"
    elif kind == "document":
        label += f" · hujjat: {media.get('filename') or 'fayl'}"
    return label


def _who(interaction: Interaction, names) -> str:
    if interaction.direction is Direction.out or interaction.source in (
        InteractionSource.assistant_bot,
        InteractionSource.manual,
    ):
        return "Siz"
    if interaction.source is InteractionSource.phone_call:
        return "qo'ng'iroq"
    return names.get(interaction.person_id or 0, "noma'lum")


def _text(interaction: Interaction) -> str:
    return (interaction.raw_text or interaction.transcript or "").strip()


async def _chat_lines(session, chat: int, first: datetime, last: datetime):
    n = settings.recall_context_lines
    member = sa.and_(
        Interaction.tg_chat_id == chat,
        Interaction.source == InteractionSource.telegram_userbot,
        sa.func.coalesce(Interaction.meta["kind"].astext, "") != "window",
    )
    before = list(
        await session.scalars(
            sa.select(Interaction)
            .where(member, Interaction.occurred_at < first)
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
            .limit(n)
        )
    )
    middle = list(
        await session.scalars(
            sa.select(Interaction)
            .where(
                member, Interaction.occurred_at >= first, Interaction.occurred_at <= last
            )
            .order_by(Interaction.occurred_at, Interaction.id)
        )
    )
    after = list(
        await session.scalars(
            sa.select(Interaction)
            .where(member, Interaction.occurred_at > last)
            .order_by(Interaction.occurred_at, Interaction.id)
            .limit(n)
        )
    )
    return [*reversed(before), *middle, *after]


async def _episodes(session: AsyncSession, hits: list[Passage]) -> list[Episode]:
    if not hits:
        return []
    interactions = {
        i.id: i
        for i in await session.scalars(
            sa.select(Interaction).where(
                Interaction.id.in_({p.interaction_id for p in hits})
            )
        )
    }
    groups: list[list[Passage]] = []
    for passage in sorted(hits, key=lambda p: p.occurred_at):
        last = groups[-1][-1] if groups else None
        if (
            last is not None
            and passage.source is InteractionSource.telegram_userbot
            and last.source is InteractionSource.telegram_userbot
            and passage.tg_chat_id == last.tg_chat_id
            and passage.occurred_at - last.occurred_at <= EPISODE_GAP
        ):
            groups[-1].append(passage)
        else:
            groups.append([passage])
    groups = groups[:MAX_EPISODES]

    context_rows: dict[int, list[Interaction]] = {}
    for index, group in enumerate(groups):
        if group[0].source is InteractionSource.telegram_userbot and group[0].tg_chat_id:
            context_rows[index] = await _chat_lines(
                session, group[0].tg_chat_id, group[0].occurred_at, group[-1].occurred_at
            )
    everyone = list(interactions.values()) + [
        r for rows in context_rows.values() for r in rows
    ]
    monitors, names = await _labels(session, everyone)

    episodes: list[Episode] = []
    for index, group in enumerate(groups):
        first = interactions[group[0].interaction_id]
        hit_ids = {p.interaction_id for p in group}
        if index in context_rows:
            lines = [
                Line(
                    ref=f"m{row.id}",
                    when=row.occurred_at,
                    who=_who(row, names),
                    text=_text(row)[: HIT_CHARS if row.id in hit_ids else CONTEXT_CHARS],
                    hit=row.id in hit_ids,
                )
                for row in context_rows[index]
                if _text(row)
            ]
            if len(lines) > MAX_LINES:
                hits_at = [i for i, line in enumerate(lines) if line.hit]
                centre = hits_at[0] if hits_at else 0
                start = max(0, min(centre - MAX_LINES // 2, len(lines) - MAX_LINES))
                lines = lines[start : start + MAX_LINES]
        else:
            passage = group[0]
            neighbours = {
                p.chunk_no: p.body
                for p in await session.scalars(
                    sa.select(Passage).where(
                        Passage.interaction_id == passage.interaction_id,
                        Passage.chunk_no.in_(
                            (passage.chunk_no - 1, passage.chunk_no + 1)
                        ),
                    )
                )
            }
            before = neighbours.get(passage.chunk_no - 1, "")[-NEIGHBOUR_CHARS:]
            after = neighbours.get(passage.chunk_no + 1, "")[:NEIGHBOUR_CHARS]
            body = passage.body[:HIT_CHARS]
            text = (
                (f"…{before} " if before else "") + body + (f" {after}…" if after else "")
            )
            lines = [
                Line(
                    ref=_ref(passage),
                    when=passage.occurred_at,
                    who=_who(first, names),
                    text=text,
                    hit=True,
                )
            ]
        episodes.append(
            Episode(
                ref=_ref(group[0]),
                when=group[0].occurred_at,
                where=where_label(first, monitors, names),
                source_label=first.source.value,
                lines=lines,
            )
        )
    return episodes
