"""Namesake guard for "was this aimed at the owner?" (WP-38).

In a group where another Bekzod speaks, a bare "Bekzod" may be him. The
forms the owner named himself — "Bekzod aka", "bekzodaka", the @username —
always count; only a bare first name that another member of the chat also
carries is demoted, to "maybe to you", where /menga still shows it but no
window, loop or instant path acts on it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Interaction, Person
from miya.services import text as text_service
from miya.services.people import _HONORIFICS as HONORIFICS
from miya.services.people import normalise

CACHE_SECONDS = 600
_cache: dict[int, tuple[float, set[str]]] = {}


def clear_cache() -> None:
    _cache.clear()


def alias_stem(alias: str) -> str | None:
    """The first-name token an alias stands on: 'Bekzod aka' → 'bekzod',
    'bekzodaka' → 'bekzod', 'GSR Logistics' → 'gsr'; None for an @handle."""
    if not alias or alias.strip().startswith("@"):
        return None
    tokens = normalise(alias).split()
    if not tokens:
        return None
    stem = tokens[0]
    if len(tokens) == 1:
        for honorific in sorted(HONORIFICS, key=len, reverse=True):
            if stem.endswith(honorific) and len(stem) - len(honorific) >= 3:
                return stem[: -len(honorific)]
    return stem


def alias_hits(text: str, aliases) -> list[str]:
    """The aliases whose own pattern matches ``text``."""
    folded = text_service.fold_apostrophes(text or "")
    hits = []
    for alias in aliases:
        pattern = text_service.alias_pattern((alias,))
        if pattern is not None and pattern.search(folded):
            hits.append(alias)
    return hits


def has_honorific(alias: str) -> bool:
    """'Bekzod aka', 'bekzodaka', 'Бекзод ака': a form only the owner is."""
    squashed = text_service.to_latin(alias.lower().replace(" ", "").replace("-", ""))
    return squashed.endswith("aka")


def _tokens(person_names) -> set[str]:
    tokens: set[str] = set()
    for name in person_names:
        tokens.update(normalise(name or "").split())
    return tokens


async def namesake_tokens(
    session: AsyncSession, tg_chat_id: int, *, sender: Person | None = None
) -> set[str]:
    """Name tokens of everyone who spoke in this chat lately, plus the sender."""
    now = time.monotonic()
    cached = _cache.get(tg_chat_id)
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        tokens = set(cached[1])
    else:
        since = datetime.now(settings.tz) - timedelta(
            days=settings.owner_alias_namesake_days
        )
        speakers = (
            sa.select(Interaction.person_id)
            .where(
                Interaction.tg_chat_id == tg_chat_id,
                Interaction.source == InteractionSource.telegram_userbot,
                Interaction.direction == Direction.in_,
                Interaction.occurred_at >= since,
                Interaction.person_id.isnot(None),
            )
            .distinct()
        )
        rows = await session.execute(
            sa.select(Person.display_name, Person.aliases).where(Person.id.in_(speakers))
        )
        tokens = set()
        for display_name, aliases in rows.all():
            tokens |= _tokens([display_name, *(aliases or [])])
        _cache[tg_chat_id] = (now, set(tokens))
    if sender is not None:
        tokens |= _tokens([sender.display_name, *(sender.aliases or [])])
    return tokens


async def demote_if_namesake(
    session: AsyncSession,
    meta: dict,
    text: str,
    *,
    mentioned: bool,
    chat_id: int | None,
    sender: Person | None,
    aliases,
) -> dict:
    """``meta`` with to_me turned into to_me_maybe when every alias hit is
    a bare first name another member of the chat also carries."""
    if not meta.get("to_me") or mentioned or chat_id is None:
        return meta
    hits = alias_hits(text, aliases)
    if not hits:
        return meta
    candidates = [
        h
        for h in hits
        if not h.strip().startswith("@")
        and not has_honorific(h)
        and len(normalise(h).split()) == 1
    ]
    if len(candidates) != len(hits):
        return meta  # an honorific form or the @handle: always the owner
    tokens = await namesake_tokens(session, chat_id, sender=sender)
    shadowed = [h for h in candidates if alias_stem(h) in tokens]
    if len(shadowed) != len(hits):
        return meta
    demoted = {k: v for k, v in meta.items() if k != "to_me"}
    demoted["to_me_maybe"] = True
    demoted["namesake"] = alias_stem(shadowed[0])
    return demoted
