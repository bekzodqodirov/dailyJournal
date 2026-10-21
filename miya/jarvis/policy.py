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
  bots, channels, groups the owner has not enabled), then the chat rule, the
  person rule, the channel default and the mode above. Restricting is
  immediate; loosening is the owner's confirmed tap (JV-12).

The resolution functions are pure; the loaders and writers take a session
and never commit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import JarvisPolicy, ReplyRule
from miya.jarvis.chatkey import Channel

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
REASON_GROUP: Final = "group"

# Hard exclusions: never written to by anyone through the outbox.
TELEGRAM_SERVICE_ID: Final = 777000
EXCLUDED_SAVED_MESSAGES: Final = "saved_messages"
EXCLUDED_SERVICE_CHAT: Final = "service_chat"
EXCLUDED_BOT: Final = "bot_chat"
EXCLUDED_CHANNEL: Final = "channel_chat"
EXCLUDED_OWNER: Final = "owner_alias"


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
    """Every active rule that names this chat, its person or an address."""
    targets = [ReplyRule.chat_key == chat_key]
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
    if facts.is_owner:
        return EXCLUDED_OWNER
    return None


def looks_like_bot(username: str | None) -> bool:
    """Telegram reserves the «bot» suffix for bot accounts."""
    return bool(username) and str(username).casefold().endswith("bot")


__all__ = [
    "DEFAULT_MODE",
    "GLOBAL",
    "MODES",
    "MODE_RANK",
    "PAUSE_OWNER",
    "TELEGRAM_SERVICE_ID",
    "ChatFacts",
    "Mode",
    "Policies",
    "PolicySnapshot",
    "RuleSnapshot",
    "Stop",
    "as_mode",
    "ceiling",
    "channel_intent_scope",
    "channel_scope",
    "effective_mode",
    "group_enabled",
    "hard_exclusion",
    "intent_scope",
    "load",
    "load_rules",
    "looks_like_bot",
    "lower_mode",
    "never_rule",
    "pause",
    "resume",
    "rule_mode",
    "set_mode",
    "snapshot",
    "stop_for",
    "stored_mode",
    "trip_breaker",
]
