"""Client codes (GS367) and waybill numbers (YW26-004715), WP-29.

The owner's clients carry a code in their names ("GS367 Akmal"), and fuzzy
name matching used to treat GS368 as a spelling of GS367 — a debt could land
on the wrong client. Codes are therefore read by one regex and compared
exactly, never fuzzily. Pure functions only.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from miya.config import settings
from miya.db.models import ClientCode, Person

log = logging.getLogger(__name__)

# Each Latin prefix letter also accepts its Cyrillic look-alike: a phone
# keyboard in the other layout still writes the same code.
LOOKALIKE = {
    "G": "GГ",
    "S": "SС",
    "Y": "YУ",
    "W": "W",
    "K": "KК",
    "A": "AА",
    "B": "BВ",
    "C": "CС",
    "E": "EЕ",
    "H": "HН",
    "M": "MМ",
    "O": "OО",
    "P": "PР",
    "T": "TТ",
    "X": "XХ",
}
# Grammatical endings a code may carry: "GS367ga", "GS367ning".
UZ_SUFFIXES = frozenset(
    {
        "ga",
        "ka",
        "qa",
        "ni",
        "ning",
        "niki",
        "da",
        "dan",
        "dagi",
        "lar",
        "larga",
        "larni",
        "larning",
        "га",
        "ка",
        "қа",
        "ни",
        "нинг",
        "ники",
        "да",
        "дан",
        "даги",
        "лар",
        "ларга",
        "ларни",
        "ларнинг",
    }
)
_LETTERS = "0-9A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ'"
_TAIL = "A-Za-zА-Яа-яЎўҚқҒғҲҳ'"


def _prefixes(raw: str) -> tuple[str, ...]:
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def _prefix_class(prefix: str) -> str:
    return "".join(f"[{re.escape(LOOKALIKE.get(ch, ch))}]" for ch in prefix)


@functools.lru_cache(maxsize=8)
def _client_code_re(prefixes: tuple[str, ...], digits: int) -> re.Pattern[str]:
    alternatives = "|".join(_prefix_class(p) for p in prefixes)
    return re.compile(
        rf"(?<![{_LETTERS}])(?P<p>{alternatives})[ \t]{{0,2}}[-–—.#№:_]?[ \t]{{0,2}}"
        rf"(?P<d>\d{{1,{digits}}})(?!\d)(?P<tail>[{_TAIL}]*)",
        re.IGNORECASE,
    )


def client_code_re() -> re.Pattern[str]:
    """CLIENT_CODE_RE for the configured prefixes and digit count."""
    return _client_code_re(
        _prefixes(settings.client_code_prefixes), settings.client_code_max_digits
    )


def _canonical_prefix(matched: str) -> str:
    folded = matched.upper()
    for prefix in _prefixes(settings.client_code_prefixes):
        if len(prefix) == len(folded) and all(
            ch in LOOKALIKE.get(p, p) + LOOKALIKE.get(p, p).lower()
            for ch, p in zip(folded, prefix, strict=True)
        ):
            return prefix
    return folded


def _canonical(match: re.Match[str]) -> str:
    digits = match.group("d")
    if settings.client_code_strip_leading_zeros:
        digits = digits.lstrip("0") or "0"
    return _canonical_prefix(match.group("p")) + digits


def _counts(match: re.Match[str]) -> bool:
    tail = match.group("tail")
    return tail == "" or tail.lower() in UZ_SUFFIXES


def canonical_client_code(text: str) -> str | None:
    """'gs-0367' → 'GS367'; None unless the whole text is exactly one code."""
    stripped = (text or "").strip()
    match = client_code_re().fullmatch(stripped)
    if match is None or match.group("tail"):
        return None
    return _canonical(match)


def find_client_codes(text: str) -> list[str]:
    """Every code in free text, canonical, de-duplicated, in order."""
    found: list[str] = []
    for match in client_code_re().finditer(text or ""):
        if _counts(match):
            code = _canonical(match)
            if code not in found:
                found.append(code)
    return found


def canonicalise_codes(text: str) -> str:
    """Every code in ``text`` written canonically: '/tarix GS 367 20' has its
    code as one token ('GS367 20'), so a count after it is not misread."""

    def one(match: re.Match[str]) -> str:
        return _canonical(match) + match.group("tail") if _counts(match) else match[0]

    return client_code_re().sub(one, text or "")


_EDGE = re.compile(r"^[\s()\[\]—–\-,:]+|[\s()\[\]—–\-,:]+$")


def split_codes(name: str) -> tuple[list[str], str]:
    """(codes, the name without them): 'Akmal (GS367)' → (['GS367'], 'Akmal')."""
    codes: list[str] = []
    kept: list[str] = []
    last = 0
    for match in client_code_re().finditer(name or ""):
        if not _counts(match):
            continue
        code = _canonical(match)
        if code not in codes:
            codes.append(code)
        kept.append(name[last : match.start()])
        last = match.end()
    kept.append((name or "")[last:])
    rest = " ".join(" ".join(kept).split())
    rest = re.sub(r"\(\s*\)", " ", rest)
    rest = " ".join(_EDGE.sub("", rest).split())
    return codes, rest


def strip_codes(text: str) -> str:
    return split_codes(text)[1]


@functools.lru_cache(maxsize=8)
def _waybill_re(prefixes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(p) for p in prefixes)
    return re.compile(
        rf"(?<![0-9A-Za-z])(?P<p>{alternatives})[ \t]?(?P<y>\d{{2}})[ \t]?[-–—]?[ \t]?"
        r"(?P<n>\d{4,8})(?!\d)",
        re.IGNORECASE,
    )


def waybill_re() -> re.Pattern[str]:
    return _waybill_re(_prefixes(settings.waybill_prefixes))


def canonical_waybill(match: re.Match[str]) -> str:
    return f"{match.group('p').upper()}{match.group('y')}-{match.group('n')}"


def find_waybills(text: str) -> list[str]:
    found: list[str] = []
    for match in waybill_re().finditer(text or ""):
        waybill = canonical_waybill(match)
        if waybill not in found:
            found.append(waybill)
    return found


@functools.lru_cache(maxsize=8)
def _join_re(prefixes: tuple[str, ...], digits: int) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(p.lower()) for p in prefixes)
    return re.compile(rf"\b({alternatives})[\s\-_#№]*(\d{{1,{digits}}})\b")


def join_pattern() -> re.Pattern[str]:
    """For text.normalise_for_search: 'gs 367' → 'gs367' (lower-case, Latin)."""
    return _join_re(
        _prefixes(settings.client_code_prefixes), settings.client_code_max_digits
    )


# --- who holds which code (WP-30) ---------------------------------------------
#
# Every function takes a session and never commits; the caller's transaction
# decides. At most one active row per code is the database's own guarantee
# (ux_client_codes_active_code).

ACTIVE = "active"
SUGGESTED = "suggested"
REJECTED = "rejected"
DETACHED = "detached"
ACTIVE_CODE_INDEX = "ux_client_codes_active_code"


class CodeTaken(Exception):
    """The code is active on another person."""

    def __init__(self, holder) -> None:
        super().__init__(getattr(holder, "display_name", holder))
        self.holder = holder


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(settings.tz)


def _note(row, field: str, old, new, by: str, now: datetime) -> None:
    row.history = [
        *(row.history or []),
        {"at": now.isoformat(), "field": field, "old": old, "new": new, "by": by},
    ]


async def _active_row(session: AsyncSession, code: str):
    return await session.scalar(
        sa.select(ClientCode)
        .where(ClientCode.code == code, ClientCode.status == ACTIVE)
        .with_for_update()
    )


async def holder(session: AsyncSession, code: str) -> Person | None:
    return await session.scalar(
        sa.select(Person)
        .join(ClientCode, ClientCode.person_id == Person.id)
        .where(ClientCode.code == code, ClientCode.status == ACTIVE)
    )


async def codes_of(session: AsyncSession, person_id: int) -> list[str]:
    return list(
        await session.scalars(
            sa.select(ClientCode.code)
            .where(ClientCode.person_id == person_id, ClientCode.status == ACTIVE)
            .order_by(ClientCode.code)
        )
    )


async def codes_of_many(session: AsyncSession, ids) -> dict[int, list[str]]:
    ids = list(ids)
    result: dict[int, list[str]] = {i: [] for i in ids}
    if not ids:
        return result
    rows = await session.execute(
        sa.select(ClientCode.person_id, ClientCode.code)
        .where(ClientCode.person_id.in_(ids), ClientCode.status == ACTIVE)
        .order_by(ClientCode.person_id, ClientCode.code)
    )
    for person_id, code in rows.all():
        result[person_id].append(code)
    return result


async def _pair(session: AsyncSession, code: str, person_id: int):
    return await session.scalar(
        sa.select(ClientCode)
        .where(ClientCode.code == code, ClientCode.person_id == person_id)
        .with_for_update()
    )


def _constraint(exc: IntegrityError) -> str | None:
    return getattr(
        getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None
    )


async def attach(
    session: AsyncSession,
    person: Person,
    code: str,
    *,
    source: str,
    by: str,
    interaction_id: int | None = None,
    now: datetime | None = None,
) -> ClientCode:
    """Give ``person`` the active ``code``; CodeTaken if someone else holds it."""
    now = _now(now)
    current = await _active_row(session, code)
    if current is not None:
        if current.person_id == person.id:
            return current
        raise CodeTaken(await session.get(Person, current.person_id))
    row = await _pair(session, code, person.id)
    try:
        async with session.begin_nested():
            if row is not None:
                _note(row, "status", row.status, ACTIVE, by, now)
                row.status = ACTIVE
                row.answered_at = row.answered_at or now
            else:
                row = ClientCode(
                    code=code,
                    person_id=person.id,
                    status=ACTIVE,
                    source=source,
                    source_interaction_id=interaction_id,
                )
                session.add(row)
            await session.flush()
    except IntegrityError as exc:
        if _constraint(exc) != ACTIVE_CODE_INDEX:
            raise
        raise CodeTaken(await holder(session, code)) from None
    return row


async def move(
    session: AsyncSession,
    code: str,
    person: Person,
    *,
    by: str,
    now: datetime | None = None,
) -> ClientCode:
    """Hand ``code`` to ``person``; the old holder's row is detached first."""
    now = _now(now)
    current = await _active_row(session, code)
    old_person = current.person_id if current is not None else None
    if current is not None:
        if current.person_id == person.id:
            return current
        _note(current, "person", old_person, person.id, by, now)
        current.status = DETACHED
        # The partial unique index is checked per statement: the old row
        # must leave "active" before the new one enters it.
        await session.flush()
    row = await _pair(session, code, person.id)
    if row is None:
        row = ClientCode(code=code, person_id=person.id, status=ACTIVE, source="command")
        session.add(row)
    else:
        row.status = ACTIVE
    _note(row, "person", old_person, person.id, by, now)
    await session.flush()
    return row


