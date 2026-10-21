"""Who the AI may write to, and how freely (docs/jarvis-plan.md §5.4, §6.1).

Two kinds of policy live here:

* **Modes, pauses and breakers** in ``jarvis_policy``. The effective mode of
  a reply resolves from the most specific scope that sets one
  (``channel:<c>:intent:<i>``, ``intent:<i>``, ``channel:<c>``, ``global``)
  and never exceeds the env ceiling ``JARVIS_MODE``. A pause stops every send
  in its scope; one without an end lasts until the owner resumes it. A
  breaker is the same stop tripped by a platform limit, always with an end.
* **Reply rules and hard exclusions.** Precedence: any active ``never`` rule
  wins, then the hard exclusions (Saved Messages, Telegram's service chat,
  bots, channels, the owner's own aliases, WhatsApp broadcasts), then groups
  the owner has not enabled, then the chat rule, the person rule, the channel
  default and the mode above. Restricting is immediate; loosening is the
  owner's confirmed tap (JV-12).
* **The owner's personal deny-list and group switch (JV-12).** ``/shaxsiy``
  marks a person or a chat ``never`` at once and snapshots every address
  they are known at; unmarking and switching a group on are confirmed taps.
  Suggestions of likely family and friends come from SQL signals only, and
  nothing is marked without the owner. No code path here loosens a rule on
  its own: only ``unmark_personal`` and ``set_group_reply``, called from the
  owner's confirmed tap, ever do.

The resolution functions are pure; the loaders and writers take a session
and never commit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType, Direction
from miya.db.models import (
    ChatMonitor,
    ClientCode,
    Interaction,
    JarvisPolicy,
    Person,
    ReplyRule,
)
from miya.jarvis import chatkey
from miya.jarvis.chatkey import Channel
from miya.services import codes, people
from miya.services.text import normalise_for_search

log = logging.getLogger(__name__)

Mode = Literal["off", "shadow", "draft", "auto"]
MODES: Final[tuple[Mode, ...]] = ("off", "shadow", "draft", "auto")
MODE_RANK: Final[dict[Mode, int]] = {mode: rank for rank, mode in enumerate(MODES)}
# What a missing global row resolves to: the migration's seed.
DEFAULT_MODE: Final[Mode] = "shadow"

GLOBAL: Final = "global"

# pause_reason / breaker_reason values (String(32)).
PAUSE_OWNER: Final = "owner"

# reply_rules values.
RULE_NEVER: Final = "never"
RULE_DRAFT: Final = "draft"
RULE_AUTO: Final = "auto"
RULE_ACTIVE: Final = "active"
RULE_REVOKED: Final = "revoked"
REASON_GROUP: Final = "group"
REASON_PERSONAL: Final = "personal"
SUBJECT_PERSON: Final = "person"
SUBJECT_CHAT: Final = "chat"
SUBJECT_ADDRESS: Final = "address"
# The history key that ties an address snapshot to the rule it was taken
# for, so unmarking the person revokes every address of that one marking.
# A rule another marking relies on (it already held that marking's chat or
# address) gets a ``LINKED_FIELD`` entry with the same key: it lives as long
# as any marking it belongs to.
LINK_KEY: Final = "of_rule"
LINKED_FIELD: Final = "linked"

# Hard exclusions: never written to by anyone through the outbox.
TELEGRAM_SERVICE_ID: Final = 777000
EXCLUDED_SAVED_MESSAGES: Final = "saved_messages"
EXCLUDED_SERVICE_CHAT: Final = "service_chat"
EXCLUDED_BOT: Final = "bot_chat"
EXCLUDED_CHANNEL: Final = "channel_chat"
EXCLUDED_OWNER: Final = "owner_alias"
EXCLUDED_BROADCAST: Final = "broadcast_chat"
# Refusal codes of the group line (AI-authored rows only).
GROUP_OFF: Final = "group_off"
GROUP_NOT_ADDRESSED: Final = "group_not_addressed"
PERSONAL_CHAT: Final = "personal_chat"


def channel_scope(channel: str) -> str:
    return f"channel:{channel}"


def intent_scope(intent: str) -> str:
    return f"intent:{intent}"


def channel_intent_scope(channel: str, intent: str) -> str:
    return f"channel:{channel}:intent:{intent}"


def as_mode(value: object) -> Mode | None:
    for mode in MODES:
        if value == mode:
            return mode
    return None


def lower_mode(a: Mode, b: Mode) -> Mode:
    return a if MODE_RANK[a] <= MODE_RANK[b] else b


def ceiling() -> Mode:
    """The env ceiling; ``off`` whenever Jarvis is switched off."""
    if not settings.jarvis_enabled:
        return "off"
    return as_mode(settings.jarvis_mode) or "off"


# --- modes, pauses, breakers -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """One ``jarvis_policy`` row, detached from the session."""

    scope: str
    mode: Mode | None = None
    paused_until: datetime | None = None
    pause_reason: str | None = None
    breaker_until: datetime | None = None
    breaker_reason: str | None = None

    def paused(self, now: datetime) -> bool:
        if self.pause_reason is None:
            return False
        return self.paused_until is None or self.paused_until > now

    def tripped(self, now: datetime) -> bool:
        return self.breaker_until is not None and self.breaker_until > now


Policies = Mapping[str, PolicySnapshot]


def snapshot(row: JarvisPolicy) -> PolicySnapshot:
    return PolicySnapshot(
        scope=row.scope,
        mode=as_mode(row.mode),
        paused_until=row.paused_until,
        pause_reason=row.pause_reason,
        breaker_until=row.breaker_until,
        breaker_reason=row.breaker_reason,
    )


async def load(session: AsyncSession) -> dict[str, PolicySnapshot]:
    rows = await session.scalars(sa.select(JarvisPolicy))
    return {row.scope: snapshot(row) for row in rows}


def stored_mode(policies: Policies, channel: str, intent: str | None) -> Mode:
    """The most specific mode set in ``jarvis_policy``, before the ceiling."""
    scopes: list[str] = []
    if intent:
        scopes += [channel_intent_scope(channel, intent), intent_scope(intent)]
    scopes += [channel_scope(channel), GLOBAL]
    for scope in scopes:
        policy = policies.get(scope)
        if policy is not None and policy.mode is not None:
            return policy.mode
    return DEFAULT_MODE


def effective_mode(
    policies: Policies, channel: str, intent: str | None, *, env_ceiling: Mode
) -> Mode:
    """The mode a reply of ``intent`` on ``channel`` runs in (§6.1)."""
    return lower_mode(stored_mode(policies, channel, intent), env_ceiling)


@dataclass(frozen=True, slots=True)
class Stop:
    """Why sending is stopped in a scope, and until when (None: until resumed)."""

    code: str
    until: datetime | None


def stop_for(policies: Policies, channel: str, now: datetime) -> Stop | None:
    """The global or channel pause or breaker in force, if any."""
    for scope in (GLOBAL, channel_scope(channel)):
        policy = policies.get(scope)
        if policy is None:
            continue
        if policy.paused(now):
            prefix = "paused" if scope == GLOBAL else "channel_paused"
            return Stop(code=prefix, until=policy.paused_until)
        if policy.tripped(now):
            return Stop(code="breaker", until=policy.breaker_until)
    return None


def _entry(
    field: str, old: object, new: object, *, by: str, now: datetime
) -> dict[str, object]:
    return {
        "at": now.isoformat(),
        "field": field,
        "old": old.isoformat() if isinstance(old, datetime) else old,
        "new": new.isoformat() if isinstance(new, datetime) else new,
        "by": by,
    }


async def _locked(session: AsyncSession, scope: str) -> JarvisPolicy:
    """The scope's row under a row lock, created when missing."""
    row: JarvisPolicy | None = await session.scalar(
        sa.select(JarvisPolicy).where(JarvisPolicy.scope == scope).with_for_update()
    )
    if row is not None:
        return row
    await session.execute(
        pg_insert(JarvisPolicy)
        .values(scope=scope, history=[])
        .on_conflict_do_nothing(index_elements=[JarvisPolicy.scope])
    )
    row = await session.scalar(
        sa.select(JarvisPolicy)
        .where(JarvisPolicy.scope == scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        # The insert above or a concurrent one made it; a missing row now is
        # a database that refuses writes, and the caller's error to see.
        raise LookupError(scope)
    return row


async def pause(
    session: AsyncSession,
    *,
    scope: str = GLOBAL,
    reason: str = PAUSE_OWNER,
    until: datetime | None = None,
    by: str,
    now: datetime,
) -> JarvisPolicy:
    """Stop every send in ``scope`` (``/jarvis toxtat``). Checked at claim time."""
    row = await _locked(session, scope)
    history = list(row.history or [])
    history.append(_entry("pause", row.pause_reason, reason, by=by, now=now))
    row.pause_reason = reason
    row.paused_until = until
    row.history = history
    await session.flush()
    return row


async def resume(
    session: AsyncSession, *, scope: str = GLOBAL, by: str, now: datetime
) -> JarvisPolicy:
    """Lift the pause and the breaker of ``scope`` (``/jarvis yoq [kanal]``)."""
    row = await _locked(session, scope)
    history = list(row.history or [])
    if row.pause_reason is not None or row.paused_until is not None:
        history.append(_entry("pause", row.pause_reason, None, by=by, now=now))
    if row.breaker_reason is not None or row.breaker_until is not None:
        history.append(_entry("breaker", row.breaker_reason, None, by=by, now=now))
    row.pause_reason = None
    row.paused_until = None
    row.breaker_reason = None
    row.breaker_until = None
    row.history = history
    await session.flush()
    return row


async def trip_breaker(
    session: AsyncSession,
    *,
    channel: Channel,
    reason: str,
    until: datetime,
    by: str,
    now: datetime,
) -> JarvisPolicy:
    """Stop one channel after a platform limit; reading goes on (§5.7)."""
    row = await _locked(session, channel_scope(channel))
    history = list(row.history or [])
    history.append(_entry("breaker", row.breaker_reason, reason, by=by, now=now))
    row.breaker_reason = reason[:32]
    row.breaker_until = until
    row.history = history
    await session.flush()
    return row


async def set_mode(
    session: AsyncSession, *, scope: str, mode: Mode | None, by: str, now: datetime
) -> JarvisPolicy:
    """Store a scope's mode (None inherits). The ceiling still applies."""
    row = await _locked(session, scope)
    if row.mode != mode:
        history = list(row.history or [])
        history.append(_entry("mode", row.mode, mode, by=by, now=now))
        row.mode = mode
        row.history = history
        await session.flush()
    return row


# --- reply rules and hard exclusions ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    id: int
    subject: str
    mode: str
    reason: str
    person_id: int | None = None
    chat_key: str | None = None
    address: str | None = None


async def load_rules(
    session: AsyncSession,
    *,
    chat_key: str,
    person_id: int | None,
    addresses: Iterable[str] = (),
) -> tuple[RuleSnapshot, ...]:
    """Every active rule that names this chat, its person or an address.

    A direct chat's key is itself its channel-qualified address (``tg:<user
    id>``, ``wa:<jid>``), so an address snapshot holds that chat even for a
    row that carries no address of its own. A group or mail-thread key never
    equals a stored address.
    """
    targets = [
        ReplyRule.chat_key == chat_key,
        ReplyRule.address.in_(sorted({chat_key, chat_key.casefold()})),
    ]
    if person_id is not None:
        targets.append(ReplyRule.person_id == person_id)
    wanted = [a for a in addresses if a]
    if wanted:
        targets.append(ReplyRule.address.in_(wanted))
    rows = await session.scalars(
        sa.select(ReplyRule)
        .where(ReplyRule.state == RULE_ACTIVE, sa.or_(*targets))
        .order_by(ReplyRule.id)
    )
    return tuple(
        RuleSnapshot(
            id=row.id,
            subject=row.subject,
            mode=row.mode,
            reason=row.reason,
            person_id=row.person_id,
            chat_key=row.chat_key,
            address=row.address,
        )
        for row in rows
    )


def never_rule(rules: Sequence[RuleSnapshot]) -> RuleSnapshot | None:
    """Any active ``never`` rule wins over everything (§5.4)."""
    return next((rule for rule in rules if rule.mode == RULE_NEVER), None)


def rule_mode(rules: Sequence[RuleSnapshot]) -> str | None:
    """The chat rule's mode, else the person's (or an address's), else None."""
    for subjects in (("chat",), ("person", "address")):
        for rule in rules:
            if rule.subject in subjects:
                return rule.mode
    return None


def group_enabled(rules: Sequence[RuleSnapshot]) -> bool:
    """The owner switched AI replies on for this group (JV-12)."""
    return any(
        rule.subject == "chat"
        and rule.reason == REASON_GROUP
        and rule.mode in (RULE_DRAFT, RULE_AUTO)
        for rule in rules
    )


@dataclass(frozen=True, slots=True)
class ChatFacts:
    """What is known about the recipient chat, from SQL and the adapter."""

    chat_key: str
    channel: str
    tg_chat_id: int | None = None
    # private | group | channel; None when the chat was never seen.
    chat_type: str | None = None
    is_bot: bool = False
    # The account's own chat (Telegram Saved Messages).
    is_self: bool = False
    # The recipient is the owner under one of their aliases.
    is_owner: bool = False


def _whatsapp_jid(chat_key: str) -> str:
    """The jid of a ``wa:`` chat key, casefolded; empty for any other channel."""
    prefix, _, ident = (chat_key or "").partition(":")
    return ident.strip().casefold() if prefix == chatkey.PREFIX[chatkey.WHATSAPP] else ""


def is_broadcast(facts: ChatFacts) -> bool:
    """WhatsApp status updates, newsletters and broadcast lists (§5.4)."""
    jid = _whatsapp_jid(facts.chat_key)
    return jid.endswith("@broadcast") or jid.endswith("@newsletter")


def chat_kind(facts: ChatFacts) -> str | None:
    """private | group | channel, from the facts or, for a WhatsApp chat the
    adapter has not described, from its jid (``@g.us`` is a group)."""
    if facts.chat_type is not None:
        return facts.chat_type
    if _whatsapp_jid(facts.chat_key).endswith("@g.us"):
        return "group"
    return None


def hard_exclusion(facts: ChatFacts) -> str | None:
    """A chat nobody may write to through the outbox, whatever the rules say."""
    if facts.is_self:
        return EXCLUDED_SAVED_MESSAGES
    if facts.tg_chat_id == TELEGRAM_SERVICE_ID:
        return EXCLUDED_SERVICE_CHAT
    if facts.is_bot:
        return EXCLUDED_BOT
    if facts.chat_type == "channel":
        return EXCLUDED_CHANNEL
    if is_broadcast(facts):
        return EXCLUDED_BROADCAST
    if facts.is_owner:
        return EXCLUDED_OWNER
    return None


def ai_refusal(
    facts: ChatFacts, rules: Sequence[RuleSnapshot], *, addressed: bool | None
) -> str | None:
    """Why MIYA may not put its own text in this chat, or None (§5.4).

    The precedence, first match wins: any active ``never`` rule, the hard
    exclusions, a group the owner has not switched on, a group message not
    addressed to the owner (``addressed`` False; None while it is not known,
    as when a row is created before its trigger is read). No rule, no mode
    and no setting can lift an earlier line.
    """
    if never_rule(rules) is not None:
        return PERSONAL_CHAT
    excluded = hard_exclusion(facts)
    if excluded is not None:
        return excluded
    if chat_kind(facts) == "group":
        if not group_enabled(rules):
            return GROUP_OFF
        if addressed is False:
            return GROUP_NOT_ADDRESSED
    return None


def looks_like_bot(username: str | None) -> bool:
    """Telegram reserves the «bot» suffix for bot accounts."""
    return bool(username) and str(username).casefold().endswith("bot")


# --- the owner's rules: personal chats and group replies (JV-12) ----------------------


def _rule_entry(
    field_name: str,
    old: object,
    new: object,
    *,
    by: str,
    now: datetime,
    link: int | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "at": now.isoformat(),
        "field": field_name,
        "old": old,
        "new": new,
        "by": by[:16],
    }
    if link is not None:
        entry[LINK_KEY] = link
    return entry


def _append(rule: ReplyRule, entry: dict[str, object]) -> None:
    rule.history = [*(rule.history or []), entry]


def _tighten(rule: ReplyRule, *, by: str, now: datetime) -> bool:
    """Make a rule the owner's personal ``never``; True when it changed."""
    changed = False
    if rule.mode != RULE_NEVER:
        _append(rule, _rule_entry("mode", rule.mode, RULE_NEVER, by=by, now=now))
        rule.mode = RULE_NEVER
        changed = True
    if rule.reason != REASON_PERSONAL:
        _append(rule, _rule_entry("reason", rule.reason, REASON_PERSONAL, by=by, now=now))
        rule.reason = REASON_PERSONAL
        changed = True
    return changed


def _revoke(rule: ReplyRule, *, by: str, now: datetime) -> None:
    _append(rule, _rule_entry("state", rule.state, RULE_REVOKED, by=by, now=now))
    rule.state = RULE_REVOKED


def _link_value(entry: object) -> int | None:
    value = entry.get(LINK_KEY) if isinstance(entry, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def link_of(history: Iterable[Any]) -> int | None:
    """The rule an address snapshot was taken for, from its history."""
    for entry in history or []:
        if isinstance(entry, dict) and entry.get("field") == LINKED_FIELD:
            continue
        value = _link_value(entry)
        if value is not None:
            return value
    return None


def links_of(history: Iterable[Any]) -> list[int]:
    """Every marking a rule belongs to: the one its snapshot was taken for
    and every one that relies on it."""
    found = (_link_value(entry) for entry in history or [])
    return list(dict.fromkeys(value for value in found if value is not None))


def _rely_on(rule: ReplyRule, head: ReplyRule, *, by: str, now: datetime) -> None:
    """``head`` relies on ``rule`` for a chat or an address: tie them, so
    unmarking either one never drops what the other still needs. Never in a
    loop: a rule the head already belongs to is not tied back."""
    if rule.id == head.id or head.id in links_of(rule.history):
        return
    if rule.id in links_of(head.history):
        return
    _append(rule, _rule_entry(LINKED_FIELD, None, head.id, by=by, now=now, link=head.id))


async def _locked_rules(
    session: AsyncSession,
    *,
    person_id: int | None,
    chat_keys: Sequence[str],
    addresses: Sequence[str],
) -> list[ReplyRule]:
    """Every active rule naming the person, a chat key or an address, locked."""
    targets: list[sa.ColumnElement[bool]] = []
    if person_id is not None:
        targets.append(ReplyRule.person_id == person_id)
    if chat_keys:
        targets.append(ReplyRule.chat_key.in_(list(chat_keys)))
    if addresses:
        targets.append(ReplyRule.address.in_(list(addresses)))
    if not targets:
        return []
    rows = await session.scalars(
        sa.select(ReplyRule)
        .where(ReplyRule.state == RULE_ACTIVE, sa.or_(*targets))
        .order_by(ReplyRule.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _insert(session: AsyncSession, rule: ReplyRule) -> bool:
    """Add one rule in a savepoint; False when an active rule already holds
    its person, chat or address (a concurrent tap made it first)."""
    try:
        async with session.begin_nested():
            session.add(rule)
            await session.flush()
    except IntegrityError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class Marked:
    """The rule that now keeps MIYA out of the chat."""

    rule_id: int
    # It was a ``never`` rule before this call.
    already: bool


async def mark_personal(
    session: AsyncSession,
    *,
    person_id: int | None,
    chat_key: str | None,
    addresses: Sequence[str],
    by: str,
    now: datetime,
) -> Marked:
    """Mark a person or a chat personal: ``never``, at once (§5.4).

    The marking rule names the person (or the chat) and the first free
    address; every other address gets its own snapshot rule, tied to the
    marking by ``LINK_KEY`` so unmarking takes them all back. A rule already
    holding the chat or an address is tightened to ``never``, never loosened.
    """
    for attempt in (1, 2):
        try:
            async with session.begin_nested():
                return await _mark(
                    session,
                    person_id=person_id,
                    chat_key=chat_key,
                    addresses=addresses,
                    by=by,
                    now=now,
                )
        except _Raced:
            # A concurrent tap marked the same chat between the lock and the
            # insert; the second pass finds its rule and tightens it.
            if attempt == 2:
                raise LookupError("reply_rule_conflict") from None
    raise LookupError("reply_rule_conflict")


class _Raced(Exception):
    """The marking rule's insert lost to a concurrent one."""


async def _mark(
    session: AsyncSession,
    *,
    person_id: int | None,
    chat_key: str | None,
    addresses: Sequence[str],
    by: str,
    now: datetime,
) -> Marked:
    if person_id is None and not chat_key:
        raise ValueError("no_target")
    wanted = [a for a in dict.fromkeys(addresses) if a]
    keys = [chat_key] if chat_key else []
    existing = await _locked_rules(
        session, person_id=person_id, chat_keys=keys, addresses=wanted
    )
    if person_id is not None:
        head = next((r for r in existing if r.person_id == person_id), None)
    else:
        head = next((r for r in existing if r.chat_key == chat_key), None)
    already = head is not None and head.mode == RULE_NEVER
    if head is None:
        held_keys = {r.chat_key for r in existing if r.chat_key}
        held_addresses = {r.address for r in existing if r.address}
        head = ReplyRule(
            subject=SUBJECT_PERSON if person_id is not None else SUBJECT_CHAT,
            person_id=person_id,
            chat_key=chat_key if chat_key and chat_key not in held_keys else None,
            address=next((a for a in wanted if a not in held_addresses), None),
            mode=RULE_NEVER,
            reason=REASON_PERSONAL,
            set_by=by[:16],
            history=[_rule_entry("state", None, RULE_ACTIVE, by=by, now=now)],
        )
        if not await _insert(session, head):
            raise _Raced()
    # Whatever else already names this chat or an address is made never too,
    # and is tied to this marking: it now guards the marking's chat as well.
    for rule in existing:
        _tighten(rule, by=by, now=now)
        _rely_on(rule, head, by=by, now=now)
    covered = {r.address for r in existing if r.address}
    if head.address:
        covered.add(head.address)
    for address in wanted:
        if address in covered:
            continue
        snapshot = ReplyRule(
            subject=SUBJECT_ADDRESS,
            address=address,
            mode=RULE_NEVER,
            reason=REASON_PERSONAL,
            set_by=by[:16],
            history=[
                _rule_entry("state", None, RULE_ACTIVE, by=by, now=now, link=head.id)
            ],
        )
        if not await _insert(session, snapshot):
            other = await session.scalar(
                sa.select(ReplyRule)
                .where(ReplyRule.state == RULE_ACTIVE, ReplyRule.address == address)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if other is not None:
                _tighten(other, by=by, now=now)
                _rely_on(other, head, by=by, now=now)
    await session.flush()
    return Marked(rule_id=head.id, already=already)


async def mark_person(
    session: AsyncSession, person: Person, *, by: str, now: datetime
) -> Marked:
    """``/shaxsiy Ism`` or a 🔒 tap: the person and every address they are
    known at (their Telegram chat, a WhatsApp number)."""
    addresses = await people.known_addresses(session, person)
    telegram = [
        a for a in addresses if a.startswith(f"{chatkey.PREFIX[chatkey.TELEGRAM]}:")
    ]
    return await mark_personal(
        session,
        person_id=person.id,
        chat_key=telegram[0] if telegram else None,
        addresses=addresses,
        by=by,
        now=now,
    )


async def mark_chat(
    session: AsyncSession, monitor: ChatMonitor, *, by: str, now: datetime
) -> Marked:
    """``/shaxsiy`` naming a chat (a family group): the chat itself, and for
    a private chat the user's address too."""
    key = chatkey.telegram(monitor.tg_chat_id)
    private = monitor.chat_type == ChatType.private and monitor.tg_chat_id > 0
    return await mark_personal(
        session,
        person_id=None,
        chat_key=key,
        addresses=[key] if private else [],
        by=by,
        now=now,
    )


async def personal_rule(
    session: AsyncSession,
    *,
    person_id: int | None = None,
    chat_keys: Sequence[str] = (),
) -> ReplyRule | None:
    """The active ``never`` rule marking this person, else the marking that
    holds this chat, by its key or by an address snapshot of it (a chat
    marked by its title, a person forgotten with ``/unut``)."""
    if person_id is not None:
        rule: ReplyRule | None = await session.scalar(
            sa.select(ReplyRule).where(
                ReplyRule.state == RULE_ACTIVE,
                ReplyRule.mode == RULE_NEVER,
                ReplyRule.person_id == person_id,
            )
        )
        if rule is not None:
            return rule
    keys = [k for k in chat_keys if k]
    if not keys:
        return None
    found: ReplyRule | None = await session.scalar(
        sa.select(ReplyRule)
        .where(
            ReplyRule.state == RULE_ACTIVE,
            ReplyRule.mode == RULE_NEVER,
            sa.or_(ReplyRule.chat_key.in_(keys), ReplyRule.address.in_(keys)),
        )
        .order_by(ReplyRule.id)
        .limit(1)
    )
    return await _marking_of(session, found) if found is not None else None


async def _marking_of(session: AsyncSession, rule: ReplyRule) -> ReplyRule:
    """The marking a snapshot (or a chat rule another marking relies on) is
    shown through in ``/shaxsiylar``: unmarking that one is what opens the
    chat. A person's marking is its own."""
    seen = {rule.id}
    current = rule
    while current.person_id is None:
        heads = [link for link in links_of(current.history) if link not in seen]
        if not heads:
            return current
        nxt: ReplyRule | None = await session.scalar(
            sa.select(ReplyRule)
            .where(
                ReplyRule.id.in_(heads),
                ReplyRule.state == RULE_ACTIVE,
                ReplyRule.mode == RULE_NEVER,
            )
            .order_by(ReplyRule.id)
            .limit(1)
        )
        if nxt is None:
            return current
        seen.add(nxt.id)
        current = nxt
    return current


async def personal_rule_by_id(session: AsyncSession, rule_id: int) -> ReplyRule | None:
    """The rule a 🔓 button names, while it still keeps MIYA out."""
    rule = await session.get(ReplyRule, rule_id)
    if rule is None or rule.state != RULE_ACTIVE or rule.mode != RULE_NEVER:
        return None
    return rule


async def unmark_personal(
    session: AsyncSession, rule_id: int, *, by: str, now: datetime
) -> ReplyRule | None:
    """The owner's confirmed «✅ Ha, chiqar» tap: revoke the marking and its
    address snapshots, under a row lock. None when it is no longer an
    active ``never`` rule (a second tap, or already unmarked).

    A rule tied to the marking stays active when it is another person's
    marking or another active marking also relies on it. When another
    marking relies on the marking itself, its chat and address go on
    guarding that one as a new snapshot.
    """
    head: ReplyRule | None = await session.scalar(
        sa.select(ReplyRule)
        .where(ReplyRule.id == rule_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if head is None or head.state != RULE_ACTIVE or head.mode != RULE_NEVER:
        return None
    revoked = {head.id}
    _revoke(head, by=by, now=now)
    await session.flush()
    survivors = await _active_ids(session, links_of(head.history), revoked)
    candidates: dict[int, ReplyRule] = {}
    pending = [head.id]
    changed = True
    while changed:
        changed = False
        while pending:
            linked = await session.scalars(
                sa.select(ReplyRule)
                .where(
                    ReplyRule.state == RULE_ACTIVE,
                    ReplyRule.history.contains([{LINK_KEY: pending.pop()}]),
                )
                .order_by(ReplyRule.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            # Another person's marking is theirs to unmark.
            candidates.update(
                (rule.id, rule) for rule in linked if rule.person_id is None
            )
        # A rule another active marking relies on goes on guarding that one;
        # checked again after each round, as a rule revoked later in the
        # round can be the one it relied on.
        for rule in candidates.values():
            if rule.id in revoked:
                continue
            if await _active_ids(session, links_of(rule.history), revoked):
                continue
            _revoke(rule, by=by, now=now)
            revoked.add(rule.id)
            pending.append(rule.id)
            changed = True
    await session.flush()
    if survivors and (head.chat_key or head.address):
        await _carry_over(session, head, survivors, by=by, now=now)
    return head


async def _active_ids(
    session: AsyncSession, ids: Iterable[int], excluded: set[int]
) -> list[int]:
    """Which of ``ids`` are still active ``never`` markings."""
    wanted = [i for i in ids if i not in excluded]
    if not wanted:
        return []
    found = await session.scalars(
        sa.select(ReplyRule.id)
        .where(
            ReplyRule.id.in_(wanted),
            ReplyRule.state == RULE_ACTIVE,
            ReplyRule.mode == RULE_NEVER,
        )
        .order_by(ReplyRule.id)
    )
    return list(found)


async def _carry_over(
    session: AsyncSession,
    head: ReplyRule,
    survivors: Sequence[int],
    *,
    by: str,
    now: datetime,
) -> None:
    """The unmarked rule's chat and address, as a snapshot of the marking
    that still relies on them."""
    first, *rest = survivors
    history = [_rule_entry("state", None, RULE_ACTIVE, by=by, now=now, link=first)]
    history += [
        _rule_entry(LINKED_FIELD, None, other, by=by, now=now, link=other)
        for other in rest
    ]
    replacement = ReplyRule(
        subject=SUBJECT_CHAT if head.chat_key else SUBJECT_ADDRESS,
        chat_key=head.chat_key,
        address=head.address,
        mode=RULE_NEVER,
        reason=REASON_PERSONAL,
        set_by=by[:16],
        history=history,
    )
    if not await _insert(session, replacement):
        # Another active rule took the chat or the address in the meantime:
        # it guards them already.
        log.info(
            "personal snapshot not carried over",
            extra={"rule_id": head.id, "survivor_id": first},
        )


GroupState = Literal["on", "off", "personal"]


def _group_state(rule: ReplyRule | None) -> GroupState:
    if rule is None:
        return "off"
    if rule.mode == RULE_NEVER:
        return "personal"
    if rule.reason == REASON_GROUP:
        return "on"
    return "off"


async def _chat_rule(
    session: AsyncSession, chat_key: str, *, lock: bool
) -> ReplyRule | None:
    query = sa.select(ReplyRule).where(
        ReplyRule.state == RULE_ACTIVE, ReplyRule.chat_key == chat_key
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    rule: ReplyRule | None = await session.scalar(query)
    return rule


async def group_reply_state(session: AsyncSession, chat_key: str) -> GroupState:
    """``on`` when the owner switched AI replies on for this group."""
    return _group_state(await _chat_rule(session, chat_key, lock=False))


async def set_group_reply(
    session: AsyncSession, *, chat_key: str, enabled: bool, by: str, now: datetime
) -> GroupState:
    """``/guruhai``: switch AI replies in one group on (the owner's confirmed
    tap) or off (at once). Replies there are always drafts for the owner's
    tap and only to messages addressed to them. A personal group stays
    personal. Returns the state after the call."""
    rule = await _chat_rule(session, chat_key, lock=True)
    state = _group_state(rule)
    if state == "personal" or (state == "on") == enabled:
        return state
    if not enabled:
        if rule is not None:
            _revoke(rule, by=by, now=now)
            await session.flush()
        return "off"
    if rule is not None:
        # A chat rule of another reason: it becomes the group switch, as a
        # draft (an owner-set auto would be looser than group replies get).
        _append(rule, _rule_entry("reason", rule.reason, REASON_GROUP, by=by, now=now))
        rule.reason = REASON_GROUP
        if rule.mode != RULE_DRAFT:
            _append(rule, _rule_entry("mode", rule.mode, RULE_DRAFT, by=by, now=now))
            rule.mode = RULE_DRAFT
        await session.flush()
        return "on"
    switch = ReplyRule(
        subject=SUBJECT_CHAT,
        chat_key=chat_key,
        mode=RULE_DRAFT,
        reason=REASON_GROUP,
        set_by=by[:16],
        history=[_rule_entry("state", None, RULE_ACTIVE, by=by, now=now)],
    )
    if not await _insert(session, switch):
        return _group_state(await _chat_rule(session, chat_key, lock=True))
    return "on"


async def enabled_groups(session: AsyncSession) -> list[str]:
    """The chat keys of the groups whose replies the owner switched on."""
    keys = await session.scalars(
        sa.select(ReplyRule.chat_key)
        .where(
            ReplyRule.state == RULE_ACTIVE,
            ReplyRule.reason == REASON_GROUP,
            ReplyRule.mode.in_([RULE_DRAFT, RULE_AUTO]),
            ReplyRule.chat_key.isnot(None),
        )
        .order_by(ReplyRule.id)
    )
    return [key for key in keys if key]


async def enabled_group_titles(session: AsyncSession) -> list[str | None]:
    """The titles of the groups whose replies are switched on (`/guruhai`)."""
    keys = await enabled_groups(session)
    tg_ids = [tg for tg in (_tg_id(key) for key in keys) if tg is not None]
    titles: dict[int, str] = {}
    if tg_ids:
        rows = await session.execute(
            sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title).where(
                ChatMonitor.tg_chat_id.in_(tg_ids)
            )
        )
        titles = {int(tg): str(title) for tg, title in rows if title}
    found: list[str | None] = []
    for key in keys:
        tg = _tg_id(key)
        found.append(titles.get(tg) if tg is not None else None)
    return found


# --- what the owner sees: the list and the suggestions ---------------------------------


@dataclass(frozen=True, slots=True)
class Listed:
    """One personal marking, named for the owner (None: nothing left to name
    it by, as after ``/unut`` of a chat nobody else knows)."""

    rule_id: int
    name: str | None
    person_id: int | None = None


def _tg_id(key: str | None) -> int | None:
    if not key:
        return None
    try:
        return chatkey.tg_chat_id(key)
    except chatkey.BadChatKey:
        return None


async def describe(session: AsyncSession, rules: Sequence[ReplyRule]) -> list[Listed]:
    """Each rule with the person's name, else the chat's title."""
    person_ids = {r.person_id for r in rules if r.person_id is not None}
    names: dict[int, str] = {}
    if person_ids:
        rows = await session.execute(
            sa.select(Person.id, Person.display_name).where(Person.id.in_(person_ids))
        )
        names = {int(pid): str(name) for pid, name in rows}
    tg_ids = {
        tg
        for r in rules
        for tg in (_tg_id(r.chat_key), _tg_id(r.address))
        if tg is not None
    }
    titles: dict[int, str] = {}
    if tg_ids:
        titles = {
            tg: title
            for tg, title in await session.execute(
                sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title).where(
                    ChatMonitor.tg_chat_id.in_(tg_ids)
                )
            )
            if title
        }
    listed: list[Listed] = []
    for rule in rules:
        name = names.get(rule.person_id) if rule.person_id is not None else None
        for key in (rule.chat_key, rule.address):
            tg = _tg_id(key)
            if name is None and tg is not None:
                name = titles.get(tg)
        listed.append(Listed(rule_id=rule.id, name=name, person_id=rule.person_id))
    return listed


async def personal_chats(session: AsyncSession) -> list[Listed]:
    """``/shaxsiylar``: every active personal marking, oldest first. A
    person's marking always has its line; an address snapshot, or a chat
    rule another marking relies on, is shown through that marking."""
    rules = list(
        await session.scalars(
            sa.select(ReplyRule)
            .where(ReplyRule.state == RULE_ACTIVE, ReplyRule.mode == RULE_NEVER)
            .order_by(ReplyRule.id)
        )
    )
    active = {rule.id for rule in rules}
    links = {
        rule.id: [i for i in links_of(rule.history) if i in active] for rule in rules
    }
    heads = {
        rule.id for rule in rules if rule.person_id is not None or not links[rule.id]
    }
    # Shown through a listed marking, directly or through another snapshot.
    shown = set(heads)
    grew = True
    while grew:
        grew = False
        for rule in rules:
            if rule.id not in shown and any(i in shown for i in links[rule.id]):
                shown.add(rule.id)
                grew = True
    # Rules that only rely on each other still need a line of their own.
    listed = [rule for rule in rules if rule.id in heads or rule.id not in shown]
    return await describe(session, listed)


def _lexicon(raw: str) -> frozenset[str]:
    """Words written as people type them, folded like the text they meet."""
    return frozenset(
        token for word in raw.split() for token in normalise_for_search(word).split()
    )


# How the owner names family and close friends in their contacts: possessive
# and endearing forms only («Akam», «Onajon»), never the bare honorifics
# («aka», «opa») that clients get too.
KIN_NAMES: Final = _lexicon(
    """
    onam onajon onajonim oyim oyijon oyijonim ayam ayajon dadam dadajon dadajonim
    otam otajon akam akajon akamjon ukam ukajon ukajonim opam opajon opajonim
    singlim singiljon singiljonim xotinim xotinjon erim o'g'lim o'g'ilim qizim
    qizalog'im bolam bolajonim nevaram buvim buvijon bobom bobojon amakim tog'am
    xolam xolajon ammam jiyanim kelinim kuyovim qaynonam qaynotam do'stim dugonam
    jonim jonginam asalim azizim sevgilim
    мама мамочка мамуля папа папочка папуля братик братишка сестрёнка сестренка
    жена муж любимая любимый сынок доча дочка бабушка бабуля дедушка дедуля
    """
)
# What the owner wrote in a person's relationship field (/kim, set by hand).
RELATION_WORDS: Final = _lexicon(
    """
    ona ota aka uka opa singil xotin er turmush o'g'il qiz farzand buvi bobo amaki
    tog'a xola amma jiyan kuyov kelin qaynona qaynota qarindosh oila do'st dugona
    sinfdosh kursdosh qo'shni
    мать отец мама папа брат сестра жена муж сын дочь бабушка дедушка дядя тётя
    тетя друг подруга семья родственник родственница сосед
    """
)
# The owner's own informal register: «sen», endearments, Russian «ты».
INFORMAL_WORDS: Final = _lexicon(
    """
    sen senga seni sening senda sendan senam senchi qalaysan qalesan yaxshimisan
    bormisan kelyapsan kelasanmi qayerdasan qilyapsan jonim jonginam asalim azizim
    onajon dadajon akajon opajon ukajon bolajonim
    ты тебе тебя тобой твой твоя целую люблю обнимаю
    """
)
INFORMAL_EMOJI: Final = ("❤", "😘", "🥰", "😍", "💋", "🤗")
# Work talk: prices, payments, cargo, orders. A chat with much of it is a
# client's, however warm the tone.
BUSINESS_WORDS: Final = _lexicon(
    """
    narx narxi narxlar narxini tolov to'lov to'lovni to'ladim to'lab dollar dollor
    so'm som usd yuan kurs kg kilo kilogramm yuk yuki yukni yuklar gruz zakaz
    buyurtma dostavka yetkazib yetkazish sklad ombor konteyner fura tamojnya
    bojxona kargo cargo shartnoma dogovor chegirma skidka oplata predoplata avans
    faktura schet invoys invoice nakladnoy mijoz klient tovar mahsulot partiya
    цена оплата доллар сум юань курс груз заказ доставка склад контейнер фура
    таможня карго договор скидка предоплата аванс счёт счет накладная клиент товар
    партия
    """
)

SUGGEST_LOOKBACK: Final = timedelta(days=365)
# The most active people looked at, and the newest messages read per person.
SUGGEST_POOL: Final = 150
SUGGEST_TEXTS: Final = 40
SUGGEST_LIMIT: Final = 8
# Informal messages from the owner that make a suggestion on their own.
SUGGEST_MIN_INFORMAL: Final = 2
# A person whose messages are more than one in this many about work is not
# suggested («no business vocabulary»).
BUSINESS_ONE_IN: Final = 10
# A client code held is business by definition.
CODE_ACTIVE: Final = "active"


@dataclass(frozen=True, slots=True)
class Signals:
    """Why a person looks like family or a friend, from SQL rows only."""

    kin: bool = False
    informal: int = 0
    business: int = 0
    messages: int = 0

    @property
    def suggested(self) -> bool:
        if self.business * BUSINESS_ONE_IN > self.messages:
            return False
        return self.kin or self.informal >= SUGGEST_MIN_INFORMAL


def _tokens(text: str | None) -> frozenset[str]:
    return frozenset(normalise_for_search(text or "").split())


def personal_signals(
    *,
    names: Sequence[str],
    relationship: str | None,
    texts: Sequence[tuple[bool, str]],
) -> Signals:
    """Score one person: kinship in their contact name or the owner's
    relationship note, the owner's informal register towards them, and how
    much of the chat is work. ``texts`` are (written by the owner, text)."""
    name_tokens = (
        frozenset().union(*(_tokens(n) for n in names)) if names else frozenset()
    )
    kin = bool(name_tokens & KIN_NAMES) or bool(_tokens(relationship) & RELATION_WORDS)
    informal = business = 0
    for by_owner, text in texts:
        tokens = _tokens(text)
        if tokens & BUSINESS_WORDS or codes.find_client_codes(text):
            business += 1
        if by_owner and (
            tokens & INFORMAL_WORDS or any(mark in text for mark in INFORMAL_EMOJI)
        ):
            informal += 1
    return Signals(kin=kin, informal=informal, business=business, messages=len(texts))


@dataclass(frozen=True, slots=True)
class Suggestion:
    person_id: int
    name: str
    signals: Signals = field(default_factory=Signals)


async def suggest_personal(
    session: AsyncSession, *, now: datetime, limit: int = SUGGEST_LIMIT
) -> list[Suggestion]:
    """Likely family and friends to offer for marking (``/shaxsiylar``).

    Only offered, never marked: the owner taps. Left out: anyone with an
    active rule, anyone whose Telegram chat a personal rule already holds (a
    chat marked by its title, a person forgotten with ``/unut`` who wrote
    again), anyone the owner has unmarked, anyone holding a client code,
    the owner, bots.
    """
    since = now - SUGGEST_LOOKBACK
    unmarked = sa.and_(
        ReplyRule.state == RULE_REVOKED, ReplyRule.reason == REASON_PERSONAL
    )
    ruled = sa.select(ReplyRule.person_id).where(
        ReplyRule.person_id.isnot(None),
        sa.or_(ReplyRule.state == RULE_ACTIVE, unmarked),
    )
    held_or_unmarked = sa.or_(
        sa.and_(ReplyRule.state == RULE_ACTIVE, ReplyRule.mode == RULE_NEVER), unmarked
    )
    tg_address = sa.func.concat(
        f"{chatkey.PREFIX[chatkey.TELEGRAM]}:", Person.telegram_id
    )
    chat_held = (
        sa.exists()
        .where(
            held_or_unmarked,
            sa.or_(ReplyRule.chat_key == tg_address, ReplyRule.address == tg_address),
        )
        .correlate(Person)
    )
    coded = sa.select(ClientCode.person_id).where(ClientCode.status == CODE_ACTIVE)
    counts = (
        sa.select(Interaction.person_id, sa.func.count().label("n"))
        .where(
            Interaction.person_id.isnot(None),
            Interaction.tg_chat_id > 0,
            Interaction.occurred_at >= since,
        )
        .group_by(Interaction.person_id)
        .subquery()
    )
    owner_id = settings.owner_telegram_id
    query = (
        sa.select(Person, sa.func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.person_id == Person.id)
        .where(
            Person.id.not_in(ruled),
            Person.id.not_in(coded),
            sa.or_(Person.telegram_id.is_(None), ~chat_held),
            sa.or_(counts.c.n > 0, Person.relationship_.isnot(None)),
        )
        .order_by(sa.func.coalesce(counts.c.n, 0).desc(), Person.id)
        .limit(SUGGEST_POOL)
    )
    if owner_id is not None:
        query = query.where(
            sa.or_(Person.telegram_id.is_(None), Person.telegram_id != owner_id)
        )
    pool = [
        (person, int(n))
        for person, n in (await session.execute(query)).all()
        if not looks_like_bot(person.telegram_username)
        and not people.is_owner_alias(person.display_name)
    ]
    if not pool:
        return []
    ranked = (
        sa.select(
            Interaction.person_id,
            Interaction.direction,
            sa.func.coalesce(Interaction.raw_text, Interaction.transcript).label("text"),
            sa.func.row_number()
            .over(
                partition_by=Interaction.person_id,
                order_by=(Interaction.occurred_at.desc(), Interaction.id.desc()),
            )
            .label("rn"),
        )
        .where(
            Interaction.person_id.in_([person.id for person, _ in pool]),
            Interaction.tg_chat_id > 0,
            Interaction.occurred_at >= since,
        )
        .subquery()
    )
    texts: dict[int, list[tuple[bool, str]]] = {}
    for person_id, direction, text in await session.execute(
        sa.select(ranked.c.person_id, ranked.c.direction, ranked.c.text).where(
            ranked.c.rn <= SUGGEST_TEXTS, ranked.c.text.isnot(None)
        )
    ):
        texts.setdefault(int(person_id), []).append((direction == Direction.out, text))
    found: list[tuple[Suggestion, int]] = []
    for person, n in pool:
        try:
            signals = personal_signals(
                names=[person.display_name, *(person.aliases or [])],
                relationship=person.relationship_,
                texts=texts.get(person.id, []),
            )
        except Exception as exc:
            # One unreadable row never hides the other suggestions (rule 25).
            log.error(
                "personal suggestion skipped",
                extra={"person_id": person.id, "error": type(exc).__name__},
            )
            continue
        if signals.suggested:
            found.append((Suggestion(person.id, person.display_name, signals), n))
    found.sort(
        key=lambda pair: (not pair[0].signals.kin, -pair[0].signals.informal, -pair[1])
    )
    return [suggestion for suggestion, _ in found[:limit]]


__all__ = [
    "BUSINESS_WORDS",
    "DEFAULT_MODE",
    "EXCLUDED_BROADCAST",
    "GLOBAL",
    "GROUP_NOT_ADDRESSED",
    "GROUP_OFF",
    "INFORMAL_WORDS",
    "KIN_NAMES",
    "LINKED_FIELD",
    "LINK_KEY",
    "MODES",
    "MODE_RANK",
    "PAUSE_OWNER",
    "PERSONAL_CHAT",
    "REASON_GROUP",
    "REASON_PERSONAL",
    "RELATION_WORDS",
    "RULE_NEVER",
    "TELEGRAM_SERVICE_ID",
    "ChatFacts",
    "GroupState",
    "Listed",
    "Marked",
    "Mode",
    "Policies",
    "PolicySnapshot",
    "RuleSnapshot",
    "Signals",
    "Stop",
    "Suggestion",
    "ai_refusal",
    "as_mode",
    "ceiling",
    "channel_intent_scope",
    "channel_scope",
    "chat_kind",
    "describe",
    "effective_mode",
    "enabled_group_titles",
    "enabled_groups",
    "group_enabled",
    "group_reply_state",
    "hard_exclusion",
    "intent_scope",
    "is_broadcast",
    "link_of",
    "links_of",
    "load",
    "load_rules",
    "looks_like_bot",
    "lower_mode",
    "mark_chat",
    "mark_person",
    "mark_personal",
    "never_rule",
    "pause",
    "personal_chats",
    "personal_rule",
    "personal_rule_by_id",
    "personal_signals",
    "resume",
    "rule_mode",
    "set_group_reply",
    "set_mode",
    "snapshot",
    "stop_for",
    "stored_mode",
    "suggest_personal",
    "trip_breaker",
    "unmark_personal",
]
