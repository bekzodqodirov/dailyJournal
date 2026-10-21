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

``day_report`` is Jarvis's section of the 09:00 brief (yesterday) and of the
20:00 recap (today, JV-22): the replies sent (on their own or on the owner's
tap), the escalations the owner was shown and how many of them are still
open, the sends the owner commanded, the sends in doubt, the spend, and per
intent how the drafts fared. Every figure is SQL over ``outbox_messages``,
``escalations`` and ``usage_log`` in the report's window.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from miya.config import settings
from miya.db.models import (
    ESCALATION_OPEN_STATES,
    ChatMonitor,
    Escalation,
    Interaction,
    JarvisPolicy,
    OutboxMessage,
    Person,
    ReplyRule,
)
from miya.jarvis import caps, cards, chatkey, costs, gate, outbox, policy
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
    per_intent = await session.execute(
        sa.select(OutboxMessage.intent, sa.func.count())
        .where(shadow_reply, _strayed())
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
class IntentQuality:
    """One intent's AI drafts in a report's window (JV-22): how many the
    responder made (drafts and observation drafts alike), how many the owner
    approved with the AI's words unchanged, how many they rewrote, and how
    many strayed from the approved shapes (``unapproved_statement``: never
    sent on their own). A rewritten draft is judged by the owner's words
    from then on, so its AI text is not counted as strayed any more."""

    intent: str
    drafts: int
    unchanged: int = 0
    edited: int = 0
    unapproved: int = 0


@dataclass(frozen=True, slots=True)
class DayReport:
    """Jarvis in one report's window ``[start, end)`` (JV-22), plain values
    (the renderers never import miya.jarvis).

    ``sent`` are the replies to clients that went out (``auto`` of them on
    their own, ``approved`` on the owner's tap); ``escalations`` the ones the
    owner was shown, ``escalations_open`` of them still open now, and
    ``shadow_escalations`` the ones observation mode only counted;
    ``commands`` the sends the owner commanded; ``unknown`` the sends in
    doubt (claimed, never confirmed)."""

    sent: int = 0
    auto: int = 0
    approved: int = 0
    escalations: int = 0
    escalations_open: int = 0
    shadow_escalations: int = 0
    commands: int = 0
    unknown: int = 0
    cost_usd: Decimal = Decimal("0")
    cost_cap_usd: Decimal = Decimal("0")
    cost_unpriced: int = 0
    quality: tuple[IntentQuality, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.sent
            or self.escalations
            or self.shadow_escalations
            or self.commands
            or self.unknown
            or self.quality
            or self.cost_usd
            or self.cost_unpriced
        )


def _between(
    column: sa.ColumnElement[Any] | InstrumentedAttribute[Any],
    start: datetime,
    end: datetime,
) -> sa.ColumnElement[bool]:
    return sa.and_(column >= start, column < end)


def _strayed() -> sa.ColumnElement[bool]:
    """The bound verdict carries the ``unapproved_statement`` flag."""
    return OutboxMessage.guard["flags"].op("@>")(
        sa.cast(sa.literal([gate.UNAPPROVED_STATEMENT], JSONB), JSONB)
    )


async def day_report(session: AsyncSession, start: datetime, end: datetime) -> DayReport:
    """Jarvis's activity in ``[start, end)``, counted in SQL.

    A reply is counted when it was sent in the window (``sent_at``), a send
    in doubt by the moment it was claimed, an escalation and a draft by when
    they were made; ``escalations_open`` is their state now."""
    is_command = OutboxMessage.origin == outbox.OWNER_COMMAND_ORIGIN
    sent, auto, commands = (
        await session.execute(
            sa.select(
                sa.func.count().filter(~is_command),
                sa.func.count().filter(
                    ~is_command, OutboxMessage.approved_by == caps.AUTO_APPROVER
                ),
                sa.func.count().filter(is_command),
            ).where(
                OutboxMessage.state == outbox.SENT,
                _between(OutboxMessage.sent_at, start, end),
            )
        )
    ).one()
    unknown = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.state == outbox.UNKNOWN,
            _between(caps.wire_moment(), start, end),
        )
    )
    shown = cards.shown_escalation()
    escalations, still_open, observed = (
        await session.execute(
            sa.select(
                sa.func.count().filter(shown),
                sa.func.count().filter(
                    shown, Escalation.state.in_(ESCALATION_OPEN_STATES)
                ),
                sa.func.count().filter(_shadow_escalation()),
            ).where(_between(Escalation.created_at, start, end))
        )
    ).one()
    # Every AI row keeps its words in ``ai_body`` from the start; a rewrite
    # is told by the row turning the owner's (``outbox.owner_text``).
    edited = OutboxMessage.authored_by == gate.OWNER
    approved_unchanged = sa.and_(
        OutboxMessage.approved_by.is_not(None),
        OutboxMessage.approved_by != caps.AUTO_APPROVER,
        OutboxMessage.authored_by == gate.AI,
    )
    per_intent = await session.execute(
        sa.select(
            OutboxMessage.intent,
            sa.func.count(),
            sa.func.count().filter(approved_unchanged),
            sa.func.count().filter(edited),
            sa.func.count().filter(OutboxMessage.authored_by == gate.AI, _strayed()),
        )
        .where(
            OutboxMessage.origin == cards.DRAFT_ORIGIN,
            _between(OutboxMessage.created_at, start, end),
            sa.or_(caps.became(outbox.DRAFT), caps.became(outbox.SHADOW)),
        )
        .group_by(OutboxMessage.intent)
        .order_by(sa.func.count().desc(), OutboxMessage.intent)
    )
    spend = await costs.spend_between(session, start, end)
    sent_n = int(sent or 0)
    auto_n = int(auto or 0)
    return DayReport(
        sent=sent_n,
        auto=auto_n,
        approved=sent_n - auto_n,
        escalations=int(escalations or 0),
        escalations_open=int(still_open or 0),
        shadow_escalations=int(observed or 0),
        commands=int(commands or 0),
        unknown=int(unknown or 0),
        cost_usd=spend.usd,
        cost_cap_usd=settings.jarvis_daily_cost_cap_usd,
        cost_unpriced=spend.unpriced,
        quality=tuple(
            IntentQuality(
                intent=str(kind or "other"),
                drafts=int(n),
                unchanged=int(k or 0),
                edited=int(r or 0),
                unapproved=int(u or 0),
            )
            for kind, n, k, r, u in per_intent
        ),
    )


