"""Ask before writing what a counterparty asserts (build step 3).

The owner's decision (docs/owner-decisions.md, "Counterparty claims"): when
someone *else* says "you owe me" or "I paid you back", ask first, never write
silently. The extractor marks every debt, settlement, promise, transaction and
fulfilment with who said it (``asserted_by``); this module decides which of
those are claims, parks them as ``Claim`` rows, and turns the owner's answer
into the row the extraction would have written — or into nothing.

The gate (``is_claim``): with ``asserted_by == "them"``,

* any debt, any settlement, any transaction is a claim — money a counterparty
  asserts is always asked;
* a promise is a claim when ``made_by == "me"`` — the counterparty says the
  owner committed to something;
* a fulfilment is a claim when ``made_by == "them"`` — the counterparty says
  they did their part, which would close a promise on their word.

A counterparty's own commitment (promise ``made_by == "them"``) and a
counterparty acknowledging the owner did his part (fulfilment ``made_by ==
"me"``) are written straight away: opening a promise is safe, closing one on
their word is not. Events, tasks, facts and the summary never pass here.

Accepting goes through the very writer ``persistence.apply_extraction`` uses,
under a row lock, so a double tap on ✅ cannot write the debt twice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Currency, DebtDirection, PromiseMadeBy, TransactionType
from miya.db.models import (
    Claim,
    Debt,
    DebtPayment,
    Interaction,
    Person,
    Transaction,
)
from miya.services import codes as client_codes
from miya.services import people, persistence, records
from miya.services.extraction import (
    ExtractedDebt,
    ExtractedFulfilment,
    ExtractedPromise,
    ExtractedSettlement,
    ExtractedTransaction,
)

log = logging.getLogger(__name__)

KIND_DEBT = "debt"
KIND_SETTLEMENT = "settlement"
KIND_TRANSACTION = "transaction"
KIND_PROMISE = "promise"
KIND_FULFILMENT = "fulfilment"

PENDING = "pending"
ACCEPTED = "accepted"
DECLINED = "declined"
AUTO = "auto"
BY_AUTO_DUPLICATE = "auto:duplicate"
BY_AUTO_OWN = "auto:own"
MONEY_KINDS = ("debt", "settlement", "transaction")

# Who answered — the claim's ``answered_by`` and the "by" of the history entry
# the accepted row gets. The same words as records.BY_BUTTON / BY_COMMAND.
BY_BUTTON = records.BY_BUTTON
BY_COMMAND = records.BY_COMMAND

# The pydantic item each kind of claim carries in ``payload``.
ITEM_OF: dict[str, type[BaseModel]] = {
    KIND_DEBT: ExtractedDebt,
    KIND_SETTLEMENT: ExtractedSettlement,
    KIND_TRANSACTION: ExtractedTransaction,
    KIND_PROMISE: ExtractedPromise,
    KIND_FULFILMENT: ExtractedFulfilment,
}

# Which `/tuzat` fields each kind of claim has a use for. A fulfilment is a
# hint about a promise: only the name can be wrong. A settlement has no due
# date; a promise has no amount.
EDITABLE: dict[str, tuple[str, ...]] = {
    KIND_DEBT: ("person", "amount", "currency", "due", "direction"),
    KIND_SETTLEMENT: ("person", "amount", "currency", "direction"),
    KIND_TRANSACTION: ("person", "amount", "currency"),
    KIND_PROMISE: ("person", "due"),
    KIND_FULFILMENT: ("person",),
}

_REF = re.compile(r"^\s*#?c(\d{1,9})\s*$", re.IGNORECASE)


class AlreadyAnswered(Exception):
    """accept / decline / edit on a claim that is no longer pending."""


class UnknownCode(ValueError):
    """`/tuzat c5 GS999`: a client code nobody holds (WP-31)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"client code not assigned: {code}")
        self.code = code


# --- the gate ----------------------------------------------------------------


