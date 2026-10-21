"""The owner's question bank (docs/jarvis-plan.md §5.2 step 3, JV-23).

The statement allowlist reads a question by a closed grammar, but a grammar
cannot know every way a question carries an offer or a status. So an
autonomous reply (``auto:policy``, no owner tap) may ask only a question from
this bank: a closed list the owner approved, matched after normalisation
(``redlines.bank_key``), with no slot but the counterparty's name, words of
address, a courtesy segment («Rahmat aka, …») and the GS codes and waybills
the client wrote in the chat. Any
other question the model writes is a ``free_question``: a card for the
owner, never sent on its own. Drafts are not affected.

The bank starts from ``SEEDS``: the questions a cargo business asks a client
(where from, where to, what cargo, the weight, the cubes, the number of
places, the GS code, who receives it, the documents or an invoice, the
pickup address), in Uzbek Latin, Uzbek Cyrillic and Russian, with no time,
price or promise in them. Every seed, and every question the owner adds,
passes the red-line guard in the reply role and reads as one question
(``check_entry``); a test holds the seeds to it.

No table of its own: the owner's edits are the ``history`` of the
``jarvis_policy`` row ``question_bank`` (0028), one ``{at, field, old, new,
by}`` entry each, so the audit trail is the bank. An addition is proposed
first (``question_proposed``) and becomes part of the bank only on the
owner's «✅ Qo'shish» tap (``confirm``), re-checked under the row lock;
a removal is immediate (restricting needs no confirmation) and takes the
question's twins in the other scripts (``TWINS``). ``load`` replays the
seeds and the history.

Functions take the caller's session and flush; the caller commits.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.models import JarvisPolicy
from miya.jarvis import policy, redlines

log = logging.getLogger(__name__)

SCOPE: Final = policy.QUESTION_BANK
# history ``field`` of a question in the bank: ``new`` the text added,
# ``old`` the text removed.
FIELD: Final = "question"
# history ``field`` of an addition waiting for the owner's tap: ``new`` the
# text proposed, ``old`` the text of a proposal the owner declined; ``code``
# its ref.
PROPOSED_FIELD: Final = "question_proposed"

# The button letters: ``qb:y|n:<ref>`` under a proposal, ``qb:d:<ref>``
# under the list (keyboards.py builds the payloads from the same letters).
ADD_ACTION: Final = "y"
DISMISS_ACTION: Final = "n"
REMOVE_ACTION: Final = "d"

# The longest question the bank takes, and the most questions it holds.
MAX_CHARS: Final = 120
MAX_ENTRIES: Final = 100
REF_CHARS: Final = 10

# Why a question is not taken (the bot says it in Uzbek).
EMPTY: Final = "empty"
TOO_LONG: Final = "too_long"
NOT_ONE_QUESTION: Final = "not_one_question"
RED_LINE: Final = "red_line"
NOT_A_QUESTION: Final = "not_a_question"
ALREADY: Final = "already"
FULL: Final = "full"
REFUSALS: Final = (EMPTY, TOO_LONG, NOT_ONE_QUESTION, RED_LINE, NOT_A_QUESTION)

SEEDS: Final[tuple[str, ...]] = (
    # Uzbek, Latin.
    "Yuk qaysi shahardan?",
    "Yuk qaysi shaharga?",
    "Qanday yuk?",
    "Yukingiz necha kilo?",
    "Yukingiz necha kub?",
    "Yuk necha joy?",
    "Mijoz kodingiz qanday?",
    "GS kodingiz qanday?",
    "Yukni kim qabul qiladi?",
    "Yuk hujjatlari bormi?",
    "Invoys bormi?",
    "Yuk qaysi manzilda?",
    # Uzbek, Cyrillic.
    "Юк қайси шаҳардан?",
    "Юк қайси шаҳарга?",
    "Қандай юк?",
    "Юкингиз неча кило?",
    "Юкингиз неча куб?",
    "Юк неча жой?",
    "Мижоз кодингиз қандай?",
    "Юкни ким қабул қилади?",
    "Юк ҳужжатлари борми?",
    "Инвойс борми?",
    "Юк қайси манзилда?",
    # Russian.
    "Откуда груз?",
    "Куда груз?",
    "Какой груз?",
    "Сколько весит груз?",
    "Какой объём груза?",
    "Сколько мест?",
    "Какой у вас код клиента?",
    "Кто контактное лицо?",
    "Есть ли документы?",
    "Есть ли инвойс?",
    "Какой адрес у груза?",
)

# The same question in the other scripts: a removal takes the whole group,
# since a Russian text never matches its Uzbek twin's key (``remove``).
TWINS: Final[tuple[tuple[str, ...], ...]] = (
    ("Yuk qaysi shahardan?", "Юк қайси шаҳардан?", "Откуда груз?"),
    ("Yuk qaysi shaharga?", "Юк қайси шаҳарга?", "Куда груз?"),
    ("Qanday yuk?", "Қандай юк?", "Какой груз?"),
    ("Yukingiz necha kilo?", "Юкингиз неча кило?", "Сколько весит груз?"),
    ("Yukingiz necha kub?", "Юкингиз неча куб?", "Какой объём груза?"),
    ("Yuk necha joy?", "Юк неча жой?", "Сколько мест?"),
    ("Mijoz kodingiz qanday?", "Мижоз кодингиз қандай?", "Какой у вас код клиента?"),
    ("Yukni kim qabul qiladi?", "Юкни ким қабул қилади?", "Кто контактное лицо?"),
    ("Yuk hujjatlari bormi?", "Юк ҳужжатлари борми?", "Есть ли документы?"),
    ("Invoys bormi?", "Инвойс борми?", "Есть ли инвойс?"),
    ("Yuk qaysi manzilda?", "Юк қайси манзилда?", "Какой адрес у груза?"),
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One question of the bank. ``ref`` names this exact text (the 🗑
    button's payload); ``key`` is what a reply's question is matched by."""

    ref: str
    text: str
    key: str
    seed: bool = False


