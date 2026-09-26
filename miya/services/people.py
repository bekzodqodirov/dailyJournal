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
from typing import Literal

import sqlalchemy as sa
from rapidfuzz import fuzz
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import Person
from miya.services import codes
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


NameKey = tuple[str, frozenset[str]]


def _name_and_codes(name: str) -> NameKey:
    """A name as (normalised words without codes, its client codes), WP-29."""
    return normalise(codes.strip_codes(name)), frozenset(codes.find_client_codes(name))


def _candidates(person: Person) -> list[NameKey]:
    keys = [
        _name_and_codes(n) for n in [person.display_name, *(person.aliases or [])] if n
    ]
    return [k for k in keys if k[0] or k[1]]


def _score(query: NameKey, candidate: NameKey) -> float:
    """Codes compare exactly and decide alone; names compare fuzzily.

    GS368 is never a spelling of GS367: two different codes are two different
    clients whatever the names say.
    """
    q_name, q_codes = query
    c_name, c_codes = candidate
    if q_codes & c_codes:
        return 100.0
    if q_codes and c_codes:
        return 0.0
    if q_name and c_name:
        return float(fuzz.token_set_ratio(q_name, c_name))
    return 0.0


def _is_exact(query: NameKey, candidate: NameKey) -> bool:
    q_name, q_codes = query
    c_name, c_codes = candidate
    return bool(q_codes & c_codes) or (not q_codes and bool(q_name) and q_name == c_name)


def best_match(name: str, people: list[Person]) -> tuple[Person | None, float]:
    """Highest-scoring person for `name`, and that score."""
    target = _name_and_codes(name)
    if not (target[0] or target[1]):
        return None, 0.0
    # An exact name (or a held code) wins before any fuzzy scoring:
    # token_set_ratio gives "Akmal" a perfect score against "Akmal Toshkent"
    # too, and the tie would send a fact about plain Akmal to whichever row
    # is older.
    for person in people:
        if any(_is_exact(target, c) for c in _candidates(person)):
            return person, 100.0
    best: Person | None = None
    best_score = 0.0
    for person in people:
        score = _person_score(target, person)
        if best is None or score > best_score:
            best, best_score = person, score
    if best is None:
        return None, 0.0
    return best, best_score


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
    # WP-31: the person was found by a client code, or the code in the
    # query is held by nobody.
    via_code: str | None = None
    unknown_code: str | None = None

    @property
    def ambiguous(self) -> bool:
        """A person was found, but another one scores within the margin."""
        return (
            not self.exact
            and self.person is not None
            and self.runner_up is not None
            and self.score - self.runner_up_score <= AMBIGUITY_MARGIN
        )


def _person_score(target: NameKey, person: Person) -> float:
    """The same scorer as best_match, per person: its best candidate name."""
    return max((_score(target, c) for c in _candidates(person)), default=0.0)


async def find_person(
    session: AsyncSession, name: str, *, threshold: float = QUESTION_THRESHOLD
) -> Match:
    """Best and second-best person for ``name`` — loads people once.

    Never creates and never learns an alias: a question is not a contact.
    """
    target = _name_and_codes(name or "")
    if not (target[0] or target[1]):
        return Match(person=None, score=0.0, runner_up=None, runner_up_score=0.0)
    unknown_code: str | None = None
    if target[1]:
        # Codes first (WP-31): a held code is that person, never a guess.
        holders: list[tuple[str, Person]] = []
        for code in sorted(target[1]):
            held = await codes.holder(session, code)
            if held is not None and held.id not in {h.id for _, h in holders}:
                holders.append((code, held))
        if len(holders) == 1:
            code, held = holders[0]
            return Match(
                person=held,
                score=100.0,
                runner_up=None,
                runner_up_score=0.0,
                exact=True,
                via_code=code,
            )
        if len(holders) > 1:
            return Match(
                person=holders[0][1],
                score=100.0,
                runner_up=holders[1][1],
                runner_up_score=100.0,
            )
        unknown_code = sorted(target[1])[0]
        if not target[0]:
            return Match(
                person=None,
                score=0.0,
                runner_up=None,
                runner_up_score=0.0,
                unknown_code=unknown_code,
            )
        # A code nobody holds next to a name: look the name up alone.
        target = (target[0], frozenset())
    match = await _find_by_name(session, target, threshold=threshold)
    match.unknown_code = unknown_code
    return match


