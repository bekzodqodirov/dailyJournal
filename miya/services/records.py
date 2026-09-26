"""Closing and correcting one record (build step 1 of docs/owner-decisions.md).

`/bajarildi d12`, `/yop p7`, `/qaytar p7`, `/tuzat d12 6 mln` and the inline
buttons all land here. Three rules:

* **Nothing is silent.** Every change is appended to the row's ``history``
  list — what changed, from what, to what, when, and whether a button or a
  command did it — so a correction can always be audited the way `/unut` can.
* **Status is written here and nowhere else.** ``PromiseStatus.done`` and
  ``TaskStatus.done`` existed for five phases without a single writer; the
  evening report's "Bajarilganlar" could never list a kept promise, and an
  overdue promise was reminded every day forever. This module is the writer.
* **A debt's payments never sum above its amount, and a mis-tap is always
  reversible.** The row a mutation touches is locked (``SELECT … FOR
  UPDATE``) so two taps on ✅ cannot both settle it, an amount edit resizes
  the payment ✅ wrote rather than leaving it stale, and a refusal happens
  before anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from rapidfuzz.distance import Levenshtein
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from miya.bot.formatting import kind_of, parse_ref
from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    PromiseStatus,
    TaskStatus,
    TransactionType,
)
from miya.db.models import Debt, DebtPayment, Person, Promise, Task, Transaction
from miya.services.people import resolve_person

Record = Debt | Promise | Task | Transaction

MODEL_OF = {"debt": Debt, "promise": Promise, "task": Task, "transaction": Transaction}

# Which field each kind of row can have corrected. A task has no person and a
# promise has no amount; asking for either is answered, not guessed at.
EDITABLE = {
    "debt": ("amount", "currency", "direction", "person", "due"),
    "promise": ("person", "due"),
    "task": ("due",),
    # A money row (WP-13): "due" is the day it happened, "note" its
    # description. It is never "done" — it is voided or corrected.
    "transaction": (
        "amount",
        "currency",
        "direction",
        "person",
        "due",
        "note",
        "category",
    ),
}
# The longest description and category a correction may write.
NOTE_MAX = 500
CATEGORY_MAX = 64

# Who or what made the change — the history's "by" value.
BY_COMMAND = "command"
BY_BUTTON = "button"
BY_EXTRACTION = "extraction"


class RecordError(Exception):
    """Base for the owner-facing refusals; the message is chosen in replies."""


class NotOpen(RecordError):
    """The row is already closed."""


class AlreadyOpen(RecordError):
    """`/qaytar` on a row that was never closed."""


class NotReopenable(RecordError):
    """`/qaytar` on a debt that real payments cover: undoing a close cannot
    delete money the owner or the extractor recorded — that is `/tuzat`."""


class NotClosable(RecordError):
    """`/yop` on a debt: money is settled or corrected, never voided."""


class NotDoable(RecordError):
    """`/bajarildi x12`: a money row is not a promise — it is voided
    (`/ochir`) or corrected (`/tuzat`), never "done"."""


class NotEditable(RecordError):
    """The field does not exist on this kind of row."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class PaymentsExceed(RecordError):
    """`/tuzat d12 <amount>` below what was really repaid: the invariant
    "payments never sum above the amount" wins, and the owner is told what
    the books hold instead of the payments being silently trimmed."""

    def __init__(self, paid: Decimal, amount: Decimal, currency: Currency) -> None:
        super().__init__(f"{paid} > {amount}")
        self.paid = paid
        self.amount = amount
        self.currency = currency


class PaymentsExist(RecordError):
    """A currency change while payments are on the books. The payments carry
    the old currency, and the balance sums them without converting; nothing
    is converted silently, so the change is refused until they are gone."""