def _clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def ref_of(text: str) -> str:
    """A short stable name of one exact question text (case aside)."""
    digest = hashlib.sha256(_clean(text).casefold().encode("utf-8")).hexdigest()
    return digest[:REF_CHARS]


def _entry(text: str, *, seed: bool = False) -> Entry:
    clean = _clean(text)
    return Entry(ref=ref_of(clean), text=clean, key=redlines.bank_key(clean), seed=seed)


def check_entry(text: str) -> str | None:
    """Why ``text`` may not be in the bank, or None: it must be one question
    the grammar reads as one, at most MAX_CHARS long, that passes the
    red-line guard in the reply role (no digit, time, price, promise, link
    or name), with something left to match once words of address are
    dropped."""
    clean = _clean(text)
    if not clean:
        return EMPTY
    if len(clean) > MAX_CHARS:
        return TOO_LONG
    if len(redlines.sentences(clean)) != 1:
        return NOT_ONE_QUESTION
    verdict = redlines.check_outbound(
        clean, "reply", client_script=redlines.script_of(clean)
    )
    if not verdict.passed:
        return RED_LINE
    if redlines.check_shapes(clean).shapes != ("question",):
        return NOT_A_QUESTION
    if not redlines.bank_key(clean):
        return NOT_A_QUESTION
    return None


def _twins(bank: Iterable[Entry], gone: Entry) -> list[Entry]:
    """The entries a removal of ``gone`` takes: the text itself, every text
    that matches the same way (its Latin or Cyrillic twin) and the seed's
    twins in the other scripts (``TWINS``, the Russian one among them)."""
    refs = {gone.ref}
    keys = {gone.key}
    for group in TWINS:
        members = [_entry(text) for text in group]
        if any(m.ref == gone.ref or m.key == gone.key for m in members):
            refs.update(m.ref for m in members)
            keys.update(m.key for m in members)
    return [e for e in bank if e.ref in refs or e.key in keys]


def replay(history: Iterable[Any]) -> tuple[Entry, ...]:
    """The bank: the seeds, then each addition and removal in order. A
    removal takes every text of the same question (its Latin, Cyrillic or
    Russian twin, ``_twins``), so a removed question is gone."""
    seeds = {ref_of(text) for text in SEEDS}
    active: dict[str, Entry] = {}
    for text in SEEDS:
        entry = _entry(text, seed=True)
        active[entry.ref] = entry
    for item in history or []:
        if not isinstance(item, dict) or item.get("field") != FIELD:
            continue
        new, old = item.get("new"), item.get("old")
        if isinstance(new, str) and _clean(new):
            added = _entry(new, seed=ref_of(new) in seeds)
            active[added.ref] = added
        elif isinstance(old, str) and _clean(old):
            for twin in _twins(list(active.values()), _entry(old)):
                active.pop(twin.ref)
    return tuple(active.values())


async def _history(session: AsyncSession) -> list[Any]:
    row = await session.get(JarvisPolicy, SCOPE, populate_existing=True)
    return list(row.history or []) if row is not None else []


async def load(session: AsyncSession) -> tuple[Entry, ...]:
    """The bank in force now."""
    return replay(await _history(session))


async def texts(session: AsyncSession) -> tuple[str, ...]:
    """The questions in force now, as the guard reads them."""
    return tuple(entry.text for entry in await load(session))