async def detach(
    session: AsyncSession, code: str, *, by: str, now: datetime | None = None
) -> Person | None:
    """Take ``code`` off its holder; returns who held it."""
    now = _now(now)
    current = await _active_row(session, code)
    if current is None:
        return None
    _note(current, "status", ACTIVE, DETACHED, by, now)
    current.status = DETACHED
    await session.flush()
    return await session.get(Person, current.person_id)


async def suggest(
    session: AsyncSession,
    person: Person,
    code: str,
    *,
    source: str,
    interaction_id: int | None = None,
) -> ClientCode | None:
    """A code this person may hold, for the owner to confirm. Refused when
    the pairing was ever recorded or someone already holds the code."""
    if await _pair(session, code, person.id) is not None:
        return None
    if await holder(session, code) is not None:
        return None
    row = ClientCode(
        code=code,
        person_id=person.id,
        status=SUGGESTED,
        source=source,
        source_interaction_id=interaction_id,
    )
    session.add(row)
    await session.flush()
    return row


async def _locked_suggestion(session: AsyncSession, row_id: int) -> ClientCode | None:
    return await session.scalar(
        sa.select(ClientCode)
        .where(ClientCode.id == row_id, ClientCode.status == SUGGESTED)
        .with_for_update()
    )


async def accept_suggestion(
    session: AsyncSession, row_id: int, *, by: str, now: datetime | None = None
) -> ClientCode | None:
    now = _now(now)
    row = await _locked_suggestion(session, row_id)
    if row is None:
        return None
    current = await _active_row(session, row.code)
    if current is not None and current.person_id != row.person_id:
        raise CodeTaken(await session.get(Person, current.person_id))
    _note(row, "status", SUGGESTED, ACTIVE, by, now)
    row.status = ACTIVE
    row.answered_at = now
    await session.flush()
    return row