async def _find_by_name(
    session: AsyncSession, target: NameKey, *, threshold: float
) -> Match:
    people = list(await session.scalars(sa.select(Person).order_by(Person.id)))
    # Stable sort: on a tie the earlier row wins, as in best_match.
    ranked = sorted(
        ((_person_score(target, p), p) for p in people),
        key=lambda pair: pair[0],
        reverse=True,
    )
    exact = [p for p in people if any(_is_exact(target, c) for c in _candidates(p))]
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


class UnknownCode(Exception):
    """A code nobody holds, with no name beside it (WP-31)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IdentityConflict(Exception):
    """The code belongs to one person, the name to another (WP-31)."""

    def __init__(self, code: str, holder: Person, named: str, other: Person | None):
        super().__init__(f"{code}: {holder.display_name} vs {named}")
        self.code = code
        self.holder = holder
        self.named = named
        self.other = other


class OwnerNamed(Exception):
    """The name is the owner's own (raised by the owner guard, WP-36)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


CodePolicy = Literal["attach", "suggest", "ignore"]


def _backfill(
    person: Person,
    *,
    telegram_id: int | None,
    telegram_username: str | None,
    phone: str | None,
) -> None:
    if telegram_id is not None and person.telegram_id is None:
        person.telegram_id = telegram_id
    if telegram_username and not person.telegram_username:
        person.telegram_username = telegram_username
    if phone and not person.phone:
        person.phone = phone


def _learn_alias(person: Person, spelling: str) -> None:
    """Keep a new spelling (never one with a code in it, WP-29); a spelling
    in the other script is kept too (WP-28)."""
    if not spelling:
        return
    names = [person.display_name, *(person.aliases or [])]
    known = {normalise(codes.strip_codes(n)) for n in names}
    new_script = _is_cyrillic(spelling) not in {_is_cyrillic(n) for n in names if n}
    if normalise(spelling) not in known or new_script:
        # ORM change tracking does not see in-place list mutation.
        person.aliases = [*(person.aliases or []), spelling]


async def _give_codes(
    session: AsyncSession,
    person: Person,
    wanted: list[str],
    *,
    policy: CodePolicy,
    source: str | None,
    interaction_id: int | None,
) -> None:
    """Attach or suggest the codes nobody holds yet, per the policy."""
    if policy == "ignore":
        return
    for code in wanted:
        if await codes.holder(session, code) is not None:
            continue
        if policy == "attach":
            await codes.attach(
                session,
                person,
                code,
                source=source or "extraction",
                by=source or "extraction",
                interaction_id=interaction_id,
            )
        else:
            await codes.suggest(
                session,
                person,
                code,
                source=source or "extraction",
                interaction_id=interaction_id,
            )


