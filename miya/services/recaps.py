"""What a day held, from SQL alone — the recaps' only data source (WP-51).

Every figure and count a recap shows comes from here; the prose writer
(WP-52) phrases, it never counts. Windows are half-open ``[start, end)``.
Member messages are told from window rows by metadata (WP-48). Nothing in
this module builds a model client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from miya.db.enums import ChatType, Direction, InteractionSource, WindowStatus
from miya.db.models import (
    ChatMonitor,
    Claim,
    ConversationWindow,
    Debt,
    Interaction,
    Person,
    Promise,
    Transaction,
)
from miya.services import loops, queries
from miya.services.queries import (
    ACTIVE_TXN,
    CheckableTxn,
    MoneyDay,
    RepaymentLine,
    is_member_message,
)

QUESTIONS_SCANNED = 200
QUESTIONS_PER_PERSON = 2
RAW_LINES = 15


@dataclass(slots=True)
class AskedQuestion:
    interaction_id: int
    text: str
    asked_at: datetime
    answered: bool
    chat_title: str | None


@dataclass(slots=True)
class PersonDay:
    person: Person
    messages_in: int = 0
    messages_out: int = 0
    group_mentions: int = 0
    calls: int = 0
    call_seconds: int = 0
    missed: int = 0
    questions: list[AskedQuestion] = field(default_factory=list)
    new_debt_ids: list[int] = field(default_factory=list)
    new_promise_ids: list[int] = field(default_factory=list)
    repayment_debt_ids: list[int] = field(default_factory=list)
    claim_ids: list[int] = field(default_factory=list)
    transaction_ids: list[int] = field(default_factory=list)
    content_ids: list[int] = field(default_factory=list)
    last_at: datetime | None = None
    score: int = 0

    @property
    def key(self) -> str:
        return f"p:{self.person.id}"

    def seen(self, moment: datetime | None) -> None:
        if moment is not None and (self.last_at is None or moment > self.last_at):
            self.last_at = moment


@dataclass(slots=True)
class GroupDay:
    tg_chat_id: int
    title: str
    messages: int = 0
    to_me: int = 0
    summary_ids: list[int] = field(default_factory=list)
    raw_ids: list[int] = field(default_factory=list)
    last_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"g:{self.tg_chat_id}"


@dataclass(slots=True)
class CallStats:
    total: int = 0
    incoming: int = 0
    outgoing: int = 0
    missed: int = 0
    rejected: int = 0
    seconds: int = 0
    unknown: int = 0


@dataclass(slots=True)
class DayActivity:
    start: datetime
    end: datetime
    now: datetime
    money: MoneyDay
    new_debts: list[Debt] = field(default_factory=list)
    new_promises: list[Promise] = field(default_factory=list)
    completed: Any = None
    people: list[PersonDay] = field(default_factory=list)
    groups: list[GroupDay] = field(default_factory=list)
    calls: CallStats = field(default_factory=CallStats)
    to_me: list[Interaction] = field(default_factory=list)
    chat_titles: dict[int, str] = field(default_factory=dict)
    claims_new: list[Claim] = field(default_factory=list)
    questions_open: list = field(default_factory=list)
    missed_open: list = field(default_factory=list)
    needs_review: int = 0

    def is_empty(self) -> bool:
        money = self.money
        return not (
            money.income
            or money.expense
            or money.repayments
            or self.new_debts
            or self.new_promises
            or self.people
            or self.groups
            or self.calls.total
            or self.claims_new
        )


def _window(column, start: datetime, end: datetime):
    return sa.and_(column >= start, column < end)


def _person(people: dict[int, PersonDay], persons: dict[int, Person], pid: int):
    if pid not in people:
        people[pid] = PersonDay(person=persons[pid])
    return people[pid]


async def gather_activity(
    session: AsyncSession, start: datetime, end: datetime, *, now: datetime
) -> DayActivity:
    """Everything [start, end) held, per person and per group, from SQL."""
    activity = DayActivity(
        start=start,
        end=end,
        now=now,
        money=await queries.money_between(session, start, end),
        completed=await queries.completed_between(session, start, end),
    )
    monitors = list(await session.scalars(sa.select(ChatMonitor)))
    activity.chat_titles = {m.tg_chat_id: m.title or str(m.tg_chat_id) for m in monitors}
    chat_types = {m.tg_chat_id: m.chat_type for m in monitors}
    in_window = _window(Interaction.occurred_at, start, end)

    # Everyone who shows up anywhere in the window, loaded once.
    touched: set[int] = set()
    counts: list[tuple[int, Direction, ChatType, int, datetime]] = []

    # (a) Messages: private chats both ways, and group lines aimed at the owner.
    rows = await session.execute(
        sa.select(
            Interaction.person_id,
            Interaction.direction,
            ChatMonitor.chat_type,
            sa.func.count(Interaction.id),
            sa.func.max(Interaction.occurred_at),
        )
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .where(is_member_message(), in_window, Interaction.person_id.is_not(None))
        .where(
            sa.or_(
                ChatMonitor.chat_type == ChatType.private,
                Interaction.meta["to_me"].astext == "true",
            )
        )
        .group_by(Interaction.person_id, Interaction.direction, ChatMonitor.chat_type)
    )
    for pid, direction, chat_type, n, last in rows.all():
        counts.append((pid, direction, chat_type, n, last))
        touched.add(pid)

    # (b) Calls, from the call log; recordings only when there is no log.
    call_type = Interaction.media["call_type"].astext
    duration = sa.cast(Interaction.media["duration_seconds"].astext, sa.Integer)
    call_rows = (
        await session.execute(
            sa.select(
                Interaction.person_id,
                call_type,
                sa.func.count(Interaction.id),
                sa.func.coalesce(sa.func.sum(duration), 0),
                sa.func.max(Interaction.occurred_at),
            )
            .where(Interaction.source == InteractionSource.phone_call)
            .where(Interaction.media["type"].astext == "call_log")
            .where(in_window)
            .group_by(Interaction.person_id, call_type)
        )
    ).all()
    if not call_rows:
        recording_rows = (
            await session.execute(
                sa.select(
                    Interaction.person_id,
                    sa.cast(Interaction.direction, sa.Text),
                    sa.func.count(Interaction.id),
                    sa.func.coalesce(sa.func.sum(duration), 0),
                    sa.func.max(Interaction.occurred_at),
                )
                .where(Interaction.source == InteractionSource.phone_call)
                .where(Interaction.media["type"].astext == "call_recording")
                .where(in_window)
                .group_by(Interaction.person_id, Interaction.direction)
            )
        ).all()
        call_rows = [
            (pid, "outgoing" if d == "out" else "incoming", n, secs, last)
            for pid, d, n, secs, last in recording_rows
        ]
    for pid, _kind, _, _, _ in call_rows:
        if pid is not None:
            touched.add(pid)

    # (c) Records.
    activity.new_debts = list(
        await session.scalars(
            sa.select(Debt)
            .options(selectinload(Debt.person))
            .where(_window(Debt.created_at, start, end))
            .order_by(Debt.id)
        )
    )
    activity.new_promises = list(
        await session.scalars(
            sa.select(Promise)
            .options(selectinload(Promise.person))
            .where(_window(Promise.created_at, start, end))
            .order_by(Promise.id)
        )
    )
    txns = list(
        await session.scalars(
            sa.select(Transaction)
            .where(_window(Transaction.occurred_at, start, end))
            .where(ACTIVE_TXN)
            .where(Transaction.counterparty_person_id.is_not(None))
            .order_by(Transaction.id)
        )
    )
    activity.claims_new = list(
        await session.scalars(
            sa.select(Claim)
            .where(_window(Claim.created_at, start, end))
            .order_by(Claim.id)
        )
    )
    touched |= {d.person_id for d in activity.new_debts}
    touched |= {p.person_id for p in activity.new_promises}
    touched |= {r.person_id for r in activity.money.repayments}
    touched |= {t.counterparty_person_id for t in txns}
    touched |= {c.person_id for c in activity.claims_new if c.person_id}

    # (d) Questions asked of the owner in the window.
    asked_rows = list(
        await session.scalars(
            sa.select(Interaction)
            .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
            .where(is_member_message(), in_window)
            .where(Interaction.direction == Direction.in_)
            .where(Interaction.person_id.is_not(None))
            .where(
                sa.or_(
                    ChatMonitor.chat_type == ChatType.private,
                    Interaction.meta["to_me"].astext == "true",
                )
            )
            .order_by(Interaction.occurred_at.desc())
            .limit(QUESTIONS_SCANNED)
        )
    )
    asked_rows = [
        r for r in asked_rows if loops.looks_like_question(r.raw_text or r.transcript)
    ]
    replied = dict(
        (
            await session.execute(
                sa.select(Interaction.tg_chat_id, sa.func.max(Interaction.occurred_at))
                .where(is_member_message())
                .where(Interaction.direction == Direction.out)
                .where(Interaction.occurred_at >= start, Interaction.occurred_at <= now)
                .group_by(Interaction.tg_chat_id)
            )
        ).all()
    )
    touched |= {r.person_id for r in asked_rows}

    persons = {
        p.id: p
        for p in await session.scalars(sa.select(Person).where(Person.id.in_(touched)))
    }
    people: dict[int, PersonDay] = {}

    for pid, direction, chat_type, n, last in counts:
        day = _person(people, persons, pid)
        if chat_type is ChatType.private:
            if direction is Direction.out:
                day.messages_out += n
            else:
                day.messages_in += n
        else:
            day.group_mentions += n
        day.seen(last)

    for pid, kind, n, seconds, last in call_rows:
        stats = activity.calls
        stats.total += n
        if kind == "incoming":
            stats.incoming += n
        elif kind == "outgoing":
            stats.outgoing += n
        elif kind == "missed":
            stats.missed += n
        elif kind == "rejected":
            stats.rejected += n
        stats.seconds += int(seconds or 0)
        if pid is None:
            stats.unknown += n
            continue
        day = _person(people, persons, pid)
        if kind in ("incoming", "outgoing"):
            day.calls += n
            day.call_seconds += int(seconds or 0)
        elif kind in ("missed", "rejected"):
            day.missed += n
        day.seen(last)

    for debt in activity.new_debts:
        day = _person(people, persons, debt.person_id)
        day.new_debt_ids.append(debt.id)
        day.seen(debt.created_at)
    for promise in activity.new_promises:
        day = _person(people, persons, promise.person_id)
        day.new_promise_ids.append(promise.id)
        day.seen(promise.created_at)
    for line in activity.money.repayments:
        day = _person(people, persons, line.person_id)
        if line.debt_id not in day.repayment_debt_ids:
            day.repayment_debt_ids.append(line.debt_id)
        day.seen(line.paid_at)
    for txn in txns:
        day = _person(people, persons, txn.counterparty_person_id)
        day.transaction_ids.append(txn.id)
        day.seen(txn.occurred_at)
    for claim in activity.claims_new:
        if claim.person_id and claim.person_id in persons:
            day = _person(people, persons, claim.person_id)
            day.claim_ids.append(claim.id)
            day.seen(claim.created_at)

    for row in asked_rows:
        day = _person(people, persons, row.person_id)
        if len(day.questions) >= QUESTIONS_PER_PERSON:
            continue
        later = replied.get(row.tg_chat_id)
        day.questions.append(
            AskedQuestion(
                interaction_id=row.id,
                text=(row.raw_text or row.transcript or "").strip(),
                asked_at=row.occurred_at,
                answered=loops.is_answered(row)
                or (later is not None and later > row.occurred_at),
                chat_title=activity.chat_titles.get(row.tg_chat_id or 0),
            )
        )

    # (e) Groups the owner reads.
    activity.groups = await _groups(session, monitors, start, end, in_window)

    # (f) What each person's words were, for the prose writer.
    for pid, day in people.items():
        day.content_ids = await _content_ids(session, pid, chat_types, in_window)

    # (g) Money first, then how much happened.
    for day in people.values():
        money_flag = bool(
            day.repayment_debt_ids
            or day.transaction_ids
            or day.new_debt_ids
            or day.claim_ids
        )
        day.score = (
            1000 * money_flag
            + 100
            * (len(day.new_debt_ids) + len(day.new_promise_ids) + len(day.claim_ids))
            + 50 * day.missed
            + 10 * day.calls
            + day.messages_in
            + day.messages_out
            + 5 * day.group_mentions
        )
    activity.people = sorted(
        people.values(),
        key=lambda d: (d.score, d.last_at or start),
        reverse=True,
    )

    # (h)–(j) What is open now, what waits for review, what was aimed at the owner.
    activity.questions_open = await loops.unanswered_questions(session, now=now)
    activity.missed_open = await loops.missed_calls(session, now=now)
    activity.needs_review = int(
        await session.scalar(
            sa.select(sa.func.count(Interaction.id))
            .where(Interaction.needs_review.is_(True))
            .where(_window(Interaction.created_at, start, end))
        )
        or 0
    )
    activity.to_me = await queries.messages_to_me(session, start=start, end=end)
    return activity


def _unapplied_window():
    return sa.or_(
        Interaction.window_id.is_(None),
        ConversationWindow.status != WindowStatus.applied,
    )


async def _groups(session, monitors, start, end, in_window) -> list[GroupDay]:
    groups: list[GroupDay] = []
    for monitor in monitors:
        if monitor.chat_type not in (ChatType.group, ChatType.channel):
            continue
        if not monitor.monitor_enabled:
            continue
        chat = Interaction.tg_chat_id == monitor.tg_chat_id
        messages, to_me, last = (
            await session.execute(
                sa.select(
                    sa.func.count(Interaction.id),
                    sa.func.count(Interaction.id).filter(
                        Interaction.meta["to_me"].astext == "true"
                    ),
                    sa.func.max(Interaction.occurred_at),
                ).where(chat, in_window, is_member_message())
            )
        ).one()
        summary_ids = list(
            await session.scalars(
                sa.select(Interaction.id)
                .where(chat, in_window, queries._is_window_row())
                .where(Interaction.summary.is_not(None))
                .order_by(Interaction.occurred_at)
            )
        )
        if not messages and not summary_ids:
            continue
        raw_ids = list(
            await session.scalars(
                sa.select(Interaction.id)
                .outerjoin(
                    ConversationWindow, ConversationWindow.id == Interaction.window_id
                )
                .where(chat, in_window, is_member_message(), _unapplied_window())
                .order_by(Interaction.occurred_at.desc())
                .limit(RAW_LINES)
            )
        )
        groups.append(
            GroupDay(
                tg_chat_id=monitor.tg_chat_id,
                title=monitor.title or str(monitor.tg_chat_id),
                messages=messages,
                to_me=to_me,
                summary_ids=summary_ids,
                raw_ids=raw_ids,
                last_at=last,
            )
        )
    groups.sort(key=lambda g: g.messages, reverse=True)
    return groups


async def _content_ids(session, pid: int, chat_types, in_window) -> list[int]:
    """Summaries of the person's private windows, recordings, and member lines
    no applied window covers yet."""
    private_chats = [c for c, t in chat_types.items() if t is ChatType.private]
    ids = list(
        await session.scalars(
            sa.select(Interaction.id)
            .where(Interaction.person_id == pid, in_window)
            .where(queries._is_window_row())
            .where(Interaction.summary.is_not(None))
            .where(Interaction.tg_chat_id.in_(private_chats))
            .order_by(Interaction.occurred_at)
        )
    )
    ids += list(
        await session.scalars(
            sa.select(Interaction.id)
            .where(Interaction.person_id == pid, in_window)
            .where(Interaction.source == InteractionSource.phone_call)
            .where(Interaction.media["type"].astext == "call_recording")
            .where(
                sa.or_(
                    Interaction.summary.is_not(None), Interaction.transcript.is_not(None)
                )
            )
            .order_by(Interaction.occurred_at)
        )
    )
    ids += list(
        await session.scalars(
            sa.select(Interaction.id)
            .outerjoin(ConversationWindow, ConversationWindow.id == Interaction.window_id)
            .where(Interaction.person_id == pid, in_window, is_member_message())
            .where(Interaction.tg_chat_id.in_(private_chats))
            .where(_unapplied_window())
            .order_by(Interaction.occurred_at.desc())
            .limit(RAW_LINES)
        )
    )
    return ids


__all__ = [
    "AskedQuestion",
    "CallStats",
    "CheckableTxn",
    "DayActivity",
    "GroupDay",
    "MoneyDay",
    "PersonDay",
    "RepaymentLine",
    "gather_activity",
]
