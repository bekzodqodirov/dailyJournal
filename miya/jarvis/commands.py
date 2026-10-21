"""The owner's commands in the admin bot (docs/jarvis-plan.md §3.4, JV-20).

«Sardorga Telegramdan yozib yubor, kechki 6 da gaplashamiz», typed or
spoken, reaches ``rag.answer_full(allow_actions=True)`` as mode ``command``
(``rag.classify``), and the model gets the read-only tools plus the act tools
below. **An act tool never acts; it proposes.** What it proposes is a card
the owner taps (``ja:``, never in the bot's ``_button_handlers``), and
nothing leaves or changes before that tap:

* ``propose_send_message`` stores an outbox draft (``origin='owner_command'``,
  ``authored_by='owner'``, ``command_interaction_id`` the command message;
  ``outbox.create`` requires it) whose card shows the exact text, who it goes
  to (their name, code and chat) and the channel. The owner's text passes the
  guard in the ``owner`` role: flagged on the card, never blocked.
* ``set_personal``, ``set_group_reply`` and ``set_group_monitor`` (the
  ``/shaxsiy``, ``/guruhai`` and ``/chats`` switches) and ``sheets_resync``
  store a proposal on the command message; a restricting change applies on
  one tap of its card, a loosening one (unmark personal, group replies on,
  reading a group) shows the confirm card of JV-12 first.
* ``list_pending`` reads what waits; it changes nothing.

What the model may not choose (rules 16 and 17):

* **The recipient** comes only from the owner's own words: the ``person``
  the model names must be words of the command text (a case ending such as
  «Sardorga» stripped), so a name that only a tool result carried is
  refused. It is resolved with ``people.find_person`` (never
  ``resolve_person``, which creates people), retried once without a case
  ending, and accepted only when not ambiguous and exact, by a client code or
  at ``people.MATCH_THRESHOLD``. Two people alike are asked back with one
  button each (``Qaysi Sardor?``); the owner under any of their names is
  refused.
* **The chat** must already exist on that channel (the owner's account has
  it, or MIYA stored a message of it): a command never starts a
  conversation.
* **The time.** ``send_at`` is cross-checked with ``when.parse`` of the
  owner's words: a time the owner did not say, could mean two ways, or that
  disagrees with theirs is refused, and the model asks. A time without a day
  is today while it is still ahead, else tomorrow. A scheduled row carries
  ``not_before``; the gate holds it until then.
* **The channel** is the one the owner named, else Telegram.
* **A figure.** Every money figure in the text must be one the owner said
  (rule 1): the model rephrases, it never adds an amount.

At most ``MAX_PROPOSALS`` proposals come from one turn; the same proposal
twice is one. Every tool is contained (rule 25): it runs in a savepoint,
and an error is logged with the command's ids and comes back to the model
as an error, never raised into the answer.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType, InteractionSource
from miya.db.models import ChatMonitor, Interaction, OutboxMessage, Person
from miya.jarvis import chatkey, gate, outbox, policy, style
from miya.services import amounts, chats, codes, people, sheets_sync, when
from miya.services.text import ALIAS_SUFFIXES, normalise_for_search

log = logging.getLogger(__name__)

# One turn proposes at most this many things (sends, settings, «which one?»).
MAX_PROPOSALS: Final = 3
# The longest text a send proposal takes: the card must show it whole.
TEXT_MAX: Final = 2000
# history ``by`` of a row a command created, and of the tap that took it.
BY_COMMAND: Final = outbox.BY_COMMAND
BY_BUTTON: Final = "button"
# Where a command message keeps what it proposed besides sends (an «which
# one?» question, a setting), so a tap finds exactly that proposal.
PROPOSALS_KEY: Final = "jarvis_proposals"
# How much of the owner's style profile the model reads for a message's text.
STYLE_HINT_CHARS: Final = 1500
# Rows ``list_pending`` shows.
PENDING_LIMIT: Final = 20

ActionKind = Literal["send", "ask", "no_chat", "setting", "listed"]
SEND: Final[ActionKind] = "send"
ASK: Final[ActionKind] = "ask"
NO_CHAT: Final[ActionKind] = "no_chat"
SETTING: Final[ActionKind] = "setting"
LISTED: Final[ActionKind] = "listed"

# The settings a command may propose. A loosening one shows a confirm card.
PERSONAL_ON: Final = "personal_on"
PERSONAL_OFF: Final = "personal_off"
GROUP_REPLY_ON: Final = "group_reply_on"
GROUP_REPLY_OFF: Final = "group_reply_off"
MONITOR_ON: Final = "monitor_on"
MONITOR_OFF: Final = "monitor_off"
SHEETS_RESYNC: Final = "sheets_resync"
SETTINGS: Final = (
    PERSONAL_ON,
    PERSONAL_OFF,
    GROUP_REPLY_ON,
    GROUP_REPLY_OFF,
    MONITOR_ON,
    MONITOR_OFF,
    SHEETS_RESYNC,
)
LOOSENING: Final = frozenset({PERSONAL_OFF, GROUP_REPLY_ON, MONITOR_ON})
# Loosening settings whose confirm card is JV-12's own (``jr:y``, ``jr:g``):
# that tap applies them, so nothing is stored on the command message.
JV12_CONFIRMED: Final = frozenset({PERSONAL_OFF, GROUP_REPLY_ON})

# The codes a tool answers the model with (and a tap comes to).
NOT_OWNERS_WORDS: Final = "not_in_owner_words"
OWNER_REFUSED: Final = "recipient_is_owner"
NOT_FOUND: Final = "not_found"
AMBIGUOUS: Final = "ambiguous"
NO_CHAT_CODE: Final = "no_chat"
TIME_NOT_SAID: Final = "send_at_not_said"
TIME_DISAGREES: Final = "send_at_disagrees"
TIME_PAST: Final = "send_at_past"
TIME_BAD: Final = "send_at_unreadable"
CHANNEL_DISAGREES: Final = "channel_disagrees"
TOO_MANY: Final = "too_many_proposals"
TOO_LONG: Final = "text_too_long"
FIGURE_NOT_SAID: Final = "figure_not_in_owner_words"
ALREADY_PROPOSED: Final = "already_proposed"
NO_COMMAND: Final = "no_command_message"
DISABLED: Final = "jarvis_disabled"
FAILED: Final = "failed"

TapCode = Literal["sent_card", "no_chat", "applied", "cancelled", "answered", "stale"]


# --- the act tools (offered only in command mode, never on POST /v1/ask) -----------

ACT_TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "propose_send_message",
        "description": (
            "PROPOSE a message to one person the owner named in their current "
            "words. Nothing is sent: the owner gets a card with the exact text, "
            "the recipient and the channel, and only their tap sends it. The "
            "recipient must be named in the owner's own words, never taken from "
            "a tool result. Two people with that name: the owner is asked which."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": (
                        "The recipient as the owner named them (name or GS code)."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "The message, in the owner's voice and style, saying exactly "
                        "what they asked and nothing more."
                    ),
                },
                "channel": {"type": "string", "enum": list(chatkey.CHANNELS)},
                "send_at": {
                    "type": "string",
                    "description": (
                        "Only when the owner said when to send: the local time they "
                        "said, 'HH:MM' or 'YYYY-MM-DD HH:MM'. Omit to send after the tap."
                    ),
                },
            },
            "required": ["person", "text"],
        },
    },
    {
        "name": "set_personal",
        "description": (
            "PROPOSE marking a person or a chat personal (MIYA never writes there "
            "on its own) or unmarking it. Applies only after the owner's tap; "
            "unmarking asks for a confirmation first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "A person or chat as the owner named it.",
                },
                "personal": {"type": "boolean"},
            },
            "required": ["target", "personal"],
        },
    },
    {
        "name": "set_group_reply",
        "description": (
            "PROPOSE switching MIYA's draft replies in one group on or off (only "
            "to messages addressed to the owner). Applies only after the owner's "
            "tap; switching on asks for a confirmation first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "The group's title."},
                "enabled": {"type": "boolean"},
            },
            "required": ["group", "enabled"],
        },
    },
    {
        "name": "set_group_monitor",
        "description": (
            "PROPOSE starting or stopping reading one group or channel (the "
            "/chats switch). Applies only after the owner's tap; starting asks "
            "for a confirmation first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "The group's title."},
                "enabled": {"type": "boolean"},
            },
            "required": ["group", "enabled"],
        },
    },
    {
        "name": "sheets_resync",
        "description": (
            "PROPOSE rewriting the whole Google Sheets mirror from the database "
            "on its next run. Applies only after the owner's tap."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pending",
        "description": (
            "The owner's commanded messages still waiting (for a tap, or for "
            "their scheduled time): who to, the channel, when, the state. Reads "
            "only; proposes nothing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
ACT_TOOL_NAMES: Final = frozenset(str(tool["name"]) for tool in ACT_TOOLS)
PROPOSING: Final = ACT_TOOL_NAMES - {"list_pending"}


# --- what a turn proposed ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One person a name could mean, for the «Qaysi …?» buttons."""

    person_id: int
    name: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class Action:
    """One thing the bot shows under the answer: a send card (``outbox_id``),
    a «which one?» question, a chat that does not exist, a setting card. The
    names are counterparty-controlled: the bot shows them escaped."""

    kind: ActionKind
    outbox_id: int | None = None
    name: str | None = None
    channel: str | None = None
    candidates: tuple[Candidate, ...] = ()
    setting: str | None = None
    # The proposal's place on the command message (asks and settings).
    interaction_id: int | None = None
    index: int | None = None
    # The JV-12 confirm card's row: a reply rule id (unmark), a monitor id.
    target_id: int | None = None