def is_claim(kind: str, item) -> bool:
    """Is this extracted item something to ask about rather than write?"""
    if getattr(item, "asserted_by", "me") != "them":
        return False
    if kind in (KIND_DEBT, KIND_SETTLEMENT, KIND_TRANSACTION):
        return True
    if kind == KIND_PROMISE:
        return getattr(item, "made_by", None) == "me"
    if kind == KIND_FULFILMENT:
        return getattr(item, "made_by", None) == "them"
    return False


# --- references --------------------------------------------------------------


def ref(claim_id: int) -> str:
    """'c12' for claim 12 — the handle the buttons and `/tuzat c12` use."""
    return f"c{claim_id}"


def parse_ref(handle: str) -> int | None:
    """'c12' / 'C12' / '#c12' → 12; anything else → None."""
    match = _REF.match(handle or "")
    if match is None:
        return None
    return int(match.group(1))


# --- creating and finding ------------------------------------------------------


def _name_of(item) -> str:
    return getattr(item, "person", None) or getattr(item, "counterparty", None) or ""


async def create(
    session: AsyncSession,
    interaction: Interaction,
    kind: str,
    item: BaseModel,
    *,
    person_id: int | None = None,
    now: datetime | None = None,
) -> Claim:
    """Park one extracted item as a pending claim. Added and flushed, not committed."""
    if kind not in ITEM_OF:
        raise ValueError(f"unknown claim kind {kind!r}")
    claim = Claim(
        interaction_id=interaction.id,
        person_id=person_id,
        person_name=_name_of(item),
        kind=kind,
        payload=item.model_dump(mode="json"),
        state=PENDING,
        history=[],
    )
    if now is not None:
        claim.created_at = now
    session.add(claim)
    await session.flush()
    primary = await _primary_for(session, claim, now or datetime.now(settings.tz))
    if primary is not None:
        # The same thing said again: linked to the first, never asked twice.
        claim.duplicate_of = primary.id
        await session.flush()
    return claim


# --- claims that answer themselves (WP-43) -------------------------------------


def _is_automatic(claim: Claim) -> bool:
    """Automatic rules leave alone what the owner undid (WP-45) and what the
    extractor could not attribute (WP-71)."""
    if (claim.payload or {}).get("origin") == "ambiguous":
        return False
    return not any(h.get("field") == "auto_undone" for h in claim.history or [])


def _person_key(claim: Claim):
    return claim.person_id or people.normalise(claim.person_name or "")


def signature(claim: Claim) -> tuple | None:
    """What makes two claims the same claim."""
    payload = claim.payload or {}
    if claim.kind in MONEY_KINDS:
        amount = _money(payload.get("amount"))
        if amount is None:
            return None
        return (
            claim.kind,
            _person_key(claim),
            amount,
            payload.get("currency"),
            payload.get("direction") or payload.get("type"),
        )
    description = people.normalise(payload.get("description") or "")
    if not description:
        return None
    return (claim.kind, _person_key(claim), description)


async def _primary_for(session: AsyncSession, claim: Claim, now: datetime):
    if not _is_automatic(claim):
        return None
    mine = signature(claim)
    if mine is None:
        return None
    since = now - timedelta(days=settings.claim_duplicate_days)
    candidates = await session.scalars(
        sa.select(Claim)
        .where(
            Claim.state == PENDING,
            Claim.kind == claim.kind,
            Claim.duplicate_of.is_(None),
            Claim.id != claim.id,
            Claim.created_at >= since,
        )
        .order_by(Claim.created_at, Claim.id)
    )
    for other in candidates:
        if _is_automatic(other) and signature(other) == mine:
            return other
    return None