@dataclass(frozen=True, slots=True)
class Change:
    """What a proposal, a confirmation or a removal came to. ``code`` is
    ``proposed``, ``added``, ``declined``, ``removed``, ``stale`` or a
    refusal (``REFUSALS``, ``already``, ``full``)."""

    code: str
    entry: Entry | None = None


def _proposal(history: Sequence[Any], ref: str) -> str | None:
    """The text of the newest proposal ``ref`` names that is still waiting:
    not declined and not added since."""
    text: str | None = None
    for item in history:
        if not isinstance(item, dict) or item.get("code") != ref:
            continue
        field = item.get("field")
        new = item.get("new")
        if field == PROPOSED_FIELD and isinstance(new, str):
            text = new
        elif field in (PROPOSED_FIELD, FIELD):
            # Declined, added or removed since: the proposal is answered.
            text = None
    return text


async def propose(session: AsyncSession, text: str, *, by: str, now: datetime) -> Change:
    """``/savolbank qo'sh Savol?``: a guarded question waits for the owner's
    tap. Nothing is in the bank before ``confirm``."""
    refused = check_entry(text)
    if refused is not None:
        return Change(refused)
    entry = _entry(text)
    bank = await load(session)
    if any(e.key == entry.key for e in bank):
        return Change(ALREADY, entry)
    if len(bank) >= MAX_ENTRIES:
        return Change(FULL, entry)
    await policy.note(
        session,
        scope=SCOPE,
        field=PROPOSED_FIELD,
        old=None,
        new=entry.text,
        by=by,
        now=now,
        code=entry.ref,
    )
    return Change("proposed", entry)


async def confirm(session: AsyncSession, ref: str, *, by: str, now: datetime) -> Change:
    """The owner's «✅ Qo'shish»: under the row lock, the proposal ``ref``
    names is checked again and added. A second tap, or a proposal answered
    since, changes nothing."""
    row = await policy.lock_scope(session, SCOPE)
    history = list(row.history or [])
    text = _proposal(history, ref)
    if text is None:
        return Change("stale")
    refused = check_entry(text)
    if refused is not None:
        return Change(refused)
    entry = _entry(text)
    bank = replay(history)
    if any(e.key == entry.key for e in bank):
        return Change(ALREADY, entry)
    if len(bank) >= MAX_ENTRIES:
        return Change(FULL, entry)
    await policy.note(
        session,
        scope=SCOPE,
        field=FIELD,
        old=None,
        new=entry.text,
        by=by,
        now=now,
        code=entry.ref,
    )
    log.info("question bank entry added", extra={"ref": entry.ref})
    return Change("added", entry)


async def decline(session: AsyncSession, ref: str, *, by: str, now: datetime) -> Change:
    """The owner's «✖️ Kerak emas» on a proposal."""
    row = await policy.lock_scope(session, SCOPE)
    text = _proposal(list(row.history or []), ref)
    if text is None:
        return Change("stale")
    await policy.note(
        session,
        scope=SCOPE,
        field=PROPOSED_FIELD,
        old=text,
        new=None,
        by=by,
        now=now,
        code=ref,
    )
    return Change("declined", _entry(text))


async def remove(session: AsyncSession, ref: str, *, by: str, now: datetime) -> Change:
    """🗑 or ``/savolbank o'chir 3``: the question leaves the bank at once,
    with its twins in the other scripts (``_twins``). An autonomous row that asked
    it and is not sent yet goes back to the owner's card at the claim."""
    row = await policy.lock_scope(session, SCOPE)
    bank = replay(list(row.history or []))
    entry = next((e for e in bank if e.ref == ref), None)
    if entry is None:
        return Change("stale")
    for twin in _twins(bank, entry):
        await policy.note(
            session,
            scope=SCOPE,
            field=FIELD,
            old=twin.text,
            new=None,
            by=by,
            now=now,
            code=twin.ref,
        )
    log.info("question bank entry removed", extra={"ref": entry.ref})
    return Change("removed", entry)


__all__ = [
    "ADD_ACTION",
    "ALREADY",
    "DISMISS_ACTION",
    "EMPTY",
    "FIELD",
    "FULL",
    "MAX_CHARS",
    "MAX_ENTRIES",
    "NOT_A_QUESTION",
    "NOT_ONE_QUESTION",
    "PROPOSED_FIELD",
    "RED_LINE",
    "REFUSALS",
    "REMOVE_ACTION",
    "SCOPE",
    "SEEDS",
    "TOO_LONG",
    "TWINS",
    "Change",
    "Entry",
    "check_entry",
    "confirm",
    "decline",
    "load",
    "propose",
    "ref_of",
    "remove",
    "replay",
    "texts",
]