class UnknownPerson(RecordError):
    """`/tuzat d12 Sardor` when MIYA knows no Sardor: the owner decided that
    anything uncertain is asked first, so the person is not created until he
    says so (see `ask_new_person`)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(slots=True)
class Change:
    """One applied change, with enough context to render the corrected line."""

    kind: str
    record: Record
    person: Person | None
    field: str
    old: str | None
    new: str | None


@dataclass(slots=True)
class Edit:
    """A parsed `/tuzat` argument: which field, and the typed value."""

    field: str
    value: object


# --- lookup ----------------------------------------------------------------


async def load(session: AsyncSession, kind: str, record_id: int) -> Record | None:
    """The row behind a reference, locked, with its person loaded.

    ``FOR UPDATE``: every caller is about to mutate the row (or decide not
    to), and two taps on ✅ that both read the debt as open would both write
    a settling payment. The second reader now waits for the first commit and
    sees a settled row. The person comes by a separate select (lazy="raise"
    on the relationship), so the lock covers exactly this row.
    """
    model = MODEL_OF[kind]
    stmt = sa.select(model).where(model.id == record_id).with_for_update()
    if kind == "transaction":
        stmt = stmt.options(selectinload(Transaction.counterparty))
    elif kind != "task":
        stmt = stmt.options(selectinload(model.person))
    return await session.scalar(stmt)


async def find(session: AsyncSession, text: str) -> tuple[str, Record] | None:
    """'d12' → ('debt', <Debt 12>), or None when it is not a valid open ref."""
    parsed = parse_ref(text)
    if parsed is None:
        return None
    kind, record_id = parsed
    record = await load(session, kind, record_id)
    if record is None:
        return None
    return kind, record


def person_of(record: Record) -> Person | None:
    if isinstance(record, Transaction):
        return record.counterparty
    return None if isinstance(record, Task) else record.person


async def _with_person(session: AsyncSession, record: Record) -> None:
    """Load the row's person if the caller did not (the relationship raises).

    Rows from `find` arrive loaded; a row the persistence layer just built,
    or one a test handed over, does not — and a Change must always be able
    to render its line.
    """
    if isinstance(record, Transaction):
        if "counterparty" in sa.inspect(record).unloaded:
            record.counterparty = (
                await session.get(Person, record.counterparty_person_id)
                if record.counterparty_person_id is not None
                else None
            )
        return
    if isinstance(record, Task) or "person" not in sa.inspect(record).unloaded:
        return
    record.person = await session.get(Person, record.person_id)


async def _lock(session: AsyncSession, record: Record) -> None:
    """Take the row lock and re-read the status under it.

    A row can reach a mutation without having come through `load` — the
    persistence layer's fulfilment, `settle_balance`, a test — or have been
    read before another process closed it. Locking here, and re-reading the
    columns a decision depends on, makes every close idempotent: whoever
    comes second sees the settled row and gets "already done".
    """
    await session.flush()
    if sa.inspect(record).pending:
        return  # not in the database yet; nothing to lock against
    if isinstance(record, Debt):
        names = ["status", "amount", "currency"]
    elif isinstance(record, Transaction):
        names = ["voided_at", "amount", "currency", "type"]
    else:
        names = ["status"]
    await session.refresh(record, attribute_names=names, with_for_update=True)


def is_open(record: Record) -> bool:
    if isinstance(record, Transaction):
        return record.voided_at is None
    if isinstance(record, Debt):
        return record.status is not DebtStatus.settled
    if isinstance(record, Promise):
        return record.status is PromiseStatus.open
    return record.status in (TaskStatus.todo, TaskStatus.doing)


async def paid_against(session: AsyncSession, debt: Debt) -> Decimal:
    paid = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(DebtPayment.amount), 0)).where(
            DebtPayment.debt_id == debt.id
        )
    )
    return Decimal(paid or 0)


# --- history ---------------------------------------------------------------


def _note(
    record: Record,
    field: str,
    old: object,
    new: object,
    by: str,
    now: datetime,
    **extra: object,
) -> tuple[str | None, str | None]:
    """Append one entry to the row's history; returns the stringified values."""
    old_s = _plain(old)
    new_s = _plain(new)
    # A fresh list, not .append(): the ORM only sees JSONB reassignment.
    record.history = [
        *(record.history or []),
        {
            "at": now.isoformat(),
            "field": field,
            "old": old_s,
            "new": new_s,
            "by": by,
            **extra,
        },
    ]
    return old_s, new_s


