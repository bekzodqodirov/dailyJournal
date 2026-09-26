"""Person resolution (spec §5, step 1).

Names arrive as the owner said them — "Akmal", "Akmal aka", "Akmal GZ" are one
person. A known `telegram_id` short-circuits; otherwise fuzzy-match the name
against every display name and alias, and create a new person when nothing is
close enough.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from rapidfuzz import fuzz, process
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Person
from miya.services.text import fold_apostrophes, to_latin

log = logging.getLogger(__name__)

# Writing a row about someone must be sure of who; a lower bar would merge
# "Akmal" and "Akmalov" into one ledger.
MATCH_THRESHOLD = 85
# Answering a question ("Akmal kim?", /kim, /tarix) may be more forgiving —
# a wrong guess costs a wrong answer, not a wrong debt — and a close second
# candidate is asked back instead of picked (see Match.ambiguous).
QUESTION_THRESHOLD = 70
AMBIGUITY_MARGIN = 10

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


def _is_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def normalise(name: str) -> str:
    """Casefold, Cyrillic to Latin, drop punctuation and honorifics, collapse
    whitespace. "Бекзод ака" and "Bekzod" compare equal (WP-28)."""
    cleaned = to_latin(fold_apostrophes(name).casefold())
    cleaned = re.sub(r"[^\w\s']", " ", cleaned, flags=re.UNICODE)
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
    if target in index:
        # An exact name wins before any fuzzy scoring: token_set_ratio gives
        # "Akmal" a perfect score against "Akmal Toshkent" too, and the tie
        # would send a fact about plain Akmal to whichever row is older.
        return index[target], 100.0

    # token_set_ratio so word order and extra words ("Akmal GZ" vs "GZ Akmal")
    # do not sink an otherwise obvious match.
    match = process.extractOne(target, index.keys(), scorer=fuzz.token_set_ratio)
    if match is None:
        return None, 0.0
    matched_name, score, _ = match
    return index[matched_name], float(score)


@dataclass(slots=True)
class Match:
    """The outcome of looking a name up for a question (build step 4).

    ``person`` is the best candidate at or above the threshold, or None.
    ``runner_up`` is the best *other* person, whatever its score, so a
    surface can name both when the two are too close to tell apart.
    """

    person: Person | None
    score: float
    runner_up: Person | None
    runner_up_score: float
    # The query equals one person's name or alias exactly. "Akmal" next to
    # "Akmal Toshkent" scores 100 against both; the exact one is meant.
    exact: bool = False

    @property
    def ambiguous(self) -> bool:
        """A person was found, but another one scores within the margin."""
        return (
            not self.exact
            and self.person is not None
            and self.runner_up is not None
            and self.score - self.runner_up_score <= AMBIGUITY_MARGIN
        )


def _person_score(target: str, person: Person) -> float:
    """The same scorer as best_match, per person: its best candidate name."""
    return max(
        (float(fuzz.token_set_ratio(target, c)) for c in _candidates(person)),
        default=0.0,
    )


async def find_person(
    session: AsyncSession, name: str, *, threshold: float = QUESTION_THRESHOLD
) -> Match:
    """Best and second-best person for ``name`` — loads people once.

    Never creates and never learns an alias: a question is not a contact.
    """
    target = normalise(name or "")
    if not target:
        return Match(person=None, score=0.0, runner_up=None, runner_up_score=0.0)
    people = list(await session.scalars(sa.select(Person).order_by(Person.id)))
    # Stable sort: on a tie the earlier row wins, as in best_match.
    ranked = sorted(
        ((_person_score(target, p), p) for p in people),
        key=lambda pair: pair[0],
        reverse=True,
    )
    exact = [p for p in people if target in set(_candidates(p))]
    if len(exact) > 1:
        # Two people literally sharing the name: ask back naming them both,
        # not the longer name that merely contains it.
        return Match(
            person=exact[0], score=100.0, runner_up=exact[1], runner_up_score=100.0
        )
    if len(exact) == 1:
        # Only two people literally sharing the name still ask back.
        person = exact[0]
        others = [(score, p) for score, p in ranked if p is not person]
        runner_up_score, runner_up = others[0] if others else (0.0, None)
        return Match(
            person=person,
            score=100.0,
            runner_up=runner_up,
            runner_up_score=runner_up_score,
            exact=True,
        )
    if not ranked or ranked[0][0] < threshold:
        best = ranked[0][0] if ranked else 0.0
        return Match(person=None, score=best, runner_up=None, runner_up_score=0.0)
    score, person = ranked[0]
    runner_up, runner_up_score = None, 0.0
    if len(ranked) > 1:
        runner_up_score, runner_up = ranked[1]
    return Match(
        person=person,
        score=score,
        runner_up=runner_up,
        runner_up_score=runner_up_score,
    )


def set_profile(person: Person, text: str, *, now: datetime | None = None) -> None:
    """Store the written profile and stamp when it was generated."""
    person.notes = (text or "").strip() or None
    person.profile_updated_at = now or datetime.now(settings.tz)


def set_relationship(person: Person, text: str | None) -> None:
    """Who this person is to the owner, in the owner's own words."""
    person.relationship_ = (text or "").strip() or None


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
            # Usernames change and phone numbers appear later; keep the row
            # current without ever overwriting what the owner typed himself.
            if telegram_username and existing.telegram_username != telegram_username:
                existing.telegram_username = telegram_username
            if phone and not existing.phone:
                existing.phone = phone
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
    # Two distinct Telegram accounts are two distinct people, whatever their
    # names look like. When the incoming message carries a telegram_id, any
    # candidate already bound to a *different* telegram_id must not fuzzy-merge
    # — "Akmal (supplier)" and a new "Akmal K" contact would otherwise become
    # one row and every debt of both would land on the first.
    if telegram_id is not None:
        people = [
            p for p in people if p.telegram_id is None or p.telegram_id == telegram_id
        ]
    person, score = best_match(name, people)

    if person is not None and score >= MATCH_THRESHOLD:
        names = [person.display_name, *(person.aliases or [])]
        known = {normalise(n) for n in names}
        # A spelling in the other script is kept too (WP-28): "Akmal" for
        # "Акмал" compares equal but is how the owner will type it.
        new_script = _is_cyrillic(name) not in {_is_cyrillic(n) for n in names if n}
        if normalise(name) not in known or new_script:
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