async def collapse_duplicates(session: AsyncSession, *, now: datetime) -> int:
    """The same link as ``create`` makes, as a sweep over older claims."""
    linked = 0
    rows = list(
        await session.scalars(
            _pending_stmt().where(
                Claim.created_at >= now - timedelta(days=settings.claim_duplicate_days)
            )
        )
    )
    for claim in rows:
        if claim.duplicate_of is not None:
            continue
        primary = await _primary_for(session, claim, now)
        if primary is not None and primary.created_at <= claim.created_at:
            claim.duplicate_of = primary.id
            linked += 1
    await session.flush()
    return linked


async def duplicates_of(session: AsyncSession, primary_id: int) -> list[Claim]:
    return list(
        await session.scalars(
            sa.select(Claim)
            .where(Claim.duplicate_of == primary_id, Claim.state == PENDING)
            .with_for_update()
        )
    )


async def duplicate_counts(session: AsyncSession, ids) -> dict[int, int]:
    ids = list(ids)
    if not ids:
        return {}
    rows = await session.execute(
        sa.select(Claim.duplicate_of, sa.func.count(Claim.id))
        .where(Claim.duplicate_of.in_(ids), Claim.state == PENDING)
        .group_by(Claim.duplicate_of)
    )
    return dict(rows.all())


async def _settle_duplicates(
    session: AsyncSession, primary: Claim, *, state: str, now: datetime
) -> None:
    for dup in await duplicates_of(session, primary.id):
        dup.state = state
        dup.answered_by = BY_AUTO_DUPLICATE
        dup.answered_at = now
        if state == AUTO:
            dup.result_kind = primary.result_kind
            dup.result_id = primary.result_id


def _auto_note(claim: Claim, handle: str, now: datetime) -> None:
    claim.history = [
        *(claim.history or []),
        {
            "at": now.isoformat(),
            "field": "auto",
            "old": None,
            "new": handle,
            "by": BY_AUTO_OWN,
        },
    ]


async def _claimed(session: AsyncSession, kind: str, row_id) -> bool:
    return bool(
        await session.scalar(
            sa.select(
                sa.exists().where(Claim.result_kind == kind, Claim.result_id == row_id)
            )
        )
    )


async def _claim_person_id(session: AsyncSession, claim: Claim) -> int | None:
    if claim.person_id:
        return claim.person_id
    match = await people.find_person(session, claim.person_name or "")
    if (
        match.person is None
        or match.ambiguous
        or match.score < people.MATCH_THRESHOLD
        or (
            match.runner_up is not None
            and match.runner_up_score >= people.MATCH_THRESHOLD
        )
    ):
        return None
    return match.person.id


async def _owners_row(session: AsyncSession, claim: Claim, person_id: int, since):
    """(result kind, row id, ref) of the owner's own matching row, if any."""
    payload = claim.payload or {}
    amount = _money(payload.get("amount"))
    currency = payload.get("currency") or "UZS"
    if amount is None:
        return None
    if claim.kind == KIND_DEBT:
        rows = await session.scalars(
            sa.select(Debt)
            .where(
                Debt.person_id == person_id,
                Debt.direction == DebtDirection(payload.get("direction")),
                Debt.currency == Currency(currency),
                Debt.amount == amount,
                Debt.created_at >= since,
            )
            .order_by(Debt.id)
        )
        for debt in rows:
            if not await _claimed(session, "debt", debt.id):
                return "debt", debt.id, f"d{debt.id}"
    elif claim.kind == KIND_SETTLEMENT:
        query = (
            sa.select(DebtPayment)
            .join(Debt, Debt.id == DebtPayment.debt_id)
            .where(
                Debt.person_id == person_id,
                Debt.currency == Currency(currency),
                DebtPayment.amount == amount,
                DebtPayment.paid_at >= since,
            )
            .order_by(DebtPayment.id)
        )
        if payload.get("direction"):
            query = query.where(Debt.direction == DebtDirection(payload["direction"]))
        for payment in await session.scalars(query):
            if not await _claimed(session, "payment", payment.id):
                return "payment", payment.id, f"d{payment.debt_id}"
    elif claim.kind == KIND_TRANSACTION and payload.get("type"):
        rows = await session.scalars(
            sa.select(Transaction)
            .where(
                Transaction.counterparty_person_id == person_id,
                Transaction.type == TransactionType(payload["type"]),
                Transaction.currency == Currency(currency),
                Transaction.amount == amount,
                Transaction.channel.is_(None),
                Transaction.voided_at.is_(None),
                Transaction.created_at >= since,
            )
            .order_by(Transaction.id)
        )
        for txn in rows:
            if not await _claimed(session, "transaction", txn.id):
                return "transaction", txn.id, f"x{txn.id}"
    return None