def _plain(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):  # enum member
        return str(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"  # the same text whether it came from SQL or a command
    return str(value)


# --- closing ---------------------------------------------------------------

# The note on the payment row `mark_done` writes to settle a debt in full, and
# the history field that records that row's id. Together they make the payment
# identifiable, so `reopen` can remove exactly it and nothing the owner or the
# extractor recorded as real money.
SETTLED_NOTE = "bajarildi"
PAYMENT_FIELD = "payment"


async def _payments(session: AsyncSession, debt: Debt) -> list[DebtPayment]:
    return list(
        await session.scalars(
            sa.select(DebtPayment)
            .where(DebtPayment.debt_id == debt.id)
            .order_by(DebtPayment.id)
            .with_for_update()
        )
    )


def _settling_ids(debt: Debt) -> set[int]:
    """The ids of the payments ✅ wrote, as the history names them."""
    return {
        entry["payment_id"]
        for entry in debt.history or []
        if entry.get("field") == PAYMENT_FIELD and entry.get("payment_id")
    }


def _split_payments(
    debt: Debt, payments: list[DebtPayment]
) -> tuple[list[DebtPayment], list[DebtPayment]]:
    """(synthetic, real): the settling rows ✅ wrote versus recorded money."""
    settling = _settling_ids(debt)
    synthetic = [p for p in payments if p.note == SETTLED_NOTE and p.id in settling]
    real = [p for p in payments if p not in synthetic]
    return synthetic, real


async def mark_done(
    session: AsyncSession, record: Record, *, by: str, now: datetime | None = None
) -> Change:
    """Kept: a promise/task is done, a debt is settled in full.

    Idempotent under concurrency: the row is locked and re-read first, so a
    second ✅ — a double tap, or the same button on two messages — raises
    NotOpen and writes no second payment.
    """
    now = now or datetime.now(settings.tz)
    if isinstance(record, Transaction):
        raise NotDoable()
    await _lock(session, record)
    if not is_open(record):
        raise NotOpen()
    kind = kind_of(record)
    await _with_person(session, record)

    if isinstance(record, Debt):
        remaining = record.amount - await paid_against(session, record)
        if remaining > 0:
            # A payment row keeps the books additive: the balance query sums
            # amount minus payments, so "settled" must also mean "paid".
            payment = DebtPayment(
                debt_id=record.id,
                amount=remaining,
                currency=record.currency,
                paid_at=now,
                note=SETTLED_NOTE,
            )
            session.add(payment)
            await session.flush()
            _note(record, PAYMENT_FIELD, None, remaining, by, now, payment_id=payment.id)
        old, new = _note(record, "status", record.status, DebtStatus.settled, by, now)
        record.status = DebtStatus.settled
        record.settled_at = now
    elif isinstance(record, Promise):
        old, new = _note(record, "status", record.status, PromiseStatus.done, by, now)
        record.status = PromiseStatus.done
        record.completed_at = now
    else:
        old, new = _note(record, "status", record.status, TaskStatus.done, by, now)
        record.status = TaskStatus.done
        record.completed_at = now

    await session.flush()
    return Change(kind, record, person_of(record), "status", old, new)


async def close(
    session: AsyncSession, record: Record, *, by: str, now: datetime | None = None
) -> Change:
    """Closed without being kept: cancelled promise, dropped task.

    ``completed_at`` stays empty on purpose — "Bajarilganlar" reads it.
    """
    now = now or datetime.now(settings.tz)
    if isinstance(record, Debt):
        raise NotClosable()
    if isinstance(record, Transaction):
        return await void(session, record, by=by, now=now)
    await _lock(session, record)
    if not is_open(record):
        raise NotOpen()
    kind = kind_of(record)
    await _with_person(session, record)
    target = (
        PromiseStatus.cancelled if isinstance(record, Promise) else TaskStatus.dropped
    )
    old, new = _note(record, "status", record.status, target, by, now)
    record.status = target
    await session.flush()
    return Change(kind, record, person_of(record), "status", old, new)


async def void(
    session: AsyncSession,
    txn: Transaction,
    *,
    by: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> Change:
    """Take a money row out of every total without deleting it (WP-13).

    Locked and re-read first, so a second tap raises NotOpen and leaves one
    history entry; ``reopen`` undoes it.
    """
    now = now or datetime.now(settings.tz)
    await _lock(session, txn)
    if txn.voided_at is not None:
        raise NotOpen()
    await _with_person(session, txn)
    extra = {"reason": reason} if reason else {}
    old, new = _note(txn, "status", "active", "void", by, now, **extra)
    txn.voided_at = now
    txn.void_reason = reason
    await session.flush()
    return Change("transaction", txn, person_of(txn), "status", old, new)


def _balance_rows(debt: Debt, *, open_only: bool = True) -> sa.Select:
    """Every row of the balance ``debt`` belongs to (person, direction, currency)."""
    stmt = (
        sa.select(Debt)
        .where(Debt.person_id == debt.person_id)
        .where(Debt.direction == debt.direction)
        .where(Debt.currency == debt.currency)
        .options(selectinload(Debt.person))
        .order_by(Debt.id)
    )
    if open_only:
        stmt = stmt.where(Debt.status != DebtStatus.settled)
    return stmt


async def open_in_balance(session: AsyncSession, debt: Debt) -> list[Debt]:
    """The rows of ``debt``'s balance that are still open, locked.

    A "Hali ochiqmi?" line is about a balance, and its buttons are keyed by
    the first row: "open" for that line means any row of the balance is
    open, not the one the button happens to be named after.
    """
    return list(await session.scalars(_balance_rows(debt).with_for_update()))


async def settle_balance(
    session: AsyncSession, debt: Debt, *, by: str, now: datetime | None = None
) -> list[Change]:
    """Settle every open row in the balance ``debt`` belongs to.

    The owner thinks in balances per person — "Akmal's 5 mln" — and the
    "Hali ochiqmi?" question is asked per balance, so its ✅ must settle the
    whole balance (person, direction, currency), not the one row the button
    happened to be named after. Each row gets its own payment and history
    entry, so each can be reopened on its own. Raises NotOpen when none of
    the rows is open any more — the second of two taps included: the rows
    are locked, so the second tap waits for the first commit and finds
    nothing left to settle.
    """
    now = now or datetime.now(settings.tz)
    rows = await open_in_balance(session, debt)
    if not rows:
        raise NotOpen()
    return [await mark_done(session, row, by=by, now=now) for row in rows]


async def reopen(
    session: AsyncSession, record: Record, *, by: str, now: datetime | None = None
) -> Change:
    """Undo a close: ✅ is one tap away on every message, and a mis-tap must
    not be permanent.

    A promise goes back to open and a task to todo, both with ``completed_at``
    cleared, whether they were kept (`/bajarildi`) or dropped (`/yop`). A debt
    loses only the payment `mark_done` wrote to settle it — the one tagged
    ``SETTLED_NOTE`` and named in the history — and its status is re-derived
    from the payments that remain, so a real partial repayment recorded
    earlier survives. Raises AlreadyOpen for a row that is open, and
    NotReopenable for a debt that recorded repayments cover in full — decided
    before anything is deleted or written, so a refusal leaves no trace.
    """
    now = now or datetime.now(settings.tz)
    await _lock(session, record)
    if is_open(record):
        raise AlreadyOpen()
    kind = kind_of(record)
    await _with_person(session, record)

    if isinstance(record, Debt):
        synthetic, real = _split_payments(record, await _payments(session, record))
        if sum((p.amount for p in real), Decimal(0)) >= record.amount:
            raise NotReopenable()
        for payment in synthetic:
            _note(
                record,
                PAYMENT_FIELD,
                payment.amount,
                None,
                by,
                now,
                payment_id=payment.id,
            )
            await session.delete(payment)
        await session.flush()
        old, new = await _restate(session, record, by, now)
        if old is None:  # cannot happen: the real payments are below the amount
            raise NotReopenable()
    elif isinstance(record, Transaction):
        old, new = _note(record, "status", "void", "active", by, now)
        record.voided_at = None
        record.void_reason = None
    elif isinstance(record, Promise):
        old, new = _note(record, "status", record.status, PromiseStatus.open, by, now)
        record.status = PromiseStatus.open
        record.completed_at = None
    else:
        old, new = _note(record, "status", record.status, TaskStatus.todo, by, now)
        record.status = TaskStatus.todo
        record.completed_at = None

    await session.flush()
    return Change(kind, record, person_of(record), "status", old, new)


# --- editing ---------------------------------------------------------------


async def flip(
    session: AsyncSession, debt: Debt, *, by: str, now: datetime | None = None
) -> Change:
    """Swap who owes whom — the most common extraction mistake on a debt."""
    return await set_field(session, debt, "direction", None, by=by, now=now)


async def set_field(
    session: AsyncSession,
    record: Record,
    field: str,
    value: object,
    *,
    by: str,
    now: datetime | None = None,
    create_person: bool = False,
) -> Change:
    """Change one field on one row; the old value goes into the history.

    Refusals come first and leave nothing behind: an amount below the real
    repayments (PaymentsExceed), a currency change with payments on the
    books (PaymentsExist), a person MIYA does not know (UnknownPerson —
    unless ``create_person``, which is the owner's "Ha" to the question).
    """
    now = now or datetime.now(settings.tz)
    kind = kind_of(record)
    if field not in EDITABLE[kind]:
        raise NotEditable(field)
    if isinstance(record, Debt) and field == "direction" and value is not None:
        raise NotEditable(field)  # "kirim"/"chiqim" is a money row's word
    await _lock(session, record)
    await _with_person(session, record)

    if isinstance(record, Transaction):
        if record.voided_at is not None:
            raise NotOpen()  # a voided row is locked until /qaytar
        old, new = await _set_transaction_field(
            session, record, field, value, by, now, create_person
        )
        await session.flush()
        return Change(kind, record, person_of(record), field, old, new)

    if field == "direction":
        debt: Debt = record  # type: ignore[assignment]
        target = (
            DebtDirection.i_owe_them
            if debt.direction is DebtDirection.they_owe_me
            else DebtDirection.they_owe_me
        )
        old, new = _note(debt, field, debt.direction, target, by, now)
        debt.direction = target

    elif field == "amount":
        debt = record  # type: ignore[assignment]
        amount, currency = value  # type: ignore[misc]
        old, new = await _set_amount(session, debt, amount, currency, by, now)

    elif field == "currency":
        debt = record  # type: ignore[assignment]
        if value is not debt.currency and await _payments(session, debt):
            raise PaymentsExist()
        old, new = _note(debt, field, debt.currency, value, by, now)
        debt.currency = value  # type: ignore[assignment]

    elif field == "person":
        person = await _person_named(session, str(value), create=create_person)
        old, new = _note(record, field, record.person_id, person.id, by, now)
        record.person_id = person.id  # type: ignore[union-attr]
        record.person = person  # type: ignore[union-attr]

    else:  # due
        old, new = _note(record, field, record.due_date, value, by, now)
        record.due_date = value  # type: ignore[assignment]

    await session.flush()
    return Change(kind, record, person_of(record), field, old, new)


async def _set_transaction_field(
    session: AsyncSession,
    txn: Transaction,
    field: str,
    value: object,
    by: str,
    now: datetime,
    create_person: bool,
) -> tuple[str | None, str | None]:
    """One field of a money row; every change is a history entry."""
    if field == "amount":
        amount, currency = value  # type: ignore[misc]
        old, new = _note(txn, "amount", txn.amount, amount, by, now)
        txn.amount = amount
        if currency is not None and currency is not txn.currency:
            _note(txn, "currency", txn.currency, currency, by, now)
            txn.currency = currency
        return old, new
    if field == "currency":
        old, new = _note(txn, field, txn.currency, value, by, now)
        txn.currency = value  # type: ignore[assignment]
        return old, new
    if field == "direction":
        if value is None:
            target = (
                TransactionType.expense
                if txn.type is TransactionType.income
                else TransactionType.income
            )
        else:
            target = value  # type: ignore[assignment]
        old, new = _note(txn, field, txn.type, target, by, now)
        txn.type = target
        return old, new
    if field == "person":
        person = await _person_named(session, str(value), create=create_person)
        old, new = _note(txn, field, txn.counterparty_person_id, person.id, by, now)
        txn.counterparty_person_id = person.id
        txn.counterparty = person
        return old, new
    if field == "due":
        if not isinstance(value, date):
            raise NotEditable("due")  # a payment always happened on some day
        local = txn.occurred_at.astimezone(settings.tz)
        moved = datetime.combine(value, local.timetz())
        old, new = _note(txn, "due", txn.occurred_at, moved, by, now)
        txn.occurred_at = moved
        return old, new
    if field == "note":
        text = str(value).strip()[:NOTE_MAX]
        old, new = _note(txn, field, txn.description, text, by, now)
        txn.description = text
        return old, new
    # category
    text = str(value).strip().lower()[:CATEGORY_MAX]
    old, new = _note(txn, field, txn.category, text, by, now)
    txn.category = text
    return old, new


async def _set_amount(
    session: AsyncSession,
    debt: Debt,
    amount: Decimal,
    currency: Currency | None,
    by: str,
    now: datetime,
) -> tuple[str | None, str | None]:
    """`/tuzat d12 3 mln` on a debt that may already carry payments.

    The payment ✅ wrote (``SETTLED_NOTE``) is not money the owner recorded;
    it exists only to make "settled" and "paid" agree. So it follows the
    amount: resized to the new remainder after the real payments, or removed
    when the real payments now cover the debt. Real payments are never
    touched — an amount below them is refused — and every adjustment is a
    history entry naming the payment.
    """
    payments = await _payments(session, debt)
    # The currency first: the payments are in the old one, and comparing a
    # dollar amount against so'm repayments would be meaningless.
    if currency is not None and currency is not debt.currency and payments:
        raise PaymentsExist()
    synthetic, real = _split_payments(debt, payments)
    really_paid = sum((p.amount for p in real), Decimal(0))
    if really_paid > amount:
        raise PaymentsExceed(really_paid, amount, debt.currency)

    old, new = _note(debt, "amount", debt.amount, amount, by, now)
    debt.amount = amount
    if currency is not None and currency is not debt.currency:
        _note(debt, "currency", debt.currency, currency, by, now)
        debt.currency = currency

    remainder = amount - really_paid
    for payment in synthetic:
        if remainder > 0:
            if payment.amount != remainder:
                _note(
                    debt,
                    PAYMENT_FIELD,
                    payment.amount,
                    remainder,
                    by,
                    now,
                    payment_id=payment.id,
                )
                payment.amount = remainder
            remainder = Decimal(0)  # one settling payment is enough
        else:
            _note(
                debt, PAYMENT_FIELD, payment.amount, None, by, now, payment_id=payment.id
            )
            await session.delete(payment)
    await session.flush()
    await _restate(session, debt, by, now)
    return old, new


async def _restate(
    session: AsyncSession, debt: Debt, by: str, now: datetime
) -> tuple[str | None, str | None]:
    """Re-derive a debt's status from its payments, with a history entry.

    Returns the (old, new) status strings when it changed, (None, None) when
    it did not — a status rewrite without an entry would be a silent change.
    """
    paid = await paid_against(session, debt)
    if paid >= debt.amount:
        target = DebtStatus.settled
    elif paid > 0:
        target = DebtStatus.partially_paid
    else:
        target = DebtStatus.open
    if target is debt.status:
        return None, None
    old, new = _note(debt, "status", debt.status, target, by, now)
    debt.status = target
    if target is DebtStatus.settled:
        debt.settled_at = debt.settled_at or now
    else:
        debt.settled_at = None
    return old, new


async def _person_named(session: AsyncSession, name: str, *, create: bool) -> Person:
    """The person the owner named — an existing one when close, else asked.

    A correction is the owner saying "this was Sardor". When MIYA knows a
    Sardor, that is the person (and a new spelling is kept as an alias);
    when it does not, creating one on the spot is exactly the kind of silent
    write the owner said no to — the handler asks, and ``create`` is his
    "Ha". This goes through ``resolve_person`` like every other writer, so
    the advisory lock keeps a concurrent ingestion from creating the same
    Sardor twice.
    """
    person = await resolve_person(session, name, create=create)
    if person is None:
        if not name.strip():  # a blank name; parse_edit never lets one through
            raise NotEditable("person")
        raise UnknownPerson(name)
    return person


# The history field that records an unanswered "Yangi odam yaratilsinmi?".
PENDING_PERSON_FIELD = "pending_person"


def ask_new_person(
    record: Record, name: str, *, by: str, now: datetime | None = None
) -> int:
    """Remember the name the owner typed until he answers the question.

    The answer button has to survive a restart and Telegram caps a callback
    at 64 bytes, so the name lives on the row (in its history, where it is
    also the audit trail of the question) and the button carries only the
    entry's index. Returns that index.
    """
    now = now or datetime.now(settings.tz)
    _note(record, PENDING_PERSON_FIELD, None, None, by, now, name=name)
    return len(record.history) - 1


def pending_person(record: Record, index: int) -> str | None:
    """The name a "Yangi odam …?" button refers to, or None for a stale one."""
    history = record.history or []
    if not 0 <= index < len(history):
        return None
    entry = history[index]
    if entry.get("field") != PENDING_PERSON_FIELD:
        return None
    name = entry.get("name")
    return str(name) if name else None


# --- parsing `/tuzat` arguments ----------------------------------------------

_CURRENCY_WORDS = {
    Currency.UZS: ("so'm", "so’m", "som", "sum", "uzs", "сум", "сўм"),
    Currency.USD: ("$", "usd", "dollar", "dollor", "доллар", "долл"),
    Currency.CNY: ("¥", "cny", "yuan", "юань"),
    Currency.KRW: ("₩", "krw", "won", "von", "вон"),
    Currency.RUB: ("₽", "rub", "rubl", "руб", "рубль", "ruble"),
}
_CURRENCY_OF = {
    word: currency for currency, words in _CURRENCY_WORDS.items() for word in words
}

_SCALE = {
    "mln": 1_000_000,
    "million": 1_000_000,
    "млн": 1_000_000,
    "ming": 1_000,
    "k": 1_000,
    "тыс": 1_000,
}

# The scale token must end the string or be followed by whitespace: "300k" is
# 300 000, but "300 krw" is 300 won, not scale "k" plus a tail "rw".
_AMOUNT = re.compile(
    r"^(?P<lead>[$¥₩₽])?\s*(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?:(?P<scale>mln|million|млн|ming|k|тыс)(?=\s|$))?\s*(?P<tail>[^\d\s]+)?$",
    re.IGNORECASE,
)

# The relative words the owner uses, and that the extractor resolves the
# same way against CURRENT_DATE.
_RELATIVE_DAYS = {"bugun": 0, "ertaga": 1, "indinga": 2, "kecha": -1}
_WEEKDAYS = {
    "dushanba": 0,
    "seshanba": 1,
    "chorshanba": 2,
    "payshanba": 3,
    "juma": 4,
    "shanba": 5,
    "yakshanba": 6,
}
_IN_DAYS = re.compile(r"^(\d{1,3})\s*kun(?:dan keyin)?$", re.IGNORECASE)
_IN_WEEKS = re.compile(r"^(\d{1,2})\s*hafta(?:dan keyin)?$", re.IGNORECASE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Optional field prefixes for when the bare value would be misread —
# "/tuzat d12 kim Juma" is a person, "/tuzat d12 juma" is a date.
_PREFIXES = {
    "kim": "person",
    "odam": "person",
    "summa": "amount",
    "valyuta": "currency",
    "muddat": "due",
    "sana": "due",
    "tomon": "direction",
    "izoh": "note",
    "turkum": "category",
    "kategoriya": "category",
}

# A money row's direction words (WP-13).
_TXN_DIRECTION = {"kirim": TransactionType.income, "chiqim": TransactionType.expense}

CLEAR_DUE = {"muddatsiz", "muddat yo'q", "yo'q", "-"}

# What may be taken for a person's name without a "kim" prefix: it starts
# with a letter and carries no digit or currency sign. "6 mlnn" is a typo'd
# amount, not a new person called "6 mlnn".
_NAME_LIKE = re.compile(r"^[^\W\d_][^\d$¥₩€£₽]*$", re.UNICODE)

# A bare value one keystroke away from a parser keyword is a typo of that
# keyword ("teskarii", "ertga", "muddatsizz"), not a person. It is refused —
# the usage comes back — and a real name that happens to sit next to a
# keyword goes through the "kim" prefix, which skips this check.
KEYWORDS = frozenset(
    {
        "teskari",
        "aksincha",
        "teskarisi",
        "kirim",
        "chiqim",
        "tomon",
        "izoh",
        *CLEAR_DUE,
        *_RELATIVE_DAYS,
        "kun",
        "hafta",
        "oy",
        *_SCALE,
        *_CURRENCY_OF,
        *(c.value.lower() for c in Currency),
    }
)


def looks_like_name(text: str) -> bool:
    return _NAME_LIKE.match(text.strip()) is not None


def looks_like_keyword_typo(text: str) -> bool:
    lowered = text.strip().lower().replace("’", "'")
    return any(
        Levenshtein.distance(lowered, word, score_cutoff=1) <= 1 for word in KEYWORDS
    )


def parse_date(text: str, *, today: date | None = None) -> date | None | bool:
    """ISO, or the relative words; ``None`` means "clear"; ``False`` = not a date.

    The whole value must be the date: "2026-09-20xyz" is not the 20th with
    junk, and the 8-digit form is a phone fragment more often than a date.
    """
    today = today or datetime.now(settings.tz).date()
    lowered = text.strip().lower().replace("’", "'")
    if lowered in CLEAR_DUE:
        return None
    if lowered in _RELATIVE_DAYS:
        return today + timedelta(days=_RELATIVE_DAYS[lowered])
    if lowered in _WEEKDAYS:
        ahead = (_WEEKDAYS[lowered] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    if lowered in ("keyingi hafta", "bir hafta"):
        return today + timedelta(days=7)
    match = _IN_DAYS.match(lowered)
    if match:
        return today + timedelta(days=int(match.group(1)))
    match = _IN_WEEKS.match(lowered)
    if match:
        return today + timedelta(weeks=int(match.group(1)))
    if not _ISO_DATE.match(lowered):
        return False
    try:
        return date.fromisoformat(lowered)
    except ValueError:
        return False


def parse_amount(text: str) -> tuple[Decimal, Currency | None] | None:
    """'5 mln', '5.5 mln so'm', '$300', '300 dollar', '500000' → (Decimal, cur)."""
    match = _AMOUNT.match(text.strip())
    if match is None:
        return None
    lead, number, scale, tail = match.group("lead", "number", "scale", "tail")
    currency = None
    if lead:
        currency = _CURRENCY_OF[lead]
    if tail:
        found = _CURRENCY_OF.get(tail.lower())
        if found is None:
            return None
        currency = found
    try:
        amount = Decimal(number.replace(",", ".")) * _SCALE.get((scale or "").lower(), 1)
    except InvalidOperation:
        return None
    amount = amount.quantize(Decimal("0.01"))
    if amount <= 0:
        return None
    return amount, currency


def parse_edit(text: str, *, today: date | None = None) -> Edit | None:
    """What the owner typed after the ref, as one field change.

    Detection order: an explicit prefix wins; then the direction word, a
    date, an amount, a bare currency; what is left is taken as a name only
    when it looks like one and is not a slip of a keyword. Empty or
    unreadable text is None — the handler shows the usage rather than
    guessing. A prefix with nothing after it ("kim") is None too, not a
    person called "kim".
    """
    raw = (text or "").strip()
    if not raw:
        return None
    head, *rest = raw.split(None, 1)
    prefix = _PREFIXES.get(head.lower())
    if prefix:
        return _parse_as(prefix, rest[0].strip(), today) if rest else None

    lowered = raw.lower()
    if lowered in ("teskari", "aksincha", "teskarisi"):
        return Edit("direction", None)
    if lowered in _TXN_DIRECTION:
        return Edit("direction", _TXN_DIRECTION[lowered])
    when = parse_date(raw, today=today)
    if when is not False:
        return Edit("due", when)
    amount = parse_amount(raw)
    if amount is not None:
        return Edit("amount", amount)
    currency = _CURRENCY_OF.get(lowered)
    if currency is not None:
        return Edit("currency", currency)
    if looks_like_name(raw) and not looks_like_keyword_typo(raw):
        return Edit("person", raw)
    return None


def _parse_as(field: str, value: str, today: date | None) -> Edit | None:
    if field == "person":
        return Edit("person", value) if value else None
    if field == "amount":
        amount = parse_amount(value)
        return Edit("amount", amount) if amount else None
    if field == "currency":
        currency = _CURRENCY_OF.get(value.lower())
        return Edit("currency", currency) if currency else None
    if field == "direction":
        lowered = value.lower()
        if lowered in _TXN_DIRECTION:
            return Edit("direction", _TXN_DIRECTION[lowered])
        if lowered in ("teskari", "aksincha"):
            return Edit("direction", None)
        return None
    if field in ("note", "category"):
        return Edit(field, value) if value else None
    when = parse_date(value, today=today)
    return None if when is False else Edit("due", when)
