"""Person resolution (spec §5, step 1).

Names arrive as the owner said them — "Akmal", "Akmal aka", "Akmal GZ" are one
person. A known `telegram_id` short-circuits; otherwise fuzzy-match the name
against every display name and alias, and create a new person when nothing is
close enough.
"""

from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from rapidfuzz import fuzz, process
from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.models import Person

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 85

# Uzbek/Russian kinship and honorific suffixes carry no identity — "Akmal aka"
# and "Akmal" are the same person, so they are stripped before comparing.
_HONORIFICS = {
    "aka",
    "uka",
    "opa",
    "singil",
    "amaki",
    "tog'a",
    "toga",
    "xola",
    "amma",
    "bobo",
    "buva",
    "buvi",
    "ustoz",
    "domla",
    "brat",
    "bro",
}


def normalise(name: str) -> str:
    """Casefold, drop punctuation and honorifics, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s'’]", " ", name, flags=re.UNICODE).casefold()
    cleaned = cleaned.replace("’", "'")
    tokens = [t for t in cleaned.split() if t and t not in _HONORIFICS]
    return " ".join(tokens) or cleaned.strip()


def _candidates(person: Person) -> list[str]:
    return [normalise(n) for n in [person.display_name, *(person.aliases or [])] if n]


def best_match(name: str, people: list[Person]) -> tuple[Person | None, float]:
    """Highest-scoring person for `name`, and that score."""
    target = normalise(name)
    if not target:
        return None, 0.0

    index: dict[str, Person] = {}
    for person in people:
        for candidate in _candidates(person):
            index.setdefault(candidate, person)
    if not index:
        return None, 0.0

    # token_set_ratio so word order and extra words ("Akmal GZ" vs "GZ Akmal")
    # do not sink an otherwise obvious match.
    match = process.extractOne(target, index.keys(), scorer=fuzz.token_set_ratio)
    if match is None:
        return None, 0.0
    matched_name, score, _ = match
    return index[matched_name], float(score)


async def resolve_person(
    session: AsyncSession,
    name: str,
    *,
    telegram_id: int | None = None,
    telegram_username: str | None = None,
    phone: str | None = None,
    create: bool = True,
) -> Person | None:
    """Find or create the person `name` refers to.

    A matched person gains `name` as an alias when it is a new spelling, so the
    index improves with use.
    """
    if telegram_id is not None:
        existing = await session.scalar(
            sa.select(Person).where(Person.telegram_id == telegram_id)
        )
        if existing is not None:
            return existing

    name = (name or "").strip()
    if not name:
        return None

    # The bot and the call-recording worker are separate processes; without a
    # lock, two concurrent ingestions naming the same new person both pass the
    # not-found check and insert duplicates (there is no usable unique
    # constraint — matching is fuzzy). A transaction-scoped advisory lock on
    # the normalised name serialises exactly the conflicting pair: the second
    # writer waits for the first commit and then finds the person it created.
    await session.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                sa.func.hashtext("miya:person"), sa.func.hashtext(normalise(name))
            )
        )
    )

    people = list(await session.scalars(sa.select(Person)))
    person, score = best_match(name, people)

    if person is not None and score >= MATCH_THRESHOLD:
        known = {normalise(n) for n in [person.display_name, *(person.aliases or [])]}
        if normalise(name) not in known:
            # ORM change tracking does not see in-place list mutation.
            person.aliases = [*(person.aliases or []), name]
        if telegram_id is not None and person.telegram_id is None:
            person.telegram_id = telegram_id
        if telegram_username and not person.telegram_username:
            person.telegram_username = telegram_username
        if phone and not person.phone:
            person.phone = phone
        return person

    if not create:
        return None

    log.info("creating person %r (best score %.0f)", name, score)
    person = Person(
        display_name=name,
        aliases=[],
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        phone=phone,
    )
    session.add(person)
    await session.flush()
    return person


async def find_by_phone(session: AsyncSession, phone: str) -> Person | None:
    """Match a call recording's number against known contacts (Phase 2)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 7:
        return None
    # Compare on the last 9 digits so +998 / 998 / 0 prefixes all agree.
    tail = digits[-9:]
    return await session.scalar(
        sa.select(Person)
        .where(Person.phone.isnot(None))
        .where(
            sa.func.right(sa.func.regexp_replace(Person.phone, r"\D", "", "g"), 9) == tail
        )
        .limit(1)
    )