async def resolve_superseded(session: AsyncSession, *, now: datetime) -> list[Claim]:
    """Pending money claims the owner has already written himself: closed
    without a tap, pointing at his row. Nothing new is written."""
    resolved: list[Claim] = []
    rows = list(
        await session.scalars(
            sa.select(Claim)
            .where(Claim.state == PENDING, Claim.kind.in_(MONEY_KINDS))
            .order_by(Claim.id)
            .with_for_update(skip_locked=True)
        )
    )
    for claim in rows:
        if not _is_automatic(claim):
            continue
        person_id = await _claim_person_id(session, claim)
        if person_id is None:
            continue
        since = claim.created_at - timedelta(days=settings.claim_duplicate_days)
        found = await _owners_row(session, claim, person_id, since)
        if found is None:
            continue
        kind, row_id, handle = found
        claim.state = AUTO
        claim.answered_by = BY_AUTO_OWN
        claim.answered_at = now
        claim.result_kind, claim.result_id = kind, row_id
        _auto_note(claim, handle, now)
        resolved.append(claim)
    await session.flush()
    return resolved


def _pending_stmt() -> sa.Select:
    # A duplicate is answered through its primary, never asked on its own.
    return (
        sa.select(Claim)
        .where(Claim.state == PENDING, Claim.duplicate_of.is_(None))
        .order_by(Claim.created_at, Claim.id)
    )


async def pending_count(session: AsyncSession) -> int:
    """How many claims wait for an answer — uncapped, for the report."""
    return int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Claim)
            .where(Claim.state == PENDING, Claim.duplicate_of.is_(None))
        )
        or 0
    )


async def pending(session: AsyncSession, *, limit: int = 50) -> list[Claim]:
    """Every unanswered claim, oldest first — the `/davolar` list and the brief."""
    return list(await session.scalars(_pending_stmt().limit(limit)))


async def pending_for(session: AsyncSession, interaction_id: int) -> list[Claim]:
    """The unanswered claims one interaction produced, for its receipt's buttons."""
    return list(
        await session.scalars(
            _pending_stmt().where(Claim.interaction_id == interaction_id)
        )
    )


async def unasked(
    session: AsyncSession,
    *,
    older_than: timedelta,
    limit: int = 5,
    now: datetime | None = None,
) -> list[Claim]:
    """Pending claims no message has shown yet, once they are old enough.

    A receipt normally carries the question; when no receipt went out (the
    batch path, a failed send) the worker asks these one by one.
    """
    now = now or datetime.now(settings.tz)
    return list(
        await session.scalars(
            _pending_stmt()
            .where(Claim.asked_at.is_(None))
            .where(Claim.created_at <= now - older_than)
            .limit(limit)
        )
    )


