"""The Google Sheets one-way mirror (JV-07).

Every ``SHEETS_SYNC_SECONDS`` the worker's ``sheets_sync`` job brings the
owner's spreadsheet up to date with SQL, never the other way round:

* **Build.** Each tab's rows are rendered from SQL as plain cells: promises
  and tasks (``Tasks_Promises``), the clients worth following up
  (``Client_FollowUps``) and the receipts read from images (``Receipts``).
  Money is text with its currency in a column of its own, never summed or
  mixed; what a receipt says and what the bank recorded are separate
  columns. Names, titles and references a counterparty chose pass through
  ``safety.display_name`` (no bidi or zero-width tricks).
* **Plan.** Each row's cells are hashed; only a changed hash is written. A new
  entity takes the next ``row_number`` of its tab and keeps it for good, so
  every write goes to an explicit range and a retry after a timeout rewrites
  the same cells instead of adding a row. An entity that is gone (purged, or
  a client with nothing open any more) becomes a ``tombstone`` and its cells
  are cleared. The plan is committed before any network call.
* **Write.** Missing tabs (with their header) and a grid too small for the
  next row are fixed by one ``spreadsheets.batchUpdate``; then one
  ``values.batchUpdate`` per tab writes the due rows, ``RAW``, so nothing a
  client typed becomes a formula; it rewrites the header too, and the first
  run in a process writes the header of a tab that has nothing due. No
  transaction is held during the call.
* **Record.** Written rows become ``synced`` (tombstones ``cleared``); a
  failed write keeps the rows ``failed`` with the error code and a growing
  ``next_attempt_at``. The ``sheets`` heartbeat says when the mirror last
  caught up (``/holat``) and since when it has been failing
  (``sheets_failing``); a run with nothing due still counts as failing while
  any row waits out the backoff of a failure.

Every step is contained: one row that cannot be rendered or recorded is
logged with its ids and skipped, one tab that cannot be built leaves its rows
as they are, and nothing here raises into its caller. A no-op while
``SHEETS_ENABLED`` is false; with the key file or the spreadsheet id missing
it makes no network call and only beats failing. Core MIYA never waits on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import string
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.bot import replies
from miya.bot.formatting import SOURCE_WORD, ref
from miya.config import settings
from miya.db.enums import ChatType, InteractionSource, PromiseMadeBy, PromiseStatus
from miya.db.models import (
    SHEET_TABS,
    ChatMonitor,
    ClientCode,
    Interaction,
    Person,
    Promise,
    Receipt,
    SheetRow,
    Task,
    Transaction,
)
from miya.db.session import session_scope
from miya.services import health, loops, receipts, safety, sheets
from miya.services.sheets import Cell, CellRange, SheetsAPI, TabInfo

log = logging.getLogger(__name__)

TAB_TASKS, TAB_FOLLOWUPS, TAB_RECEIPTS = SHEET_TABS
HEADERS: dict[str, tuple[str, ...]] = {
    TAB_TASKS: replies.SHEETS_TASKS_HEADER,
    TAB_FOLLOWUPS: replies.SHEETS_FOLLOWUPS_HEADER,
    TAB_RECEIPTS: replies.SHEETS_RECEIPTS_HEADER,
}

PROMISE = "promise"
TASK = "task"
RECEIPT = "receipt"
PERSON = "person"

PENDING = "pending"
SYNCED = "synced"
FAILED = "failed"
TOMBSTONE = "tombstone"
CLEARED = "cleared"
GONE = (TOMBSTONE, CLEARED)

# Row 1 is the header.
FIRST_DATA_ROW = 2
# Most rows one tab writes in one run; the rest follow a minute later.
WRITE_BATCH = 500
# A new tab, and a grid that has to grow, get this much room past the last row.
NEW_TAB_ROWS = 1000
GROW_ROWS = 500
# A failed row waits SHEETS_SYNC_SECONDS, then twice that, … up to this.
RETRY_CAP = timedelta(hours=1)
# Cell text limits: a sheet is for reading, the database keeps everything.
TEXT_CHARS = 500
CONTEXT_CHARS = 300
NAME_CHARS = 120
# Cell IDs: the bot's own refs (p12, t7, x9), plus a receipt and a client.
RECEIPT_REF = "r"
PERSON_REF = "k"
# The error code for a tab whose rows could not be built from SQL.
PLAN_FAILED = "plan_failed"
# A tombstone keeps no digest of the cells it held: after /unut nothing in
# the row can confirm a guess about what was forgotten.
BLANK_HASH = "0" * 64
# A conversation window's own interaction is a summary, not a message.
WINDOW_KIND = "window"


Key = tuple[str, int]


@dataclass(frozen=True, slots=True)
class Row:
    """One entity's cells in one tab."""

    entity_kind: str
    entity_id: int
    cells: tuple[Cell, ...]

    @property
    def key(self) -> Key:
        return (self.entity_kind, self.entity_id)