@dataclass(frozen=True, slots=True)
class ShadowDraft:
    """One draft as ``/qoralamalar`` shows it, an observation draft or one
    waiting for the owner's tap (plain values: replies.py never imports
    miya.jarvis)."""

    id: int
    name: str | None
    client_text: str
    body: str
    intent: str | None
    origin: str


async def _drafts(
    session: AsyncSession, where: sa.ColumnElement[bool], limit: int
) -> list[ShadowDraft]:
    rows = await session.execute(
        sa.select(
            OutboxMessage.id,
            sa.func.coalesce(Person.display_name, ChatMonitor.title),
            Interaction.raw_text,
            Interaction.transcript,
            OutboxMessage.body,
            OutboxMessage.intent,
            OutboxMessage.origin,
        )
        .select_from(OutboxMessage)
        .outerjoin(Person, Person.id == OutboxMessage.person_id)
        .outerjoin(ChatMonitor, ChatMonitor.tg_chat_id == OutboxMessage.tg_chat_id)
        .outerjoin(Interaction, Interaction.id == OutboxMessage.trigger_interaction_id)
        .where(where)
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


async def shadow_drafts(
    session: AsyncSession, *, limit: int = SHADOW_LIST
) -> list[ShadowDraft]:
    """The newest ``shadow`` rows, newest first: who wrote, what they wrote,
    what MIYA would have answered."""
    return await _drafts(session, OutboxMessage.state == outbox.SHADOW, limit)


async def waiting_drafts(
    session: AsyncSession, *, now: datetime, limit: int = SHADOW_LIST
) -> list[ShadowDraft]:
    """The drafts waiting for the owner's tap now, newest first (JV-22: the
    morning brief counts them and points here). Those held over the daily
    cap have no card, so this list is where the owner finds them."""
    return await _drafts(session, cards.waiting_draft(now), limit)


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
    "DayReport",
    "IntentQuality",
    "RefKind",
    "ShadowCounts",
    "ShadowDraft",
    "Status",
    "channel_state",
    "day_report",
    "gather",
    "global_mode",
    "health_facts",
    "recent_actions",
    "shadow_counts",
    "shadow_drafts",
    "waiting_drafts",
]