async def reject_suggestion(
    session: AsyncSession, row_id: int, *, by: str, now: datetime | None = None
) -> ClientCode | None:
    now = _now(now)
    row = await _locked_suggestion(session, row_id)
    if row is None:
        return None
    _note(row, "status", SUGGESTED, REJECTED, by, now)
    row.status = REJECTED
    row.answered_at = now
    await session.flush()
    return row


async def pending_suggestions(session: AsyncSession, limit: int = 10) -> list[ClientCode]:
    return list(
        await session.scalars(
            sa.select(ClientCode)
            .where(ClientCode.status == SUGGESTED)
            .options(selectinload(ClientCode.person))
            .order_by(ClientCode.created_at.desc(), ClientCode.id.desc())
            .limit(limit)
        )
    )


async def pending_suggestion_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            sa.select(sa.func.count(ClientCode.id)).where(ClientCode.status == SUGGESTED)
        )
        or 0
    )


# (person id, code) pairs already looked at in this process: a busy chat
# must not re-check the same contact name on every message.
_harvested: set[tuple[int, str]] = set()


async def harvest(
    session: AsyncSession,
    person: Person,
    name: str,
    *,
    policy: str,
    source: str | None = None,
    interaction_id: int | None = None,
) -> None:
    """Learn the codes a known person's name carries (WP-35).

    The owner's own words ('attach': a saved contact, the phone book, the
    owner's note) give the code outright; anyone else's only suggest it. A
    code active on someone else is left alone — the name does not decide.
    """
    if policy == "ignore" or person.id is None:
        return
    for code in find_client_codes(name):
        key = (person.id, code)
        if key in _harvested:
            continue
        current = await holder(session, code)
        if current is not None and current.id != person.id:
            log.warning(
                "name of person %s carries %s, held by person %s",
                person.id,
                code,
                current.id,
            )
            _harvested.add(key)
        elif current is None:
            if policy == "attach":
                await attach(
                    session,
                    person,
                    code,
                    source=source or "extraction",
                    by=source or "extraction",
                    interaction_id=interaction_id,
                )
            else:
                await suggest(
                    session,
                    person,
                    code,
                    source=source or "extraction",
                    interaction_id=interaction_id,
                )
        else:
            _harvested.add(key)


