"""What ``/jarvis`` shows: the mode, today's counts, the channels, the audit
trail (docs/jarvis-plan.md JV-11 behaviour 4, §5.9).

Read-only. Every number is counted in SQL (``caps.day_counts``); the audit
trail is the ``history`` of the Jarvis rows themselves, newest first.

The observation numbers (JV-16, ``shadow_counts``) say what the responder
would have done today in ``shadow`` mode: the replies it would have prepared,
how many of them it would have sent to the owner instead, and per intent how
many drafts strayed from the approved shapes (``unapproved_statement``), so
the owner sees how often the model would need their eye. ``shadow_drafts``
is ``/qoralamalar``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import (
    Escalation,
    Interaction,
    JarvisPolicy,
    OutboxMessage,
    Person,
    ReplyRule,
)
from miya.jarvis import caps, chatkey, gate, outbox, policy
from miya.jarvis.policy import Mode
from miya.services.reminders import day_start

ChannelState = Literal["working", "limited", "paused", "not_connected"]

# How many recent rows of each table the audit trail looks through.
HISTORY_SCAN: Final = 60
# How many observation drafts `/qoralamalar` lists.
SHADOW_LIST: Final = 10


@dataclass(frozen=True, slots=True)
class ShadowCounts:
    """Today in ``shadow`` mode (JV-16): replies the responder would have
    prepared (drafts and escalations), how many of those it would have sent
    to the owner, and the drafts per intent with a free-form statement."""

    prepared: int = 0
    escalated: int = 0
    unapproved: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Status:
    mode: Mode
    paused: bool
    counts: caps.DayCounts
    limit_used: int
    limit_cap: int
    channels: tuple[tuple[chatkey.Channel, ChannelState], ...]
    shadow: ShadowCounts | None = None


def global_mode(policies: policy.Policies) -> Mode:
    """The global mode under the env ceiling (``off`` when Jarvis is off)."""
    stored = policies.get(policy.GLOBAL)
    mode = stored.mode if stored is not None and stored.mode else policy.DEFAULT_MODE
    return policy.lower_mode(mode, policy.ceiling())


def channel_state(
    policies: policy.Policies, channel: chatkey.Channel, now: datetime
) -> ChannelState:
    scoped = policies.get(policy.channel_scope(channel))
    if scoped is not None and scoped.tripped(now):
        return "limited"
    if scoped is not None and scoped.paused(now):
        return "paused"
    if not outbox.channel_send_enabled(channel):
        return "not_connected"
    return "working"


async def gather(session: AsyncSession, now: datetime) -> Status:
    policies = await policy.load(session)
    mode = global_mode(policies)
    counts = await caps.day_counts(session, now)
    if mode == "auto":
        used, cap = counts.auto_sent, settings.jarvis_auto_daily_cap
    else:
        used, cap = counts.draft_cards, settings.jarvis_draft_daily_cap
    stored = policies.get(policy.GLOBAL)
    shadow = await shadow_counts(session, now)
    return Status(
        mode=mode,
        paused=stored is not None and stored.paused(now),
        counts=counts,
        limit_used=used,
        limit_cap=cap,
        channels=tuple(
            (channel, channel_state(policies, channel, now))
            for channel in chatkey.CHANNELS
        ),
        shadow=shadow if mode == "shadow" or shadow.prepared else None,
    )


def _shadow_escalation() -> sa.ColumnElement[bool]:
    """An escalation the responder opened in ``shadow`` mode (its creation
    entry says so): never alerted, only counted."""
    return Escalation.history.op("@>")(
        sa.cast(sa.literal([{"field": "state", "mode": "shadow"}], JSONB), JSONB)
    )


async def shadow_counts(session: AsyncSession, now: datetime) -> ShadowCounts:
    start = day_start(now)
    shadow_reply = sa.and_(
        OutboxMessage.created_at >= start,
        OutboxMessage.state == outbox.SHADOW,
        OutboxMessage.origin == "auto_reply",
    )
    drafts = await session.scalar(
        sa.select(sa.func.count()).select_from(OutboxMessage).where(shadow_reply)
    )
    escalated = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Escalation)
        .where(Escalation.created_at >= start, _shadow_escalation())
    )
    strayed = OutboxMessage.guard["flags"].op("@>")(
        sa.cast(sa.literal([gate.UNAPPROVED_STATEMENT], JSONB), JSONB)
    )
    per_intent = await session.execute(
        sa.select(OutboxMessage.intent, sa.func.count())
        .where(shadow_reply, strayed)
        .group_by(OutboxMessage.intent)
        .order_by(OutboxMessage.intent)
    )
    escalations = int(escalated or 0)
    return ShadowCounts(
        prepared=int(drafts or 0) + escalations,
        escalated=escalations,
        unapproved=tuple((str(k or "other"), int(n)) for k, n in per_intent),
    )


@dataclass(frozen=True, slots=True)
class ShadowDraft:
    """One observation draft as ``/qoralamalar`` shows it (plain values:
    replies.py never imports miya.jarvis)."""

    id: int
    name: str | None
    client_text: str
    body: str
    intent: str | None
    origin: str


async def shadow_drafts(
    session: AsyncSession, *, limit: int = SHADOW_LIST
) -> list[ShadowDraft]:
    """The newest ``shadow`` rows, newest first: who wrote, what they wrote,
    what MIYA would have answered."""
    rows = await session.execute(
        sa.select(
            OutboxMessage.id,
            Person.display_name,
            Interaction.raw_text,
            Interaction.transcript,
            OutboxMessage.body,
            OutboxMessage.intent,
            OutboxMessage.origin,
        )
        .select_from(OutboxMessage)
        .outerjoin(Person, Person.id == OutboxMessage.person_id)
        .outerjoin(Interaction, Interaction.id == OutboxMessage.trigger_interaction_id)
        .where(OutboxMessage.state == outbox.SHADOW)
        .order_by(OutboxMessage.created_at.desc(), OutboxMessage.id.desc())
        .limit(limit)
    )
    return [
        ShadowDraft(
            id=int(row_id),
            name=name,
            client_text=(raw or transcript or "").strip(),
            body=body,
            intent=kind,
            origin=origin,
        )
        for row_id, name, raw, transcript, body, kind, origin in rows
    ]


# Which table an audit entry comes from; replies.py renders the reference
# (j12, e3, a rule, Jarvis or a channel) in the owner's words.
RefKind = Literal["outbox", "escalation", "policy", "rule"]


@dataclass(frozen=True, slots=True)
class Action:
    """One audit entry: which row, what changed, by whom. ``ref`` is the row
    id, or the scope for a policy entry."""

    at: datetime
    kind: RefKind
    ref: str
    field: str
    old: str | None
    new: str | None
    by: str | None
    code: str | None = None


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _actions(kind: RefKind, ref: object, history: Iterable[Any]) -> list[Action]:
    found: list[Action] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        try:
            at = datetime.fromisoformat(str(entry.get("at")))
        except ValueError:
            continue
        found.append(
            Action(
                at=at,
                kind=kind,
                ref=str(ref),
                field=str(entry.get("field") or ""),
                old=_text(entry.get("old")),
                new=_text(entry.get("new")),
                by=_text(entry.get("by")),
                code=_text(entry.get("code")),
            )
        )
    return found


async def recent_actions(session: AsyncSession, *, limit: int = 20) -> list[Action]:
    """The last ``limit`` actions across the outbox, escalations, the policy
    and the reply rules (``/jarvis tarix``)."""
    found: list[Action] = []
    for row_id, history in await session.execute(
        sa.select(OutboxMessage.id, OutboxMessage.history)
        .order_by(OutboxMessage.updated_at.desc(), OutboxMessage.id.desc())
        .limit(HISTORY_SCAN)
    ):
        found += _actions("outbox", row_id, history or [])
    for row_id, history in await session.execute(
        sa.select(Escalation.id, Escalation.history)
        .order_by(Escalation.updated_at.desc(), Escalation.id.desc())
        .limit(HISTORY_SCAN)
    ):
        found += _actions("escalation", row_id, history or [])
    for scope, history in await session.execute(
        sa.select(JarvisPolicy.scope, JarvisPolicy.history)
    ):
        found += _actions("policy", scope, history or [])
    for row_id, history in await session.execute(
        sa.select(ReplyRule.id, ReplyRule.history)
        .order_by(ReplyRule.updated_at.desc(), ReplyRule.id.desc())
        .limit(HISTORY_SCAN)
    ):
        found += _actions("rule", row_id, history or [])
    found.sort(key=lambda action: action.at, reverse=True)
    return found[:limit]


__all__ = [
    "Action",
    "ChannelState",
    "RefKind",
    "ShadowCounts",
    "ShadowDraft",
    "Status",
    "channel_state",
    "gather",
    "global_mode",
    "recent_actions",
    "shadow_counts",
    "shadow_drafts",
]