async def resolve_person(
    session: AsyncSession,
    name: str,
    *,
    telegram_id: int | None = None,
    telegram_username: str | None = None,
    phone: str | None = None,
    create: bool = True,
    code_policy: CodePolicy = "suggest",
    source_interaction_id: int | None = None,
    source: str | None = None,
    strict: bool = False,
) -> Person | None:
    """Find or create the person `name` refers to.

    Codes first (WP-31): a code someone holds is that person, whatever the
    name; a code nobody holds with no name is UnknownCode; a code held by one
    person next to another person's name is IdentityConflict. Then the phone,
    then the name, fuzzily. A matched person gains a new spelling as an
    alias, never one with a code in it.

    Only callers that handle the identity exceptions pass ``strict=True``;
    everyone else gets None (or, for a conflict, a lookup by the name alone)
    and can never lose a message to identity.
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
            await codes.harvest(
                session,
                existing,
                name or "",
                policy=code_policy,
                source=source,
                interaction_id=source_interaction_id,
            )
            return existing

    name = (name or "").strip()
    if not name:
        return None
    q_codes, rest = codes.split_codes(name)

    # Phone first: a number the owner's contacts hold is that contact.
    if phone and len(re.sub(r"\D", "", phone)) >= 7:
        by_phone = await find_by_phone(session, phone)
        if by_phone is not None:
            if rest and best_match(rest, [by_phone])[1] >= MATCH_THRESHOLD:
                _learn_alias(by_phone, rest)
            await _give_codes(
                session,
                by_phone,
                q_codes,
                policy=code_policy,
                source=source,
                interaction_id=source_interaction_id,
            )
            return by_phone

    # The bot and the call-recording worker are separate processes; without a
    # lock, two concurrent ingestions naming the same new person both pass the
    # not-found check and insert duplicates (there is no usable unique
    # constraint — matching is fuzzy). A transaction-scoped advisory lock on
    # the normalised name serialises exactly the conflicting pair: the second
    # writer waits for the first commit and then finds the person it created.
    lock_key = normalise(rest) if rest else q_codes[0]
    await session.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                sa.func.hashtext("miya:person"), sa.func.hashtext(lock_key)
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

    if q_codes:
        holders = {c: await codes.holder(session, c) for c in q_codes}
        distinct = {h.id: h for h in holders.values() if h is not None}
        conflict: IdentityConflict | None = None
        if len(distinct) > 1:
            first, second = list(distinct.values())[:2]
            conflict = IdentityConflict(q_codes[0], first, rest or name, second)
        elif len(distinct) == 1:
            held = next(iter(distinct.values()))
            code = next(c for c, h in holders.items() if h is not None)
            if rest and best_match(rest, [held])[1] < QUESTION_THRESHOLD:
                other, score = best_match(rest, [p for p in people if p.id != held.id])
                if other is not None and score >= MATCH_THRESHOLD:
                    conflict = IdentityConflict(code, held, rest, other)
                elif telegram_id is not None:
                    # A Telegram account's own name: a code in it that belongs
                    # to someone named differently does not make it theirs.
                    conflict = IdentityConflict(code, held, rest, None)
            if (
                conflict is None
                and telegram_id is not None
                and held.telegram_id not in (None, telegram_id)
            ):
                conflict = IdentityConflict(code, held, rest or name, None)
            if conflict is None:
                await _give_codes(
                    session,
                    held,
                    [c for c in q_codes if holders[c] is None],
                    policy=code_policy,
                    source=source,
                    interaction_id=source_interaction_id,
                )
                _backfill(
                    held,
                    telegram_id=telegram_id,
                    telegram_username=telegram_username,
                    phone=phone,
                )
                return held
        elif not rest:
            if strict:
                raise UnknownCode(q_codes[0])
            return None
        if conflict is not None:
            if strict:
                raise conflict
            log.warning(
                "identity conflict on %s (holder %s, named %r); resolving by name only",
                conflict.code,
                conflict.holder.id,
                conflict.named,
            )
            q_codes, name = [], rest
            if not rest:
                return None
        else:
            # Anyone holding a *different* code is someone else.
            held_by = await codes.codes_of_many(session, [p.id for p in people])
            people = [
                p
                for p in people
                if not held_by.get(p.id) or set(held_by[p.id]) & set(q_codes)
            ]

    person, score = best_match(name, people)

    if person is not None and score >= MATCH_THRESHOLD:
        _learn_alias(person, rest)
        _backfill(
            person,
            telegram_id=telegram_id,
            telegram_username=telegram_username,
            phone=phone,
        )
        await _give_codes(
            session,
            person,
            q_codes,
            policy=code_policy,
            source=source,
            interaction_id=source_interaction_id,
        )
        return person

    if not create:
        return None

    try:
        await _owner_guard(session, rest or name)
    except OwnerNamed:
        if strict:
            raise
        return None

    log.info("creating person %r (best score %.0f)", rest or name, score)
    person = Person(
        display_name=rest or name,
        aliases=[],
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        phone=phone,
    )
    session.add(person)
    await session.flush()
    if telegram_id is not None and code_policy != "attach":
        # A stranger's own profile name (WP-35): the code is only suggested.
        await _give_codes(
            session,
            person,
            q_codes,
            policy=code_policy,
            source=source,
            interaction_id=source_interaction_id,
        )
        return person
    for code in q_codes:
        # A brand-new person cannot conflict: nobody held these codes.
        await codes.attach(
            session,
            person,
            code,
            source=source or "extraction",
            by=source or "extraction",
            interaction_id=source_interaction_id,
        )
    return person


async def _owner_guard(session: AsyncSession, name: str) -> None:
    """Never create a person who is the owner (WP-36 fills this in)."""
    return None


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