# --- code_mentions: exact recall of codes and waybills (WP-39) -------------------

MAX_MENTIONS_PER_INTERACTION = 200
# Rows whose text repeats other rows (a window's members) or is not the
# owner's traffic at all: stamped, never indexed.
_NOT_INDEXED_KINDS = {"window", "question", "client_import"}


async def index_interaction(
    session: AsyncSession, interaction, *, now: datetime | None = None
) -> int:
    """(Re)write the code_mentions of one interaction; returns how many."""
    from miya.db.models import CodeMention
    from miya.services.ingest import text_for_extraction  # ingest imports us

    now = _now(now)
    await session.execute(
        sa.delete(CodeMention).where(CodeMention.interaction_id == interaction.id)
    )
    if (interaction.meta or {}).get("kind") in _NOT_INDEXED_KINDS:
        interaction.codes_indexed_at = now
        await session.flush()
        return 0
    text = text_for_extraction(interaction)
    found = [("client", c) for c in find_client_codes(text)]
    found += [("waybill", w) for w in find_waybills(text)]
    found = found[:MAX_MENTIONS_PER_INTERACTION]
    for kind, code in found:
        session.add(
            CodeMention(
                interaction_id=interaction.id,
                kind=kind,
                code=code,
                occurred_at=interaction.occurred_at,
            )
        )
    interaction.codes_indexed_at = now
    await session.flush()
    return len(found)


async def index_pending(session: AsyncSession, *, limit: int = 1000) -> int:
    """Index up to ``limit`` interactions not indexed yet, oldest id first."""
    from miya.db.models import Interaction

    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(Interaction.codes_indexed_at.is_(None))
            .order_by(Interaction.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for interaction in rows:
        await index_interaction(session, interaction)
    return len(rows)


# --- exact lookups (WP-40) ----------------------------------------------------------


@dataclass(slots=True)
class MentionLine:
    when: datetime
    source: str
    chat_title: str | None
    speaker: str | None
    text: str


MENTION_EXCERPT = 300


def canonical_waybill_text(text: str) -> str | None:
    """'yw26 004715' → 'YW26-004715'; None unless the whole text is one."""
    match = waybill_re().fullmatch((text or "").strip())
    return canonical_waybill(match) if match else None


def canonical_lookup(text: str) -> tuple[str, str] | None:
    """(kind, code) when ``text`` is exactly one waybill or client code."""
    waybill = canonical_waybill_text(text)
    if waybill is not None:
        return "waybill", waybill
    code = canonical_client_code(text)
    if code is not None:
        return "client", code
    return None


async def mentions(
    session: AsyncSession,
    code: str,
    *,
    limit: int = 20,
    exclude_person_id: int | None = None,
) -> list[MentionLine]:
    """Where a code or waybill was mentioned, newest first, dated."""
    from miya.db.models import ChatMonitor, CodeMention, Interaction
    from miya.services.ingest import text_for_extraction

    query = (
        sa.select(Interaction, ChatMonitor.title, Person.display_name)
        .join(CodeMention, CodeMention.interaction_id == Interaction.id)
        .outerjoin(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .outerjoin(Person, Person.id == Interaction.person_id)
        .where(CodeMention.code == code)
        .order_by(CodeMention.occurred_at.desc(), Interaction.id.desc())
        .limit(limit)
    )
    if exclude_person_id is not None:
        query = query.where(
            sa.or_(
                Interaction.person_id.is_(None),
                Interaction.person_id != exclude_person_id,
            )
        )
    lines: list[MentionLine] = []
    for interaction, title, speaker in (await session.execute(query)).all():
        lines.append(
            MentionLine(
                when=interaction.occurred_at,
                source=interaction.source.value,
                chat_title=title,
                speaker=speaker,
                text=text_for_extraction(interaction)[:MENTION_EXCERPT],
            )
        )
    return lines