@dataclass(slots=True)
class Built:
    """A tab rendered from SQL. ``skipped`` are entities whose row could not
    be rendered this time: their sheet row is left exactly as it is."""

    tab: str
    rows: dict[Key, Row] = field(default_factory=dict)
    skipped: set[Key] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Planned:
    """One range this run writes: the row's cells, or blanks to clear it.
    ``state``, ``content_hash`` and ``attempts`` are as planned, so the
    outcome is recorded only on a row nothing else changed meanwhile."""

    row_id: int
    tab: str
    entity_kind: str
    entity_id: int
    row_number: int
    state: str
    content_hash: str
    attempts: int
    cells: tuple[Cell, ...] | None

    @property
    def clears(self) -> bool:
        return self.cells is None


@dataclass(slots=True)
class SyncResult:
    written: int = 0
    cleared: int = 0
    failed: int = 0
    # Rows still pending, failed or waiting to be cleared after the run.
    waiting: int = 0
    created_tabs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# --- cells ------------------------------------------------------------------------


def clean(value: str | None, limit: int = NAME_CHARS) -> str:
    """Text for a cell: bidi and zero-width marks stripped, whitespace folded
    to single spaces, clipped with an ellipsis."""
    if not value:
        return ""
    text = " ".join(safety.display_name(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stamp(value: datetime | None) -> str:
    """'2026-10-20 14:05' on the owner's clock: sortable, unambiguous."""
    if value is None:
        return ""
    return value.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")


def due_text(day: date | None, at: time | None) -> str:
    if day is None:
        return ""
    return f"{day.isoformat()} {at.strftime('%H:%M')}" if at else day.isoformat()


def money_text(amount: Decimal) -> str:
    """An exact amount as text, two decimals, no grouping: '1250000.00'."""
    return f"{Decimal(amount).quantize(Decimal('0.01')):f}"


def content_hash(tab: str, cells: tuple[Cell, ...]) -> str:
    payload = json.dumps([tab, list(cells)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def column_letter(count: int) -> str:
    return string.ascii_uppercase[count - 1]


def a1_range(tab: str, row_number: int) -> str:
    last = column_letter(len(HEADERS[tab]))
    return f"'{tab}'!A{row_number}:{last}{row_number}"


@dataclass(frozen=True, slots=True)
class Source:
    """Where a promise or task came from: the channel, when, and context."""

    channel: str
    at: datetime
    context: str


def _channel(source: InteractionSource, group_title: str | None) -> str:
    word = SOURCE_WORD.get(source, source.value)
    return f"{word} · {clean(group_title)}" if group_title else word


async def _group_titles(session: AsyncSession, chat_ids: Iterable[int]) -> dict[int, str]:
    """Titles of the group chats among ``chat_ids``; a private chat has none."""
    ids = sorted(set(chat_ids))
    if not ids:
        return {}
    rows = await session.execute(
        sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title).where(
            ChatMonitor.tg_chat_id.in_(ids),
            ChatMonitor.chat_type != ChatType.private,
            ChatMonitor.title.isnot(None),
        )
    )
    return {chat_id: title for chat_id, title in rows.all() if title}


async def _sources(session: AsyncSession, ids: set[int]) -> dict[int, Source]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            sa.select(
                Interaction.id,
                Interaction.source,
                Interaction.occurred_at,
                Interaction.tg_chat_id,
                Interaction.summary,
                Interaction.raw_text,
                Interaction.transcript,
            ).where(Interaction.id.in_(sorted(ids)))
        )
    ).all()
    titles = await _group_titles(session, (r.tg_chat_id for r in rows if r.tg_chat_id))
    return {
        r.id: Source(
            channel=_channel(
                r.source, titles.get(r.tg_chat_id) if r.tg_chat_id else None
            ),
            at=r.occurred_at,
            context=clean(r.summary or r.raw_text or r.transcript, CONTEXT_CHARS),
        )
        for r in rows
    }


def promise_cells(promise: Promise, name: str, source: Source | None) -> tuple[Cell, ...]:
    shown = clean(name)
    mine = promise.made_by is PromiseMadeBy.me
    status = promise.status.value
    return (
        ref(PROMISE, promise.id),
        replies.SHEETS_KIND_PROMISE,
        replies.SHEETS_OWNER if mine else shown,
        shown if mine else replies.SHEETS_OWNER,
        clean(promise.description, TEXT_CHARS),
        due_text(promise.due_date, promise.due_time),
        replies.SHEETS_PROMISE_STATUS.get(status, status),
        source.channel if source else "",
        stamp(source.at if source else promise.created_at),
        source.context if source else "",
    )


def task_cells(task: Task, source: Source | None) -> tuple[Cell, ...]:
    status = task.status.value
    return (
        ref(TASK, task.id),
        replies.SHEETS_KIND_TASK,
        replies.SHEETS_OWNER,
        "",
        clean(task.description, TEXT_CHARS),
        due_text(task.due_date, task.due_time),
        replies.SHEETS_TASK_STATUS.get(status, status),
        source.channel if source else "",
        stamp(source.at if source else task.created_at),
        source.context if source else "",
    )


def _skip(built: Built, key: Key, exc: Exception) -> None:
    built.skipped.add(key)
    log.error(
        "sheet row for %s %s not rendered: %s",
        key[0],
        key[1],
        type(exc).__name__,
        extra={"tab": built.tab, "entity_kind": key[0], "entity_id": key[1]},
    )


async def build_tasks_promises(session: AsyncSession, now: datetime) -> Built:
    """Every promise and task, open or closed, with its source."""
    built = Built(TAB_TASKS)
    promises = (
        await session.execute(
            sa.select(Promise, Person.display_name)
            .join(Person, Person.id == Promise.person_id)
            .order_by(Promise.id)
        )
    ).all()
    tasks = list(await session.scalars(sa.select(Task).order_by(Task.id)))
    source_ids = {p.source_interaction_id for p, _ in promises} | {
        t.source_interaction_id for t in tasks
    }
    sources = await _sources(session, {i for i in source_ids if i is not None})
    for promise, name in promises:
        key = (PROMISE, promise.id)
        try:
            source = sources.get(promise.source_interaction_id or 0)
            built.rows[key] = Row(
                PROMISE, promise.id, promise_cells(promise, name, source)
            )
        except Exception as exc:
            _skip(built, key, exc)
    for task in tasks:
        key = (TASK, task.id)
        try:
            source = sources.get(task.source_interaction_id or 0)
            built.rows[key] = Row(TASK, task.id, task_cells(task, source))
        except Exception as exc:
            _skip(built, key, exc)
    return built


@dataclass(slots=True)
class _Client:
    name: str
    codes: list[str] = field(default_factory=list)
    open_promises: int = 0
    next_due: tuple[date, time | None] | None = None
    asked_at: datetime | None = None
    last_at: datetime | None = None
    channel: str = ""


def followup_cells(person_id: int, client: _Client) -> tuple[Cell, ...]:
    unanswered = (
        replies.SHEETS_UNANSWERED_YES.format(vaqt=stamp(client.asked_at))
        if client.asked_at is not None
        else replies.SHEETS_UNANSWERED_NO
    )
    return (
        f"{PERSON_REF}{person_id}",
        clean(client.name),
        ", ".join(sorted(client.codes)),
        client.channel,
        stamp(client.last_at),
        unanswered,
        # Open escalations arrive with the Jarvis core (JV-11, JV-22).
        "",
        client.open_promises,
        due_text(*client.next_due) if client.next_due else "",
    )


async def build_followups(session: AsyncSession, now: datetime) -> Built:
    """One row per client worth following up: anyone holding an active
    client code, with an open promise either way, or with a question the
    owner has not answered (loops.unanswered_questions)."""
    built = Built(TAB_FOLLOWUPS)
    codes: dict[int, list[str]] = {}
    for person_id, code in await session.execute(
        sa.select(ClientCode.person_id, ClientCode.code).where(
            ClientCode.status == "active"
        )
    ):
        codes.setdefault(person_id, []).append(code)
    open_rows = (
        await session.execute(
            sa.select(Promise.person_id, Promise.due_date, Promise.due_time).where(
                Promise.status == PromiseStatus.open
            )
        )
    ).all()
    asked: dict[int, datetime] = {}
    for question in await loops.unanswered_questions(session, now=now):
        if question.person is None:
            continue
        earlier = asked.get(question.person.id)
        if earlier is None or question.asked_at < earlier:
            asked[question.person.id] = question.asked_at

    ids = set(codes) | {row.person_id for row in open_rows} | set(asked)
    if not ids:
        return built
    clients = {
        person_id: _Client(name=name)
        for person_id, name in await session.execute(
            sa.select(Person.id, Person.display_name).where(Person.id.in_(sorted(ids)))
        )
    }
    for person_id, found in codes.items():
        if person_id in clients:
            clients[person_id].codes = found
    for row in open_rows:
        client = clients.get(row.person_id)
        if client is None:
            continue
        client.open_promises += 1
        if row.due_date is not None:
            due = (row.due_date, row.due_time)
            if client.next_due is None or _due_key(due) < _due_key(client.next_due):
                client.next_due = due
    for person_id, at in asked.items():
        if person_id in clients:
            clients[person_id].asked_at = at

    last = (
        await session.execute(
            sa.select(
                Interaction.person_id,
                Interaction.source,
                Interaction.occurred_at,
                Interaction.tg_chat_id,
            )
            .where(Interaction.person_id.in_(sorted(clients)))
            .where(sa.func.coalesce(Interaction.meta["kind"].astext, "") != WINDOW_KIND)
            .distinct(Interaction.person_id)
            .order_by(Interaction.person_id, Interaction.occurred_at.desc())
        )
    ).all()
    titles = await _group_titles(session, (r.tg_chat_id for r in last if r.tg_chat_id))
    for r in last:
        client = clients[r.person_id]
        client.last_at = r.occurred_at
        client.channel = _channel(r.source, titles.get(r.tg_chat_id or 0))

    for person_id, client in clients.items():
        key = (PERSON, person_id)
        try:
            built.rows[key] = Row(PERSON, person_id, followup_cells(person_id, client))
        except Exception as exc:
            _skip(built, key, exc)
    return built


def _due_key(due: tuple[date, time | None]) -> tuple[date, time]:
    """A date-only deadline sorts as the end of its day."""
    return (due[0], due[1] or time.max)


def bank_text(txn: Transaction) -> str:
    """The bank's own record of the payment: its ref, its exact amount and
    currency, when, and the bank channel. Never the receipt's word."""
    label = clean((txn.channel or "").partition(":")[2])
    parts = [
        ref("transaction", txn.id),
        f"{money_text(txn.amount)} {txn.currency.value}",
        stamp(txn.occurred_at),
    ]
    if label:
        parts.append(label)
    return " · ".join(parts)


def receipt_cells(
    receipt: Receipt, *, sender: str, channel: str, txn: Transaction | None
) -> tuple[Cell, ...]:
    read = receipt.state == receipts.STATE_READ
    amount = money_text(receipt.amount) if receipt.amount is not None else ""
    currency = receipt.currency.value if receipt.currency is not None else ""
    card = f"****{receipt.payee_card_last4}" if receipt.payee_card_last4 else ""
    payee = " ".join(p for p in (clean(receipt.payee_name), card) if p)
    verdict = (
        replies.SHEETS_VERDICT.get(receipt.verdict, receipt.verdict)
        if read
        else replies.SHEETS_UNREADABLE
    )
    return (
        f"{RECEIPT_REF}{receipt.id}",
        stamp(receipt.paid_at),
        amount,
        currency,
        clean(receipt.payer_name),
        payee,
        clean(receipt.issuer),
        clean(receipt.reference),
        sender,
        channel,
        verdict,
        bank_text(txn) if txn is not None else "",
    )


async def build_receipts(session: AsyncSession, now: datetime) -> Built:
    """Every receipt read from an image (a rejected one was not a receipt):
    what it says, who sent it where, and separately the bank's record."""
    built = Built(TAB_RECEIPTS)
    rows = (
        await session.execute(
            sa.select(
                Receipt,
                Interaction.source,
                Interaction.tg_chat_id,
                Interaction.person_id,
            )
            .join(Interaction, Interaction.id == Receipt.interaction_id)
            .where(Receipt.state != receipts.STATE_REJECTED)
            .order_by(Receipt.id)
        )
    ).all()
    if not rows:
        return built
    person_ids = sorted(
        {pid for r in rows if (pid := r.Receipt.person_id or r.person_id) is not None}
    )
    names: dict[int, str] = {}
    if person_ids:
        for person_id, name in await session.execute(
            sa.select(Person.id, Person.display_name).where(Person.id.in_(person_ids))
        ):
            names[person_id] = name
    chat_ids = sorted({r.tg_chat_id for r in rows if r.tg_chat_id is not None})
    monitors: dict[int, tuple[str | None, ChatType]] = {}
    if chat_ids:
        for chat_id, title, chat_type in await session.execute(
            sa.select(
                ChatMonitor.tg_chat_id, ChatMonitor.title, ChatMonitor.chat_type
            ).where(ChatMonitor.tg_chat_id.in_(chat_ids))
        ):
            monitors[chat_id] = (title, chat_type)
    bank_ids: dict[int, int] = {}
    for r in rows:
        receipt = r.Receipt
        txn_id = receipt.transaction_id
        if txn_id is None and receipt.verdict == receipts.VERDICT_CONFIRMED:
            txn_id = receipts.confirmed_by(receipt)
        if txn_id is not None:
            bank_ids[receipt.id] = txn_id
    txns = (
        {
            txn.id: txn
            for txn in await session.scalars(
                sa.select(Transaction).where(
                    Transaction.id.in_(sorted(set(bank_ids.values()))),
                    Transaction.voided_at.is_(None),
                )
            )
        }
        if bank_ids
        else {}
    )

    for r in rows:
        receipt = r.Receipt
        key = (RECEIPT, receipt.id)
        try:
            if r.source in receipts.BOT_SOURCES:
                sender, channel = replies.SHEETS_OWNER, replies.SHEETS_BOT_CHANNEL
            else:
                title, chat_type = monitors.get(
                    r.tg_chat_id or 0, (None, ChatType.private)
                )
                person_id = receipt.person_id or r.person_id
                who = names.get(person_id) if person_id is not None else None
                sender = clean(who or title)
                group = chat_type is not ChatType.private and bool(title)
                channel = clean(title) if group else replies.SHEETS_PRIVATE_CHANNEL
            txn = txns.get(bank_ids.get(receipt.id, 0))
            cells = receipt_cells(receipt, sender=sender, channel=channel, txn=txn)
            built.rows[key] = Row(RECEIPT, receipt.id, cells)
        except Exception as exc:
            _skip(built, key, exc)
    return built


Builder = Callable[[AsyncSession, datetime], Awaitable[Built]]
BUILDERS: dict[str, Builder] = {
    TAB_TASKS: build_tasks_promises,
    TAB_FOLLOWUPS: build_followups,
    TAB_RECEIPTS: build_receipts,
}


# --- plan ---------------------------------------------------------------------------


def retry_at(attempts: int, now: datetime) -> datetime:
    """When a row that has failed ``attempts`` times is tried again."""
    base = timedelta(seconds=settings.sheets_sync_seconds)
    doublings: int = min(max(attempts - 1, 0), 16)
    wait: timedelta = min(RETRY_CAP, base * (2**doublings))
    return now + wait


async def _plan_rows(session: AsyncSession, built: Built) -> None:
    """Bring ``sheet_rows`` of one tab in line with what was built: new
    entities get the next row, changed ones go pending, gone ones become
    tombstones. One savepoint per row; a row that fails is logged and left."""
    tab = built.tab
    existing = (
        await session.execute(
            sa.select(
                SheetRow.id,
                SheetRow.entity_kind,
                SheetRow.entity_id,
                SheetRow.row_number,
                SheetRow.content_hash,
                SheetRow.state,
            ).where(SheetRow.tab == tab)
        )
    ).all()
    by_key = {(r.entity_kind, r.entity_id): r for r in existing}
    next_row = max(
        (r.row_number for r in existing if r.row_number is not None),
        default=FIRST_DATA_ROW - 1,
    )
    for key, row in built.rows.items():
        digest = content_hash(tab, row.cells)
        current = by_key.get(key)
        if current is None:
            statement: Any = sa.insert(SheetRow).values(
                tab=tab,
                entity_kind=row.entity_kind,
                entity_id=row.entity_id,
                row_number=next_row + 1,
                content_hash=digest,
                state=PENDING,
            )
        else:
            values: dict[str, Any] = {}
            if current.state in GONE:
                values = {"state": PENDING, "attempts": 0, "next_attempt_at": None}
            elif current.content_hash != digest and current.state != FAILED:
                # A failed row keeps its backoff: the sheet, not the row, failed.
                values = {"state": PENDING}
            if current.content_hash != digest:
                values["content_hash"] = digest
            if current.row_number is None:
                values["row_number"] = next_row + 1
            if not values:
                continue
            statement = (
                sa.update(SheetRow).where(SheetRow.id == current.id).values(**values)
            )
        try:
            async with session.begin_nested():
                await session.execute(statement)
        except Exception as exc:
            log.error(
                "sheet row for %s %s not planned: %s",
                key[0],
                key[1],
                type(exc).__name__,
                extra={"tab": tab, "entity_kind": key[0], "entity_id": key[1]},
            )
            continue
        if current is None or current.row_number is None:
            next_row += 1

    gone = [
        r.id
        for key, r in by_key.items()
        if key not in built.rows and key not in built.skipped and r.state not in GONE
    ]
    if gone:
        try:
            async with session.begin_nested():
                await session.execute(
                    sa.update(SheetRow)
                    .where(SheetRow.id.in_(gone))
                    .values(
                        state=TOMBSTONE,
                        content_hash=BLANK_HASH,
                        attempts=0,
                        next_attempt_at=None,
                    )
                )
        except Exception as exc:
            log.error(
                "%d sheet row(s) not tombstoned: %s",
                len(gone),
                type(exc).__name__,
                extra={"tab": tab, "sheet_row_ids": gone},
            )


def _due_clause(now: datetime) -> sa.ColumnElement[bool]:
    return sa.or_(
        SheetRow.state == PENDING,
        sa.and_(
            SheetRow.state.in_((FAILED, TOMBSTONE)),
            sa.or_(SheetRow.next_attempt_at.is_(None), SheetRow.next_attempt_at <= now),
        ),
    )


async def _due(session: AsyncSession, built: Built, now: datetime) -> list[Planned]:
    """The ranges this run writes for one tab, by row number."""
    rows = (
        await session.execute(
            sa.select(
                SheetRow.id,
                SheetRow.entity_kind,
                SheetRow.entity_id,
                SheetRow.row_number,
                SheetRow.state,
                SheetRow.content_hash,
                SheetRow.attempts,
            )
            .where(SheetRow.tab == built.tab, SheetRow.row_number.isnot(None))
            .where(_due_clause(now))
            .order_by(SheetRow.row_number)
            .limit(WRITE_BATCH)
        )
    ).all()
    planned: list[Planned] = []
    for r in rows:
        cells: tuple[Cell, ...] | None = None
        if r.state != TOMBSTONE:
            row = built.rows.get((r.entity_kind, r.entity_id))
            if row is None:
                continue  # not rendered this run: written once it is
            cells = row.cells
        planned.append(
            Planned(
                row_id=r.id,
                tab=built.tab,
                entity_kind=r.entity_kind,
                entity_id=r.entity_id,
                row_number=r.row_number,
                state=r.state,
                content_hash=r.content_hash,
                attempts=r.attempts,
                cells=cells,
            )
        )
    return planned


async def plan(
    session: AsyncSession, *, now: datetime, failed: list[str] | None = None
) -> list[Planned]:
    """Build every tab and plan its writes. A tab that cannot be built is
    left exactly as it is this run, and named in ``failed``."""
    planned: list[Planned] = []
    for tab, builder in BUILDERS.items():
        try:
            async with session.begin_nested():
                built = await builder(session, now)
                await _plan_rows(session, built)
                planned += await _due(session, built, now)
        except Exception as exc:
            log.error(
                "sheet tab %s not planned: %s",
                tab,
                type(exc).__name__,
                extra={"tab": tab},
            )
            if failed is not None:
                failed.append(tab)
    return planned


# --- write ------------------------------------------------------------------------


def _header_request(sheet_id: int, header: tuple[str, ...]) -> dict[str, Any]:
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [
                {"values": [{"userEnteredValue": {"stringValue": h}} for h in header]}
            ],
            "fields": "userEnteredValue",
        }
    }