async def askable(
    session: AsyncSession, *, now: datetime | None = None, for_push: bool = True
) -> list[Claim]:
    """Pending claims the question queue may put in front of the owner (WP-17).

    Pull (``for_push=False``): every pending claim that is not a repeat.
    Push: only once old enough to have missed its receipt, not shown today
    anywhere (a receipt, /davolar or /savollar stamps ``asked_at`` without a
    question_log row), and within the re-offer rule — never offered, offered
    once before today, or last offered a week ago (LOOP_UNDATED_DAYS, the
    owner's "re-remind after one week").
    """
    from miya.services import questions, reminders  # the queue imports this module

    now = now or datetime.now(settings.tz)
    stmt = _pending_stmt().where(Claim.duplicate_of.is_(None))
    if not for_push:
        return list(await session.scalars(stmt))
    today = reminders.day_start(now)
    rows = list(
        await session.scalars(
            stmt.where(
                Claim.created_at
                <= now - timedelta(minutes=settings.claim_ask_after_minutes)
            ).where(sa.or_(Claim.asked_at.is_(None), Claim.asked_at < today))
        )
    )
    offers = await questions.offers_of(
        session, questions.KIND_CLAIM, [ref(c.id) for c in rows]
    )
    week = timedelta(days=settings.loop_undated_days)
    eligible = []
    for claim in rows:
        count, last = offers.get(ref(claim.id), (0, None))
        if (
            count == 0
            or (count == 1 and last < today)
            or (last is not None and last <= now - week)
        ):
            eligible.append(claim)
    return eligible


def mark_asked(claim: Claim, *, now: datetime | None = None) -> None:
    """Record that the question was shown. Only after it really was."""
    if claim.asked_at is None:
        claim.asked_at = now or datetime.now(settings.tz)


