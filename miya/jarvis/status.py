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

``health_facts`` is Jarvis's share of ``/holat`` and of the health sweep
(JV-21): the same counts, the spend against JARVIS_DAILY_COST_CAP_USD, each
channel's switch, pause and breaker, and the send queue: rows waiting for an
adapter, rows no adapter took although their channel should be sending, and
claimed rows whose lease ran out without a reconcile.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from miya.jarvis import caps, chatkey, costs, gate, outbox, policy
from miya.jarvis.policy import Mode
from miya.services import health
from miya.services.reminders import day_start

ChannelState = Literal["working", "limited", "paused", "not_connected"]

# How many recent rows of each table the audit trail looks through.
HISTORY_SCAN: Final = 60
# How many observation drafts `/qoralamalar` lists.
SHADOW_LIST: Final = 10
# An approved row due this long ago on a channel that should be sending is
# stuck: a working sender claims every few seconds, and a row the gate holds
# gets a later next_attempt_at each time it looks (JV-21).
STUCK_AFTER: Final = timedelta(minutes=10)
# A claimed row whose lease ran out this long ago was never reconciled: the
# jarvis_outbox job looks every minute.
LEASE_STUCK_AFTER: Final = timedelta(minutes=10)
# How far back a send in doubt ('unknown') is still shown.
UNKNOWN_LOOKBACK: Final = timedelta(hours=24)


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


def _ready_moment() -> sa.ColumnElement[datetime]:
    """When an approved row became due: the latest of its approval (or
    creation), its scheduled time and the gate's next look (GREATEST skips
    NULLs)."""
    return sa.func.greatest(
        OutboxMessage.next_attempt_at,
        OutboxMessage.not_before,
        sa.func.coalesce(OutboxMessage.approved_at, OutboxMessage.created_at),
    )


def _channel_send(
    policies: policy.Policies, channel: chatkey.Channel, now: datetime
) -> health.ChannelSend:
    scoped = policies.get(policy.channel_scope(channel))
    stored = policies.get(policy.GLOBAL)
    tripped = scoped is not None and scoped.tripped(now)
    return health.ChannelSend(
        channel=channel,
        send_enabled=outbox.channel_send_enabled(channel),
        paused=(stored is not None and stored.paused(now))
        or (scoped is not None and scoped.paused(now)),
        breaker_until=scoped.breaker_until if scoped is not None and tripped else None,
        breaker_reason=scoped.breaker_reason if scoped is not None and tripped else None,
    )


async def _backlog_stuck(
    session: AsyncSession, channel: str, cutoff: datetime
) -> tuple[int, datetime | None]:
    ready = _ready_moment()
    count, oldest = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.min(ready)).where(
                OutboxMessage.channel == channel,
                OutboxMessage.state == outbox.APPROVED,
                ready <= cutoff,
            )
        )
    ).one()
    return int(count or 0), oldest


async def health_facts(
    session: AsyncSession,
    now: datetime,
    *,
    sender_paused_until: datetime | None = None,
) -> health.JarvisHealth:
    """Jarvis's share of the status (JV-21), counted in SQL.

    A channel is looked at for a stuck queue only while it should be
    sending: its switch on, no pause and no breaker in force (the breaker is
    its own problem, a pause the owner's choice). The Telegram sender's own
    pause after a flood wait (``sender_paused_until``, from the userbot beat)
    counts as a wait, not as stuck, until STUCK_AFTER past its end. The
    overdue rows of a channel not looked at are counted as held instead, so
    a stuck queue is not taken as recovered while they still wait.
    """
    policies = await policy.load(session)
    mode = global_mode(policies)
    counts = await caps.day_counts(session, now)
    if mode == "auto":
        used, daily_cap = counts.auto_sent, settings.jarvis_auto_daily_cap
    else:
        used, daily_cap = counts.draft_cards, settings.jarvis_draft_daily_cap
    stored = policies.get(policy.GLOBAL)
    spend = await costs.spend_today(session, now)
    cap = settings.jarvis_daily_cost_cap_usd
    queued = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OutboxMessage)
        .where(OutboxMessage.state == outbox.APPROVED)
    )
    channels = tuple(_channel_send(policies, c, now) for c in chatkey.CHANNELS)
    cutoff = now - STUCK_AFTER
    backlog = held = 0
    since: list[datetime] = []
    for channel in channels:
        count, oldest = await _backlog_stuck(session, channel.channel, cutoff)
        if not channel.send_enabled or channel.paused or channel.limited:
            held += count
            continue
        waited_from: datetime | None = None
        if channel.channel == chatkey.TELEGRAM and sender_paused_until is not None:
            if sender_paused_until > cutoff:
                held += count
                continue
            waited_from = sender_paused_until
        if count and oldest is not None:
            backlog += count
            since.append(max(oldest, waited_from) if waited_from else oldest)
    claimed_at = OutboxMessage.lease_until - sa.literal(caps.SEND_LEASE, sa.Interval())
    leases, lease_since = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.min(claimed_at)).where(
                OutboxMessage.state == outbox.SENDING,
                OutboxMessage.lease_until <= now - LEASE_STUCK_AFTER,
            )
        )
    ).one()
    if leases and lease_since is not None:
        since.append(lease_since)
    unknown = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.state == outbox.UNKNOWN,
            OutboxMessage.updated_at >= now - UNKNOWN_LOOKBACK,
        )
    )
    return health.JarvisHealth(
        mode=mode,
        paused=stored is not None and stored.paused(now),
        drafts=counts.drafts,
        sent=counts.sent,
        escalations=counts.escalations,
        daily_used=used,
        daily_cap=daily_cap,
        queued=int(queued or 0),
        backlog_stuck=backlog,
        backlog_held=held,
        leases_stuck=int(leases or 0),
        stuck_since=min(since) if since else None,
        unknown=int(unknown or 0),
        cost_usd=spend.usd,
        cost_cap_usd=cap,
        cost_unpriced=spend.unpriced,
        cost_capped=costs.cap_reached(spend, cap),
        channels=channels,
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
    "LEASE_STUCK_AFTER",
    "STUCK_AFTER",
    "UNKNOWN_LOOKBACK",
    "Action",
    "ChannelState",
    "RefKind",
    "ShadowCounts",
    "ShadowDraft",
    "Status",
    "channel_state",
    "gather",
    "global_mode",
    "health_facts",
    "recent_actions",
    "shadow_counts",
    "shadow_drafts",
]