def layout_requests(
    tabs: dict[str, TabInfo], needed_rows: dict[str, int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The one ``spreadsheets.batchUpdate`` that makes the spreadsheet fit:
    each missing tab added with its header row (frozen), each grid grown
    past the last row this run writes. Returns the requests and the tabs
    they create."""
    requests: list[dict[str, Any]] = []
    created: list[str] = []
    next_id = max((info.sheet_id for info in tabs.values()), default=0) + 1
    for tab in SHEET_TABS:
        header = HEADERS[tab]
        columns = len(header)
        rows = needed_rows.get(tab, 1)
        info = tabs.get(tab)
        if info is None:
            requests.append(
                {
                    "addSheet": {
                        "properties": {
                            "sheetId": next_id,
                            "title": tab,
                            "gridProperties": {
                                "rowCount": max(NEW_TAB_ROWS, rows + GROW_ROWS),
                                "columnCount": columns,
                                "frozenRowCount": 1,
                            },
                        }
                    }
                }
            )
            requests.append(_header_request(next_id, header))
            created.append(tab)
            next_id += 1
        elif info.row_count < rows or info.column_count < columns:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": info.sheet_id,
                            "gridProperties": {
                                "rowCount": max(info.row_count, rows + GROW_ROWS),
                                "columnCount": max(info.column_count, columns),
                            },
                        },
                        "fields": "gridProperties.rowCount,gridProperties.columnCount",
                    }
                }
            )
    return requests, created


def value_ranges(tab: str, planned: list[Planned]) -> list[CellRange]:
    """The tab's header and its due rows at their own ranges; a cleared row
    is written blank."""
    header = HEADERS[tab]
    blank: list[Cell] = [""] * len(header)
    data = [CellRange(a1_range(tab, 1), [list(header)])]
    for item in planned:
        cells = blank if item.cells is None else list(item.cells)
        data.append(CellRange(a1_range(tab, item.row_number), [cells]))
    return data


async def _write(
    api: SheetsAPI, planned: list[Planned], result: SyncResult
) -> dict[str, str | None]:
    """Do the network part. Returns each written tab's outcome: None when it
    went through, else the error code."""
    by_tab: dict[str, list[Planned]] = {}
    for item in planned:
        by_tab.setdefault(item.tab, []).append(item)
    outcome: dict[str, str | None] = {}
    first_look = not api.layout_checked
    try:
        if planned or first_look:
            tabs = await api.tabs()
            needed = {
                tab: max(item.row_number for item in items)
                for tab, items in by_tab.items()
            }
            requests, created = layout_requests(tabs, needed)
            if requests:
                await api.batch_update(requests)
            result.created_tabs = created
            api.layout_checked = True
            if first_look:
                # A tab that was already there but has nothing due yet (made
                # by hand, still empty) gets its header row on its own; if
                # that fails, the header goes out with the tab's first row.
                for tab in SHEET_TABS:
                    if tab in tabs and tab not in created:
                        by_tab.setdefault(tab, [])
    except Exception as exc:
        code = sheets.error_code(exc)
        log.error(
            "sheets layout not checked: %s",
            code,
            extra={"error": code, "rows": len(planned)},
        )
        result.error = code
        return dict.fromkeys(by_tab, code)
    for tab, items in by_tab.items():
        try:
            await api.write_values(value_ranges(tab, items))
            outcome[tab] = None
        except Exception as exc:
            code = sheets.error_code(exc)
            log.error(
                "sheet tab %s not written: %s",
                tab,
                code,
                extra={"tab": tab, "error": code, "rows": len(items)},
            )
            outcome[tab] = code
            result.error = code
    return outcome


# --- record -----------------------------------------------------------------------


async def _record(
    session: AsyncSession,
    planned: list[Planned],
    outcome: dict[str, str | None],
    result: SyncResult,
    now: datetime,
) -> None:
    """Write each range's outcome back, one savepoint per row, only onto a
    row nothing else changed since it was planned."""
    for item in planned:
        code = outcome.get(item.tab)
        if code is None:
            values: dict[str, Any] = {
                "state": CLEARED if item.clears else SYNCED,
                "attempts": 0,
                "next_attempt_at": None,
                "last_error": None,
                "synced_at": now,
            }
        else:
            attempts = item.attempts + 1
            values = {
                "state": TOMBSTONE if item.clears else FAILED,
                "attempts": min(attempts, 32767),
                "next_attempt_at": retry_at(attempts, now),
                "last_error": code,
            }
        try:
            async with session.begin_nested():
                changed = await session.scalar(
                    sa.update(SheetRow)
                    .where(
                        SheetRow.id == item.row_id,
                        SheetRow.state == item.state,
                        SheetRow.content_hash == item.content_hash,
                    )
                    .values(**values)
                    .returning(SheetRow.id)
                )
        except Exception as exc:
            log.error(
                "sheet row %s outcome not recorded: %s",
                item.row_id,
                type(exc).__name__,
                extra={
                    "sheet_row_id": item.row_id,
                    "entity_kind": item.entity_kind,
                    "entity_id": item.entity_id,
                },
            )
            continue
        if changed is None:
            continue
        if code is not None:
            result.failed += 1
        elif item.clears:
            result.cleared += 1
        else:
            result.written += 1

    if result.created_tabs:
        # A tab that was missing starts blank: every row it held is written
        # again on the next run.
        written = [item.row_id for item in planned if outcome.get(item.tab) is None]
        try:
            async with session.begin_nested():
                await session.execute(
                    sa.update(SheetRow)
                    .where(SheetRow.tab.in_(result.created_tabs))
                    .where(SheetRow.state == SYNCED, SheetRow.id.not_in(written))
                    .values(state=PENDING)
                )
                await session.execute(
                    sa.update(SheetRow)
                    .where(SheetRow.tab.in_(result.created_tabs))
                    .where(SheetRow.state == TOMBSTONE)
                    .values(state=CLEARED, next_attempt_at=None)
                )
        except Exception as exc:
            log.error(
                "rows of recreated sheet tabs not requeued: %s",
                type(exc).__name__,
                extra={"tabs": result.created_tabs},
            )


async def _waiting(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(SheetRow)
            .where(SheetRow.state.in_((PENDING, FAILED, TOMBSTONE)))
        )
        or 0
    )


async def _still_failing(session: AsyncSession) -> str | None:
    """The error code of the latest row still failing (a write or a clear
    that has not gone out since it failed), or None. Never raises."""
    try:
        code = await session.scalar(
            sa.select(SheetRow.last_error)
            .where(SheetRow.state.in_((FAILED, TOMBSTONE)), SheetRow.attempts > 0)
            .order_by(SheetRow.updated_at.desc(), SheetRow.id.desc())
            .limit(1)
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        code = type(exc).__name__[: sheets.ERROR_CODE_MAX]
        log.error("sheets failures not checked: %s", code, extra={"error": code})
    return code or None


async def sync(session: AsyncSession, api: SheetsAPI, *, now: datetime) -> SyncResult:
    """One pass: plan and commit, write with no transaction held, record and
    commit. Never raises; a failure is in the result's ``error``."""
    result = SyncResult()
    unbuilt: list[str] = []
    try:
        planned = await plan(session, now=now, failed=unbuilt)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        result.error = type(exc).__name__[: sheets.ERROR_CODE_MAX]
        log.error(
            "sheets sync not planned: %s", result.error, extra={"error": result.error}
        )
        return result
    if unbuilt:
        # The other tabs still go out; the mirror is not healthy, though.
        result.error = PLAN_FAILED
    started = monotonic()
    outcome = await _write(api, planned, result)
    # Outcomes count from when the write ended: a hung call can take minutes,
    # and a backoff counted from the start would already be over.
    done = now + timedelta(seconds=monotonic() - started)
    try:
        await _record(session, planned, outcome, result, done)
        result.waiting = await _waiting(session)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        code = type(exc).__name__[: sheets.ERROR_CODE_MAX]
        log.error("sheets sync not recorded: %s", code, extra={"error": code})
        result.error = result.error or code
    if result.error is None:
        # A run with nothing due is no success while rows still wait out the
        # backoff of a failure: the mirror stays failing until they go out.
        result.error = await _still_failing(session)
    if result.written or result.cleared or result.failed:
        log.info(
            "sheets: %d row(s) written, %d cleared, %d failed",
            result.written,
            result.cleared,
            result.failed,
            extra={
                "written": result.written,
                "cleared": result.cleared,
                "failed": result.failed,
                "waiting": result.waiting,
            },
        )
    return result


# --- the job and the purge hook ---------------------------------------------------


async def _beat(result: SyncResult, now: datetime) -> None:
    """The ``sheets`` heartbeat, carrying the last success and the start of
    the current failure forward. Never raises."""
    try:
        async with session_scope() as session:
            rows = await health.beats(session)
            previous = rows.get(health.SHEETS_COMPONENT)
            detail = dict((previous.detail if previous is not None else None) or {})
            last_ok = detail.get("last_ok_at")
            failing_since = detail.get("failing_since")
            if result.ok:
                last_ok, failing_since = now.isoformat(), None
            elif failing_since is None or detail.get("ok") is not False:
                failing_since = now.isoformat()
            await health.beat(
                session,
                health.SHEETS_COMPONENT,
                detail={
                    "ok": result.ok,
                    "error": result.error,
                    "last_ok_at": last_ok,
                    "failing_since": failing_since,
                    "written": result.written,
                    "cleared": result.cleared,
                    "waiting": result.waiting,
                },
                now=now,
            )
    except Exception as exc:
        log.error("sheets heartbeat not written: %s", type(exc).__name__)


async def run(
    *, api: SheetsAPI | None = None, now: datetime | None = None
) -> SyncResult | None:
    """The ``sheets_sync`` job: a no-op (None) while the mirror is off; with
    no key file or spreadsheet id, no network call, only the failing beat.
    Never raises."""
    if not settings.sheets_enabled:
        return None
    now = now or datetime.now(settings.tz)
    if api is None:
        try:
            problem = sheets.config_problem()
            found = sheets.get_api() if problem is None else None
        except Exception as exc:
            problem, found = sheets.error_code(exc), None
        if found is None:
            result = SyncResult(error=problem or sheets.KEY_FILE_MISSING)
            log.warning(
                "sheets mirror enabled but not configured: %s",
                result.error,
                extra={"error": result.error},
            )
            await _beat(result, now)
            return result
        api = found
    try:
        async with session_scope() as session:
            result = await sync(session, api, now=now)
    except Exception as exc:
        result = SyncResult(error=type(exc).__name__[: sheets.ERROR_CODE_MAX])
        log.error("sheets sync failed: %s", result.error, extra={"error": result.error})
    await _beat(result, now)
    return result


ENTITY_MODELS: dict[str, type[Promise] | type[Task] | type[Receipt] | type[Person]] = {
    PROMISE: Promise,
    TASK: Task,
    RECEIPT: Receipt,
    PERSON: Person,
}


async def tombstone_missing(session: AsyncSession) -> int:
    """Mark the sheet rows of entities that no longer exist as tombstones,
    so the next sync clears their cells (``/unut``). In the caller's
    transaction, in a savepoint; never raises. Returns the count."""
    marked = 0
    try:
        async with session.begin_nested():
            for kind, model in ENTITY_MODELS.items():
                alive = sa.select(model.id).where(model.id == SheetRow.entity_id).exists()
                ids = await session.scalars(
                    sa.update(SheetRow)
                    .where(SheetRow.entity_kind == kind, SheetRow.state.not_in(GONE))
                    .where(~alive)
                    .values(
                        state=TOMBSTONE,
                        content_hash=BLANK_HASH,
                        attempts=0,
                        next_attempt_at=None,
                    )
                    .returning(SheetRow.id)
                )
                marked += len(ids.all())
    except Exception as exc:
        log.error("sheet rows not tombstoned: %s", type(exc).__name__)
        return 0
    return marked


__all__ = [
    "HEADERS",
    "TAB_FOLLOWUPS",
    "TAB_RECEIPTS",
    "TAB_TASKS",
    "Planned",
    "Row",
    "SyncResult",
    "content_hash",
    "layout_requests",
    "plan",
    "run",
    "sync",
    "tombstone_missing",
    "value_ranges",
]