async def get(
    session: AsyncSession, claim_id: int, *, for_update: bool = False
) -> Claim | None:
    stmt = sa.select(Claim).where(Claim.id == claim_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


# --- reading a claim -----------------------------------------------------------


def item_of(claim: Claim) -> BaseModel:
    """The pydantic item back from the payload, edits included."""
    return ITEM_OF[claim.kind].model_validate(claim.payload)


@dataclass(slots=True)
class ClaimView:
    """A claim as plain fields, for rendering; nothing here can raise."""

    id: int
    kind: str
    person_name: str
    amount: Decimal | None
    currency: Currency | None
    direction: DebtDirection | None
    description: str
    due: date | None
    made_by: PromiseMadeBy | None
    state: str
    # income / expense for a transaction, so the question can say kirim / chiqim.
    txn_type: str | None = None
    # How many later claims repeat this one (WP-43).
    repeats: int = 0


def _money(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _enum(kind: type, value: object):
    try:
        return kind(value)
    except (ValueError, TypeError):
        return None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def view(claim: Claim) -> ClaimView:
    """Plain fields for a question line. Tolerates a partial or broken payload."""
    payload: dict[str, Any] = claim.payload if isinstance(claim.payload, dict) else {}
    kind = claim.kind
    money = kind in (KIND_DEBT, KIND_SETTLEMENT, KIND_TRANSACTION)
    if kind == KIND_DEBT:
        text = payload.get("reason")
    elif kind == KIND_SETTLEMENT:
        text = payload.get("note")
    else:
        text = payload.get("description")
    return ClaimView(
        id=claim.id,
        kind=kind,
        person_name=claim.person_name or _name_of_payload(payload),
        amount=_money(payload.get("amount")) if money else None,
        currency=_enum(Currency, payload.get("currency")) if money else None,
        direction=(
            _enum(DebtDirection, payload.get("direction"))
            if kind in (KIND_DEBT, KIND_SETTLEMENT)
            else None
        ),
        description=text.strip() if isinstance(text, str) else "",
        due=_date(payload.get("due_date")) if kind in (KIND_DEBT, KIND_PROMISE) else None,
        made_by=(
            _enum(PromiseMadeBy, payload.get("made_by"))
            if kind in (KIND_PROMISE, KIND_FULFILMENT)
            else None
        ),
        state=claim.state or PENDING,
        txn_type=_txn_type(payload) if kind == KIND_TRANSACTION else None,
    )


def _txn_type(payload: dict[str, Any]) -> str | None:
    value = payload.get("type")
    return value if value in ("income", "expense") else None


def _name_of_payload(payload: dict[str, Any]) -> str:
    name = payload.get("person") or payload.get("counterparty")
    return name if isinstance(name, str) else ""


# --- answering -----------------------------------------------------------------


@dataclass(slots=True)
class Accepted:
    """The answered claim and what writing its one item produced.

    ``written`` is False when the item became only a question (a repayment
    matching no open debt, a hint closing no promise): the claim then stays
    pending, so the owner's "Ha" is not spent on nothing.
    """

    claim: Claim
    applied: persistence.Applied
    written: bool = True


async def _locked_pending(session: AsyncSession, claim_id: int) -> Claim | None:
    """The claim under a row lock; AlreadyAnswered once it is not pending.

    The lock is what makes a second tap harmless: it waits for the first
    commit, re-reads the state and finds the claim answered.
    """
    claim = await get(session, claim_id, for_update=True)
    if claim is None:
        return None
    if claim.state != PENDING:
        raise AlreadyAnswered(claim.state)
    return claim


async def accept(
    session: AsyncSession,
    claim_id: int,
    *,
    by: str,
    now: datetime | None = None,
) -> Accepted | None:
    """ "Ha": write the item as the extraction would have, and close the claim.

    Returns None for a claim that no longer exists (its interaction was
    purged). Commits nothing: the caller owns the transaction. The claim's
    ``result_kind``/``result_id`` are set by the writer; they stay empty
    when the item produced only a question (a settlement matching no debt).
    """
    now = now or datetime.now(settings.tz)
    claim = await _locked_pending(session, claim_id)
    if claim is None:
        return None
    if claim.duplicate_of is not None:
        # "Ha" on a repeat is "Ha" on the first time it was said.
        return await accept(session, claim.duplicate_of, by=by, now=now)
    interaction = await session.get(Interaction, claim.interaction_id)
    if interaction is None:  # the FK cascades, so this is a race at most
        return None
    item = item_of(claim)

    claim.state = ACCEPTED
    claim.answered_at = now
    claim.answered_by = by

    # The person the question showed (WP-32); a purged one leaves NULL and
    # the name is resolved as on the day.
    hint = await session.get(Person, claim.person_id) if claim.person_id else None
    applied = persistence.Applied()
    if claim.kind == KIND_DEBT:
        row = await persistence.write_debt(
            session, interaction, item, applied, now=now, claim=claim, person_hint=hint
        )
    elif claim.kind == KIND_SETTLEMENT:
        row = await persistence.write_settlement(
            session, interaction, item, applied, now=now, claim=claim, person_hint=hint
        )
    elif claim.kind == KIND_TRANSACTION:
        row = await persistence.write_transaction(
            session, interaction, item, applied, now=now, claim=claim, person_hint=hint
        )
    elif claim.kind == KIND_PROMISE:
        row = await persistence.write_promise(
            session, interaction, item, applied, now=now, claim=claim, person_hint=hint
        )
    else:
        row = await persistence.write_fulfilment(
            session, interaction, item, applied, now=now, claim=claim, person_hint=hint
        )
    if row is None and applied.is_empty():
        # The writer refused the item the way it would have refused it on
        # the day (a blank name, a non-positive amount). Said, not hidden.
        log.warning("accepted claim %s produced nothing: %r", claim.id, claim.payload)
    written = claim.result_kind is not None or applied.is_empty()
    if not written:
        # Only a question came out (no open debt for the repayment, no
        # promise for the hint). Usually the debt or promise is itself a
        # claim he has not answered yet; keep this one open for after.
        claim.state = PENDING
        claim.answered_at = None
        claim.answered_by = None
    else:
        await _settle_duplicates(session, claim, state=AUTO, now=now)
    await session.flush()
    return Accepted(claim, applied, written=written)


async def decline(
    session: AsyncSession,
    claim_id: int,
    *,
    by: str,
    now: datetime | None = None,
) -> Claim | None:
    """ "Yo'q": close the claim; nothing is written anywhere else."""
    now = now or datetime.now(settings.tz)
    claim = await _locked_pending(session, claim_id)
    if claim is None:
        return None
    if claim.duplicate_of is not None:
        return await decline(session, claim.duplicate_of, by=by, now=now)
    claim.state = DECLINED
    claim.answered_at = now
    claim.answered_by = by
    await _settle_duplicates(session, claim, state=DECLINED, now=now)
    await session.flush()
    return claim


# --- editing before the answer ---------------------------------------------------


def _plain(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):  # enum member
        return str(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, int | float) and not isinstance(value, bool):
        # The payload holds JSON numbers; the history reads like a debt's.
        return f"{Decimal(str(value)):.2f}"
    return str(value)


def _set(
    claim: Claim, key: str, new: object, *, field: str, by: str, now: datetime
) -> None:
    """One payload change with its history entry — both rebound, never mutated."""
    payload = dict(claim.payload or {})
    old = payload.get(key)
    payload[key] = new
    claim.payload = payload
    claim.history = [
        *(claim.history or []),
        {
            "at": now.isoformat(),
            "field": field,
            "old": _plain(old),
            "new": _plain(new),
            "by": by,
        },
    ]


async def edit(
    session: AsyncSession,
    claim_id: int,
    change: records.Edit,
    *,
    by: str,
    now: datetime | None = None,
) -> Claim | None:
    """`/tuzat c12 …` before the answer: correct the item, keep it pending.

    ``change`` is what ``records.parse_edit`` yields: person (str), amount
    ((Decimal, Currency | None)), currency (Currency), due (date | None),
    direction (None — a flip). Raises ValueError, with an English message,
    for a field this kind of claim has no use for; the handler turns that
    into the Uzbek refusal.
    """
    now = now or datetime.now(settings.tz)
    claim = await _locked_pending(session, claim_id)
    if claim is None:
        return None
    field = change.field
    if field not in EDITABLE.get(claim.kind, ()):
        raise ValueError(f"a {claim.kind} claim has no {field} to correct")

    if field == "person":
        name = str(change.value or "").strip()
        if not name:
            raise ValueError("a person needs a name")
        key = "counterparty" if claim.kind == KIND_TRANSACTION else "person"
        code = client_codes.canonical_client_code(name)
        if code is not None:
            # A client code is one known person or nobody — never a name.
            holder = await client_codes.holder(session, code)
            if holder is None:
                raise UnknownCode(code)
            _set(claim, key, code, field=field, by=by, now=now)
            claim.person_id = holder.id
            claim.person_name = f"{holder.display_name} ({code})"
        else:
            _set(claim, key, name, field=field, by=by, now=now)
            claim.person_name = name
            # Whoever the window knew is no longer who the claim names; the
            # accept resolves the new name.
            claim.person_id = None

    elif field == "amount":
        amount, currency = change.value  # type: ignore[misc]
        amount = Decimal(amount).quantize(Decimal("0.01"))
        if amount <= 0:
            raise ValueError("an amount must be positive")
        _set(claim, "amount", float(amount), field=field, by=by, now=now)
        if currency is not None:
            _set(
                claim,
                "currency",
                Currency(currency).value,
                field="currency",
                by=by,
                now=now,
            )

    elif field == "currency":
        _set(claim, "currency", Currency(change.value).value, field=field, by=by, now=now)

    elif field == "due":
        value = change.value
        if value is not None and not isinstance(value, date):
            raise ValueError("a due date must be a date or empty")
        _set(
            claim,
            "due_date",
            value.isoformat() if value else None,
            field=field,
            by=by,
            now=now,
        )

    else:  # direction: a flip
        current = _enum(DebtDirection, (claim.payload or {}).get("direction"))
        if current is None:
            raise ValueError("this claim carries no direction to flip")
        target = (
            DebtDirection.i_owe_them
            if current is DebtDirection.they_owe_me
            else DebtDirection.they_owe_me
        )
        _set(claim, "direction", target.value, field=field, by=by, now=now)

    await session.flush()
    return claim