@dataclass(slots=True)
class Turn:
    """One command turn: the owner's words, the message they came in, and
    what the tools proposed so far."""

    owner_text: str
    interaction_id: int | None
    now: datetime
    actions: list[Action] = field(default_factory=list)
    proposals: int = 0
    outbox_ids: set[int] = field(default_factory=set)
    settings_seen: set[tuple[str, int]] = field(default_factory=set)
    # A send or «which one?» already proposed this turn, and what the model
    # was told: the same proposal again is that answer, never a second card.
    sends_seen: dict[tuple[object, ...], str] = field(default_factory=dict)
    # A proposing tool was called (refused or not).
    attempted: bool = False

    def shown(self) -> list[Action]:
        """What the bot shows. A ``list_pending`` read stands for the turn
        only when nothing was tried besides it: a turn whose proposals were
        all refused proposed nothing, whatever it read."""
        if self.attempted:
            return [a for a in self.actions if a.kind != LISTED]
        return list(self.actions)


class _Refused(Exception):
    """A tool's refusal: the code (and a detail) the model is told."""

    def __init__(self, code: str, **detail: object) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _dumps(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _error(code: str, **detail: object) -> str:
    return _dumps({"error": code, **detail})


def _arg(args: Mapping[str, object], key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _flag(args: Mapping[str, object], key: str) -> bool | None:
    value = args.get(key)
    return value if isinstance(value, bool) else None


# --- the owner's own words --------------------------------------------------------------

# The case endings, folded the way normalise_for_search folds a word, longest
# first: «Sardorga», «Сардорга» and «Sardordan» are Sardor.
_SUFFIXES: Final = tuple(
    sorted(
        {normalise_for_search(s) for s in ALIAS_SUFFIXES if normalise_for_search(s)},
        key=len,
        reverse=True,
    )
)
_STEM_MIN: Final = 3


def strip_ending(word: str) -> str:
    """A folded word without its case ending («sardorga» → «sardor»)."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _STEM_MIN:
            return word[: -len(suffix)]
    return word


def _owner_words(text: str) -> frozenset[str]:
    tokens = normalise_for_search(text).split()
    return frozenset(tokens) | frozenset(strip_ending(t) for t in tokens)


def from_owner_words(name: str, owner_text: str) -> bool:
    """Every word of ``name`` is a word the owner wrote (a case ending
    aside): the model may only repeat a name, never bring one."""
    wanted = normalise_for_search(name).split()
    have = _owner_words(owner_text)
    return bool(wanted) and all(w in have or strip_ending(w) in have for w in wanted)


def without_endings(name: str) -> str:
    """«Sardorga» → «Sardor»: the name with each word's case ending gone."""
    return " ".join(strip_ending(w) for w in normalise_for_search(name).split())


# The case endings as written, longest first, to cut them off a word while
# keeping the owner's own spelling and casing.
_RAW_SUFFIXES: Final = tuple(
    sorted({s.casefold() for s in ALIAS_SUFFIXES}, key=len, reverse=True)
)


def _word_without_ending(word: str) -> str:
    folded = normalise_for_search(word)
    stem = strip_ending(folded)
    if not folded or stem == folded:
        return word
    for suffix in _RAW_SUFFIXES:
        if (
            word.casefold().endswith(suffix)
            and normalise_for_search(word[: -len(suffix)]) == stem
        ):
            return word[: -len(suffix)]
    return stem


def ask_name(name: str, candidates: Sequence[Person]) -> str:
    """The name «Qaysi {ism}?» shows: what the owner said, without a case
    ending («Sardorga» → «Sardor»), unless the whole word is one of the
    candidates' own names («Malika» next to «Malik» stays «Malika»)."""
    wanted = set(normalise_for_search(name).split())
    for person in candidates:
        own: set[str] = set()
        for known in (person.display_name, *(person.aliases or [])):
            own |= set(normalise_for_search(str(known)).split())
        if wanted and wanted <= own:
            return name
    shown = " ".join(_word_without_ending(w) for w in name.split())
    return shown or without_endings(name)


# Words that name a channel only in passing: «pochta keldi» is news to
# dictate, not «send it by Gmail».
_GENERIC_CHANNEL_WORDS: Final = frozenset({"mail", "pochta"})
_CHANNEL_WORDS: Final[Mapping[str, chatkey.Channel]] = {
    normalise_for_search(word): channel
    for word, channel in chatkey.CHANNEL_WORDS.items()
    # «wa» and «tg» would be read inside ordinary words («va» is «and»).
    if len(word) >= 4 and word not in _GENERIC_CHANNEL_WORDS
}


def channel_said(owner_text: str) -> tuple[chatkey.Channel, ...]:
    """The channels the owner named («Telegramdan», «vatsapga»)."""
    found: list[chatkey.Channel] = []
    for token in normalise_for_search(owner_text).split():
        channel = _CHANNEL_WORDS.get(token) or _CHANNEL_WORDS.get(strip_ending(token))
        if channel is not None and channel not in found:
            found.append(channel)
    return tuple(found)


def resolve_channel(proposed: str, owner_text: str) -> chatkey.Channel:
    """The channel of a send: the one the owner named, else Telegram. A
    channel the model chose that the owner did not say is refused. When the
    owner's words name two («Telegramdan yozib yubor: WhatsAppga rasm
    tashladim»), the model must pick one of them."""
    said = channel_said(owner_text)
    named = (
        chatkey.channel_word(proposed)
        or _CHANNEL_WORDS.get(normalise_for_search(proposed))
        if proposed
        else None
    )
    if len(said) > 1:
        if named is None or named not in said:
            raise _Refused(CHANNEL_DISAGREES, said=list(said))
        return named
    wanted = said[0] if said else chatkey.TELEGRAM
    if proposed and named != wanted:
        raise _Refused(CHANNEL_DISAGREES, owner_channel=wanted)
    return wanted


# --- the time: the owner's words decide (when.py) ---------------------------------------

_MODEL_CLOCK: Final = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")
_MODEL_MOMENT: Final = re.compile(
    r"^(?P<d>\d{4}-\d{2}-\d{2})[ T](?P<h>\d{1,2}):(?P<m>\d{2})(?::\d{2})?"
)


def _model_time(value: str) -> tuple[date | None, time]:
    """What the model proposed: a clock time, or a day and a clock time."""
    clock = _MODEL_CLOCK.match(value)
    found = clock or _MODEL_MOMENT.match(value)
    if found is None:
        raise _Refused(TIME_BAD)
    try:
        at = time(int(found.group("h")), int(found.group("m")))
        day = date.fromisoformat(found.group("d")) if clock is None else None
    except ValueError:
        raise _Refused(TIME_BAD) from None
    return day, at


def _without_name(owner_text: str, person: str) -> str:
    """The owner's words without the recipient's name: «Jumaga» is a
    person, not Friday."""
    names = {w for w in normalise_for_search(person).split() if w}
    names |= {strip_ending(w) for w in names}
    if not names:
        return owner_text
    kept = []
    for raw in owner_text.split():
        tokens = normalise_for_search(raw).split()
        if tokens and all(t in names or strip_ending(t) in names for t in tokens):
            continue
        kept.append(raw)
    return " ".join(kept)


def send_moment(
    proposed: str, owner_text: str, now: datetime, *, person: str = ""
) -> datetime | None:
    """``not_before`` for a send: None (right after the tap) when the model
    proposed no time; else the moment the owner's words name (``when.parse``,
    the recipient's name left out), which the model's time must agree with.
    «kechki 6 da» is 18:00 today while that is still ahead, else tomorrow."""
    if not proposed:
        return None
    local = now.astimezone(settings.tz)
    said = when.parse(_without_name(owner_text, person), today=local.date())
    if said is None or said.at is None:
        raise _Refused(TIME_NOT_SAID)
    day = said.day
    if day is None:
        today_at = datetime.combine(local.date(), said.at, tzinfo=settings.tz)
        day = local.date() if today_at > local else local.date() + timedelta(days=1)
    moment = datetime.combine(day, said.at, tzinfo=settings.tz)
    model_day, model_at = _model_time(proposed)
    if model_at != said.at or (model_day is not None and model_day != day):
        raise _Refused(TIME_DISAGREES, owner_time=moment.strftime("%Y-%m-%d %H:%M"))
    if moment <= local:
        raise _Refused(TIME_PAST)
    return moment


# --- the recipient: find_person on the owner's words -----------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    person: Person | None = None
    candidates: tuple[Person, ...] = ()
    unknown_code: str | None = None


def _accepted(match: people.Match) -> bool:
    return (
        match.person is not None
        and not match.ambiguous
        and (
            match.exact
            or match.via_code is not None
            or match.score >= people.MATCH_THRESHOLD
        )
    )


def _candidates_of(match: people.Match) -> tuple[Person, ...]:
    if not match.ambiguous:
        return ()
    return tuple(p for p in (match.person, match.runner_up) if p is not None)


async def resolve_recipient(session: AsyncSession, name: str) -> Resolution:
    """The one person ``name`` means, two candidates to ask about, or none.

    ``find_person`` only (a question-grade lookup that never creates and
    never learns an alias), retried once with the case endings stripped.
    Accepted only when not ambiguous and exact, by a client code, or at
    ``people.MATCH_THRESHOLD``."""
    first = await people.find_person(session, name)
    if _accepted(first) and first.exact:
        return Resolution(person=first.person)
    stripped = without_endings(name)
    second = (
        await people.find_person(session, stripped)
        if stripped and stripped != normalise_for_search(name)
        else None
    )
    # One person only when every reading agrees: «Sardorga» next to two
    # Sardors is asked back, and so is «Malika» (ambiguous as written) whose
    # stem «mali» alone would settle on Malik.
    found: dict[int, Person] = {}
    ambiguous = False
    for match in (second, first):
        if match is None:
            continue
        if _accepted(match) and match.person is not None:
            found.setdefault(match.person.id, match.person)
        elif match.ambiguous:
            ambiguous = True
            for person in _candidates_of(match):
                found.setdefault(person.id, person)
    if len(found) == 1 and not ambiguous:
        return Resolution(person=next(iter(found.values())))
    if len(found) > 1:
        return Resolution(candidates=tuple(found.values()))
    return Resolution(unknown_code=first.unknown_code)


def is_owner(person: Person) -> bool:
    owner_id = settings.owner_telegram_id
    return (
        owner_id is not None and person.telegram_id == owner_id
    ) or people.is_owner_alias(person.display_name)


async def _chat_exists(session: AsyncSession, tg_chat_id: int) -> bool:
    """The owner's account has this chat: its dialog was synced, or MIYA
    stored a message of it. Never a chat made up for the occasion."""
    monitor = await session.scalar(
        sa.select(ChatMonitor.id).where(ChatMonitor.tg_chat_id == tg_chat_id)
    )
    if monitor is not None:
        return True
    seen = await session.scalar(
        sa.select(Interaction.id)
        .where(
            Interaction.tg_chat_id == tg_chat_id,
            Interaction.source == InteractionSource.telegram_userbot,
        )
        .limit(1)
    )
    return seen is not None


async def existing_chat(
    session: AsyncSession, person: Person, channel: str
) -> int | None:
    """The person's private chat on ``channel`` that already exists, or None
    (rule 17). Only Telegram can carry a command today; a WhatsApp or Gmail
    chat exists once its channel does (JV-24 onwards)."""
    if channel != chatkey.TELEGRAM:
        return None
    ids: list[int] = []
    if person.telegram_id is not None:
        ids.append(int(person.telegram_id))
    prefix = f"{chatkey.PREFIX[chatkey.TELEGRAM]}:"
    for address in await people.known_addresses(session, person):
        if address.startswith(prefix):
            ids.append(int(address[len(prefix) :]))
    owner_id = settings.owner_telegram_id
    for chat_id in dict.fromkeys(ids):
        if chat_id <= 0 or chat_id == owner_id:
            continue
        if await _chat_exists(session, chat_id):
            return chat_id
    return None


async def _code(session: AsyncSession, person_id: int) -> str | None:
    found = await codes.codes_of(session, person_id)
    return found[0] if found else None


# --- the stored proposals on the command message ---------------------------------------


async def _command_message(
    session: AsyncSession, interaction_id: int | None, *, lock: bool = False
) -> Interaction | None:
    if interaction_id is None:
        return None
    query = sa.select(Interaction).where(Interaction.id == interaction_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    found: Interaction | None = await session.scalar(query)
    return found


def _stored(interaction: Interaction) -> list[dict[str, Any]]:
    meta = interaction.meta if isinstance(interaction.meta, dict) else {}
    found = meta.get(PROPOSALS_KEY)
    return (
        [dict(p) for p in found if isinstance(p, dict)] if isinstance(found, list) else []
    )


def _save(interaction: Interaction, proposals: Sequence[Mapping[str, Any]]) -> None:
    meta = dict(interaction.meta) if isinstance(interaction.meta, dict) else {}
    meta[PROPOSALS_KEY] = [dict(p) for p in proposals]
    interaction.meta = meta


async def _store(
    session: AsyncSession, turn: Turn, proposal: Mapping[str, Any]
) -> tuple[int, int]:
    """Keep a proposal on the command message: (message id, its index)."""
    interaction = await _command_message(session, turn.interaction_id)
    if interaction is None:
        raise _Refused(NO_COMMAND)
    proposals = _stored(interaction)
    proposals.append({**proposal, "done": None})
    _save(interaction, proposals)
    await session.flush()
    return interaction.id, len(proposals) - 1


def _count(turn: Turn) -> None:
    if turn.proposals >= MAX_PROPOSALS:
        raise _Refused(TOO_MANY, limit=MAX_PROPOSALS)


def _local(moment: datetime | None) -> datetime | None:
    return moment.astimezone(settings.tz) if moment is not None else None


def _when_text(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment is not None else "after_tap"


# --- the tools --------------------------------------------------------------------------


async def create_send(
    session: AsyncSession,
    *,
    person: Person,
    channel: str,
    body: str,
    not_before: datetime | None,
    interaction_id: int | None,
    now: datetime,
) -> OutboxMessage | None:
    """The outbox draft of one owner-commanded send, in an existing chat, or
    None when the person has no chat on ``channel``. A refusal of the outbox
    (Jarvis off, a hard exclusion) raises ``_Refused``."""
    if interaction_id is None:
        raise _Refused(NO_COMMAND)
    chat_id = await existing_chat(session, person, channel)
    if chat_id is None:
        return None
    try:
        row = await outbox.create(
            session,
            channel=channel,
            chat_key=chatkey.telegram(chat_id),
            origin=outbox.OWNER_COMMAND_ORIGIN,
            authored_by=gate.OWNER,
            body=body,
            now=now,
            person_id=person.id,
            command_interaction_id=interaction_id,
            not_before=not_before,
            by=BY_COMMAND,
        )
    except outbox.JarvisDisabled:
        raise _Refused(DISABLED) from None
    except outbox.OutboxError as exc:
        raise _Refused(str(exc)[:64] or FAILED) from None
    if row.state != outbox.DRAFT:
        raise _Refused(row.last_error or row.state)
    return row


async def _propose_send(
    session: AsyncSession, turn: Turn, args: Mapping[str, object]
) -> str:
    name = _arg(args, "person")
    body = _arg(args, "text")
    if not name or not body:
        raise _Refused("person_and_text_required")
    if len(body) > TEXT_MAX:
        raise _Refused(TOO_LONG, limit=TEXT_MAX)
    said = {value for _, value, _ in amounts.money_mentions(turn.owner_text)}
    if any(value not in said for _, value, _ in amounts.money_mentions(body)):
        # Rule 1: a figure in the text is the owner's, never the model's (nor
        # one a tool result such as open_debts carried).
        raise _Refused(FIGURE_NOT_SAID)
    if not from_owner_words(name, turn.owner_text):
        # Rule 16: a name only a tool result (or the model) brought is never
        # a recipient.
        raise _Refused(NOT_OWNERS_WORDS)
    if people.is_owner_alias(name) or people.is_owner_alias(without_endings(name)):
        raise _Refused(OWNER_REFUSED)
    channel = resolve_channel(_arg(args, "channel"), turn.owner_text)
    not_before = send_moment(
        _arg(args, "send_at"), turn.owner_text, turn.now, person=name
    )
    when_key = not_before.isoformat() if not_before else None
    found = await resolve_recipient(session, name)
    if found.candidates:
        key: tuple[object, ...] = (ASK, without_endings(name), channel, body, when_key)
        if key in turn.sends_seen:
            return turn.sends_seen[key]
        _count(turn)
        shown_name = ask_name(name, found.candidates)
        candidates = tuple(
            [
                Candidate(p.id, p.display_name, await _code(session, p.id))
                for p in found.candidates
            ]
        )
        interaction_id, index = await _store(
            session,
            turn,
            {
                "kind": ASK,
                "name": shown_name,
                "channel": channel,
                "text": body,
                "not_before": not_before.isoformat() if not_before else None,
                "candidates": [c.person_id for c in candidates],
            },
        )
        turn.proposals += 1
        turn.actions.append(
            Action(
                ASK,
                name=shown_name,
                channel=channel,
                candidates=candidates,
                interaction_id=interaction_id,
                index=index,
            )
        )
        turn.sends_seen[key] = _dumps(
            {
                "asked": "which_person",
                "candidates": [c.name for c in candidates],
                "note": "The owner was asked with buttons; wait for their tap.",
            }
        )
        return turn.sends_seen[key]
    person = found.person
    if person is None:
        if found.unknown_code:
            raise _Refused(NOT_FOUND, client_code=found.unknown_code)
        raise _Refused(NOT_FOUND, person=name)
    if is_owner(person):
        raise _Refused(OWNER_REFUSED)
    key = (SEND, person.id, channel, body, when_key)
    if key in turn.sends_seen:
        return turn.sends_seen[key]
    _count(turn)
    row = await create_send(
        session,
        person=person,
        channel=channel,
        body=body,
        not_before=not_before,
        interaction_id=turn.interaction_id,
        now=turn.now,
    )
    if row is None:
        missing = Action(NO_CHAT, name=person.display_name, channel=channel)
        if missing not in turn.actions:
            turn.actions.append(missing)
        raise _Refused(NO_CHAT_CODE, person=person.display_name, channel=channel)
    if row.not_before != not_before:
        # The outbox's idempotency key has no time in it: the same text to
        # the same chat this turn is the row already proposed, at its time.
        raise _Refused(ALREADY_PROPOSED, send_at=_when_text(_local(row.not_before)))
    if row.id not in turn.outbox_ids:
        turn.outbox_ids.add(row.id)
        turn.proposals += 1
        turn.actions.append(
            Action(SEND, outbox_id=row.id, name=person.display_name, channel=channel)
        )
    turn.sends_seen[key] = _dumps(
        {
            "proposed": f"j{row.id}",
            "to": person.display_name,
            "channel": channel,
            "send_at": _when_text(_local(row.not_before)),
            "note": "A card waits for the owner's tap; nothing has been sent.",
        }
    )
    return turn.sends_seen[key]


async def _person_or_chat(
    session: AsyncSession, target: str
) -> tuple[Person | None, ChatMonitor | None]:
    """A setting's target: a person (found as a recipient is), else a chat
    by its title."""
    found = await resolve_recipient(session, target)
    if found.candidates:
        raise _Refused(AMBIGUOUS, candidates=[p.display_name for p in found.candidates])
    if found.person is not None:
        if is_owner(found.person):
            raise _Refused(OWNER_REFUSED)
        return found.person, None
    titled = await chats.find_titled(
        session, target, kinds=(ChatType.private, ChatType.group)
    )
    if not titled:
        titled = await chats.find_titled(
            session, without_endings(target), kinds=(ChatType.private, ChatType.group)
        )
    if len(titled) > 1:
        raise _Refused(AMBIGUOUS, candidates=[c.title for c in titled])
    if not titled:
        raise _Refused(NOT_FOUND, target=target)
    return None, titled[0]


async def _group(
    session: AsyncSession, title: str, kinds: tuple[ChatType, ...]
) -> ChatMonitor:
    found = await chats.find_titled(session, title, kinds=kinds)
    if not found:
        found = await chats.find_titled(session, without_endings(title), kinds=kinds)
    if len(found) > 1:
        raise _Refused(AMBIGUOUS, candidates=[c.title for c in found])
    if not found:
        raise _Refused(NOT_FOUND, group=title)
    return found[0]


async def _propose_setting(
    session: AsyncSession,
    turn: Turn,
    setting: str,
    *,
    name: str | None,
    person_id: int | None = None,
    monitor_id: int | None = None,
    target_id: int | None = None,
) -> str:
    seen = (setting, person_id or monitor_id or target_id or 0)
    if seen in turn.settings_seen:
        return _dumps({"proposed": setting, "duplicate": True})
    _count(turn)
    if turn.interaction_id is None:
        raise _Refused(NO_COMMAND)
    interaction_id: int | None = turn.interaction_id
    index: int | None = None
    if setting not in JV12_CONFIRMED:
        interaction_id, index = await _store(
            session,
            turn,
            {
                "kind": SETTING,
                "setting": setting,
                "name": name,
                "person_id": person_id,
                "monitor_id": monitor_id,
            },
        )
    turn.settings_seen.add(seen)
    turn.proposals += 1
    turn.actions.append(
        Action(
            SETTING,
            name=name,
            setting=setting,
            interaction_id=interaction_id,
            index=index,
            target_id=target_id,
        )
    )
    return _dumps(
        {
            "proposed": setting,
            "target": name,
            "note": "A card waits for the owner's tap; nothing has changed yet.",
        }
    )


def _already(setting: str, name: str | None) -> str:
    return _dumps({"already": setting, "target": name, "note": "Nothing to change."})


async def _set_personal(
    session: AsyncSession, turn: Turn, args: Mapping[str, object]
) -> str:
    target = _arg(args, "target")
    personal = _flag(args, "personal")
    if not target or personal is None:
        raise _Refused("target_and_personal_required")
    if not from_owner_words(target, turn.owner_text):
        raise _Refused(NOT_OWNERS_WORDS)
    person, monitor = await _person_or_chat(session, target)
    name: str | None = None
    if person is not None:
        addresses = await people.known_addresses(session, person)
        rule = await policy.personal_rule(
            session,
            person_id=person.id,
            chat_keys=[a for a in addresses if a.startswith("tg:")],
        )
        name = person.display_name
    elif monitor is not None:
        rule = await policy.personal_rule(
            session, chat_keys=[chatkey.telegram(monitor.tg_chat_id)]
        )
        name = monitor.title
    else:
        raise _Refused(NOT_FOUND, target=target)
    if personal:
        if rule is not None:
            return _already(PERSONAL_ON, name)
        return await _propose_setting(
            session,
            turn,
            PERSONAL_ON,
            name=name,
            person_id=person.id if person is not None else None,
            monitor_id=monitor.id if monitor is not None else None,
        )
    if rule is None:
        return _already(PERSONAL_OFF, name)
    # Loosening: the confirm card of JV-12 (jr:y), nothing changes before it.
    return await _propose_setting(
        session, turn, PERSONAL_OFF, name=name, target_id=rule.id
    )


async def _set_group_reply(
    session: AsyncSession, turn: Turn, args: Mapping[str, object]
) -> str:
    title = _arg(args, "group")
    enabled = _flag(args, "enabled")
    if not title or enabled is None:
        raise _Refused("group_and_enabled_required")
    if not from_owner_words(title, turn.owner_text):
        raise _Refused(NOT_OWNERS_WORDS)
    monitor = await _group(session, title, (ChatType.group,))
    state = await policy.group_reply_state(session, chatkey.telegram(monitor.tg_chat_id))
    if state == "personal":
        raise _Refused("personal_group", group=monitor.title)
    if (state == "on") == enabled:
        return _already(GROUP_REPLY_ON if enabled else GROUP_REPLY_OFF, monitor.title)
    if enabled:
        # Loosening: the confirm card of JV-12 (jr:g).
        return await _propose_setting(
            session, turn, GROUP_REPLY_ON, name=monitor.title, target_id=monitor.id
        )
    return await _propose_setting(
        session, turn, GROUP_REPLY_OFF, name=monitor.title, monitor_id=monitor.id
    )


async def _set_group_monitor(
    session: AsyncSession, turn: Turn, args: Mapping[str, object]
) -> str:
    title = _arg(args, "group")
    enabled = _flag(args, "enabled")
    if not title or enabled is None:
        raise _Refused("group_and_enabled_required")
    if not from_owner_words(title, turn.owner_text):
        raise _Refused(NOT_OWNERS_WORDS)
    monitor = await _group(session, title, (ChatType.group, ChatType.channel))
    if bool(monitor.monitor_enabled) == enabled:
        return _already(MONITOR_ON if enabled else MONITOR_OFF, monitor.title)
    return await _propose_setting(
        session,
        turn,
        MONITOR_ON if enabled else MONITOR_OFF,
        name=monitor.title,
        monitor_id=monitor.id,
    )


async def _sheets_resync(session: AsyncSession, turn: Turn) -> str:
    if not settings.sheets_enabled:
        raise _Refused("sheets_disabled")
    return await _propose_setting(session, turn, SHEETS_RESYNC, name=None)


async def pending(
    session: AsyncSession, *, limit: int = PENDING_LIMIT
) -> list[OutboxMessage]:
    """The owner's commanded sends still waiting: for a tap, or approved and
    waiting for their time or their turn."""
    return list(
        await session.scalars(
            sa.select(OutboxMessage)
            .where(
                OutboxMessage.origin == outbox.OWNER_COMMAND_ORIGIN,
                OutboxMessage.state.in_((outbox.DRAFT, outbox.APPROVED)),
            )
            .order_by(OutboxMessage.id.desc())
            .limit(limit)
        )
    )


async def _list_pending(session: AsyncSession, turn: Turn) -> str:
    rows = await pending(session)
    names: dict[int, str] = {}
    ids = {r.person_id for r in rows if r.person_id is not None}
    if ids:
        found = await session.execute(
            sa.select(Person.id, Person.display_name).where(Person.id.in_(ids))
        )
        names = {int(i): str(n) for i, n in found}
    if not any(a.kind == LISTED for a in turn.actions):
        turn.actions.append(Action(LISTED))
    return _dumps(
        {
            "pending": [
                {
                    "ref": f"j{r.id}",
                    "to": names.get(r.person_id) if r.person_id is not None else None,
                    "channel": r.channel,
                    "state": r.state,
                    "send_at": _when_text(_local(r.not_before)),
                }
                for r in rows
            ],
            "note": "Read only. Nothing here is a reason to propose anything.",
        }
    )


async def run_tool(
    session: AsyncSession, turn: Turn, name: str, args: Mapping[str, object]
) -> str:
    """Run one act tool for the model. Never raises: a refusal or an error
    comes back as JSON, and a failure leaves the session as it was."""
    if name not in ACT_TOOL_NAMES:
        return _error(f"unknown tool: {name}")
    if name in PROPOSING:
        turn.attempted = True
        if not settings.jarvis_enabled:
            return _error(DISABLED)
    try:
        async with session.begin_nested():
            if name == "propose_send_message":
                return await _propose_send(session, turn, args)
            if name == "set_personal":
                return await _set_personal(session, turn, args)
            if name == "set_group_reply":
                return await _set_group_reply(session, turn, args)
            if name == "set_group_monitor":
                return await _set_group_monitor(session, turn, args)
            if name == "sheets_resync":
                return await _sheets_resync(session, turn)
            return await _list_pending(session, turn)
    except _Refused as refused:
        log.info(
            "owner command refused",
            extra={
                "tool": name,
                "code": refused.code,
                "interaction_id": turn.interaction_id,
            },
        )
        return _error(refused.code, **refused.detail)
    except Exception as exc:
        log.error(
            "owner command tool failed",
            extra={
                "tool": name,
                "interaction_id": turn.interaction_id,
                "error": type(exc).__name__,
            },
        )
        return _error(FAILED)


async def style_hint(session: AsyncSession) -> str | None:
    """How the owner writes (the active style profile's prose, JV-14), for
    the text of a message the owner dictates. None without one; a failure to
    read it costs the command nothing."""
    try:
        async with session.begin_nested():
            profile = await style.active_profile(session)
    except Exception as exc:
        log.error("style profile unreadable", extra={"error": type(exc).__name__})
        return None
    text = (profile.prompt_text or "").strip() if profile is not None else ""
    return text[:STYLE_HINT_CHARS] or None


# --- the owner's taps on what a turn proposed -------------------------------------------


@dataclass(frozen=True, slots=True)
class TapResult:
    """What a candidate or setting tap came to. ``outbox_id`` is the send a
    candidate tap proposed; ``state`` the setting's state after it."""

    code: TapCode
    outbox_id: int | None = None
    name: str | None = None
    channel: str | None = None
    setting: str | None = None
    state: str | None = None


def _mark_done(
    interaction: Interaction,
    proposals: list[dict[str, Any]],
    index: int,
    outcome: str,
    *,
    now: datetime,
) -> None:
    proposals[index] = {**proposals[index], "done": outcome, "done_at": now.isoformat()}
    _save(interaction, proposals)


async def _proposal(
    session: AsyncSession, interaction_id: int, index: int, kind: str
) -> tuple[Interaction, list[dict[str, Any]], dict[str, Any]] | None:
    interaction = await _command_message(session, interaction_id, lock=True)
    if interaction is None:
        return None
    proposals = _stored(interaction)
    if not 0 <= index < len(proposals) or proposals[index].get("kind") != kind:
        return None
    return interaction, proposals, proposals[index]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def pick_candidate(
    session: AsyncSession,
    interaction_id: int,
    index: int,
    person_id: int,
    *,
    now: datetime,
) -> TapResult:
    """The owner tapped one person under «Qaysi …?»: the send they asked for
    is proposed to that person (a card), never sent. Only one of the people
    offered there, once; a time that has passed since makes it stale."""
    found = await _proposal(session, interaction_id, index, ASK)
    if found is None:
        return TapResult("stale")
    interaction, proposals, entry = found
    if entry.get("done"):
        return TapResult("answered")
    offered = [i for i in (_int(c) for c in entry.get("candidates") or []) if i]
    if person_id not in offered:
        return TapResult("stale")
    raw_when = entry.get("not_before")
    not_before = datetime.fromisoformat(raw_when) if isinstance(raw_when, str) else None
    person = await session.get(Person, person_id)
    channel = entry.get("channel")
    body = entry.get("text")
    if (
        person is None
        or is_owner(person)
        or not isinstance(channel, str)
        or not isinstance(body, str)
        or (not_before is not None and not_before <= now)
    ):
        _mark_done(interaction, proposals, index, "stale", now=now)
        await session.flush()
        return TapResult("stale")
    try:
        async with session.begin_nested():
            row = await create_send(
                session,
                person=person,
                channel=channel,
                body=body,
                not_before=not_before,
                interaction_id=interaction.id,
                now=now,
            )
    except _Refused as refused:
        log.info(
            "candidate send refused",
            extra={"interaction_id": interaction.id, "code": refused.code},
        )
        _mark_done(interaction, proposals, index, "refused", now=now)
        await session.flush()
        return TapResult("stale", name=person.display_name, channel=channel)
    if row is None:
        _mark_done(interaction, proposals, index, NO_CHAT_CODE, now=now)
        await session.flush()
        return TapResult("no_chat", name=person.display_name, channel=channel)
    _mark_done(interaction, proposals, index, f"j{row.id}", now=now)
    await session.flush()
    return TapResult(
        "sent_card", outbox_id=row.id, name=person.display_name, channel=channel
    )


async def _apply(
    session: AsyncSession, entry: Mapping[str, Any], *, now: datetime
) -> tuple[str | None, str | None]:
    """Make the change a setting proposal named: (its name, its state). The
    loosening ones are applied by the JV-12 confirm taps, never here."""
    setting = entry.get("setting")
    person_id = _int(entry.get("person_id"))
    monitor_id = _int(entry.get("monitor_id"))
    monitor = await session.get(ChatMonitor, monitor_id) if monitor_id else None
    if setting == PERSONAL_ON:
        if person_id is not None:
            person = await session.get(Person, person_id)
            if person is None:
                return None, None
            await policy.mark_person(session, person, by=BY_BUTTON, now=now)
            return person.display_name, "personal"
        if monitor is None:
            return None, None
        await policy.mark_chat(session, monitor, by=BY_BUTTON, now=now)
        return monitor.title, "personal"
    if setting == GROUP_REPLY_OFF and monitor is not None:
        state = await policy.set_group_reply(
            session,
            chat_key=chatkey.telegram(monitor.tg_chat_id),
            enabled=False,
            by=BY_BUTTON,
            now=now,
        )
        return monitor.title, state
    if setting in (MONITOR_ON, MONITOR_OFF) and monitor is not None:
        changed = await chats.set_monitor(
            session, monitor.id, setting == MONITOR_ON, now=now
        )
        if changed is None:
            return None, None
        return changed.title, "on" if changed.monitor_enabled else "off"
    if setting == SHEETS_RESYNC:
        queued = await sheets_sync.request_resync(session)
        return None, "queued" if queued >= 0 else None
    return None, None


async def tap_setting(
    session: AsyncSession,
    interaction_id: int,
    index: int,
    *,
    apply: bool,
    now: datetime,
) -> TapResult:
    """The owner's ✅ or ✖️ on a setting card, under the command message's
    lock: once only, re-checked at the tap."""
    found = await _proposal(session, interaction_id, index, SETTING)
    if found is None:
        return TapResult("stale")
    interaction, proposals, entry = found
    setting = entry.get("setting")
    name = entry.get("name") if isinstance(entry.get("name"), str) else None
    if entry.get("done"):
        return TapResult("answered", setting=str(setting), name=name)
    if not apply:
        _mark_done(interaction, proposals, index, "cancelled", now=now)
        await session.flush()
        return TapResult("cancelled", setting=str(setting), name=name)
    if setting not in SETTINGS or setting in JV12_CONFIRMED:
        return TapResult("stale")
    async with session.begin_nested():
        shown, state = await _apply(session, entry, now=now)
    if state is None:
        _mark_done(interaction, proposals, index, "stale", now=now)
        await session.flush()
        return TapResult("stale", setting=str(setting), name=name)
    _mark_done(interaction, proposals, index, "applied", now=now)
    await session.flush()
    log.info(
        "owner command setting applied",
        extra={"interaction_id": interaction.id, "setting": setting, "state": state},
    )
    return TapResult("applied", setting=str(setting), name=shown or name, state=state)


__all__ = [
    "ACT_TOOLS",
    "ACT_TOOL_NAMES",
    "ASK",
    "JV12_CONFIRMED",
    "LISTED",
    "LOOSENING",
    "MAX_PROPOSALS",
    "NO_CHAT",
    "SEND",
    "SETTING",
    "SETTINGS",
    "Action",
    "Candidate",
    "Resolution",
    "TapResult",
    "Turn",
    "ask_name",
    "channel_said",
    "create_send",
    "existing_chat",
    "from_owner_words",
    "pending",
    "pick_candidate",
    "resolve_channel",
    "resolve_recipient",
    "run_tool",
    "send_moment",
    "strip_ending",
    "style_hint",
    "tap_setting",
    "without_endings",
]
