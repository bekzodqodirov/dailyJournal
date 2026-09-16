"""Open loops: everything that slipped the owner's attention (build step 2).

The reminder sweep (reminders.py) only knows about items that carry a date.
Everything else was invisible: a question he never answered, a promise with
no date, a supplier who went quiet. This module finds those from data the
system already stores — SQL and a few pure rules, no model call anywhere —
and hands them to the morning brief and the report as typed rows, each with
a stable ref and an age.

Four detectors and one aggregate:

  * ``unanswered_questions`` — in a private chat, an incoming message that
    looks like a question, older than LOOP_QUESTION_HOURS, with nothing
    outgoing after it and no "✅ Javob berdim" (``meta.answered``) on it or
    on anything after it. In a group the same, but only for messages the
    userbot flagged ``meta.to_me`` (an @-mention, a reply, or one of the
    owner's aliases in plain text). Nothing older than
    LOOP_QUESTION_MAX_DAYS is looked at: a question that old is not a loop
    any more, and the bound is what lets the scan use the occurred_at
    indexes instead of reading every message ever stored.
  * ``stale_undated`` — open promises, debts and tasks with no due date,
    older than LOOP_UNDATED_DAYS and untouched for that long: no reminder
    sent or answered, no owner correction on the row. This is the ONE
    definition of "undated and stale": reminders.collect_due asks its weekly
    "Hali ochiqmi?" from this same selection.
  * ``quiet_counterparties`` — people with an open debt or promise in either
    direction and no contact of any kind — message, transaction, debt,
    payment or promise — for LOOP_QUIET_DAYS.
  * ``missed_calls`` — a missed or rejected ring (build step 6, uploaded by
    the companion app as a call-log event) that no later contact discharged,
    older than LOOP_MISSED_CALL_MINUTES and younger than
    LOOP_MISSED_CALL_MAX_DAYS. One loop per number.

``open_loops`` runs all four and orders the union by urgency: money at
stake first (larger first), then age.

Refs: a question is ``q<interaction id>``, a commitment is its d/p/t ref, a
quiet person is ``k<person id>``, a missed call is ``m<interaction id>``.
Text comes back raw; the renderer escapes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from miya.bot.formatting import ref as record_ref
from miya.config import settings
from miya.db.enums import (
    ChatType,
    Currency,
    DebtStatus,
    Direction,
    InteractionSource,
    PromiseStatus,
    TaskStatus,
)
from miya.db.models import (
    ChatMonitor,
    Debt,
    DebtPayment,
    Interaction,
    Person,
    Promise,
    ReminderLog,
    Task,
    Transaction,
)
from miya.services import phone_events, queries
from miya.services.queries import DebtBalance
from miya.services.text import fold_apostrophes

# --- what a question looks like -----------------------------------------------
#
# One list, whole-word, case-insensitive, both scripts. Extend it here and
# nowhere else. A trailing "?" counts on its own, and so does a short message
# whose final word carries the Uzbek question particle "-mi" (see
# QUESTION_PARTICLE_FORMS for the rule).
#
# Not in the list, on purpose: "nima" / "нима", "kim" / "ким" and "что". Each
# is a conjunction or a pronoun far more often than a question ("nima bo'lsa
# ham qilaman", "потому что так"), and each false positive is a nudge. They
# count only when the message also ends with "?".
QUESTION_WORDS: tuple[str, ...] = (
    # Uzbek, Latin
    "qachon",
    "qachongacha",
    "qancha",
    "qanchadan",
    "nimaga",
    "nega",
    "qayer",
    "qayerda",
    "qayerga",
    "qayerdan",
    "qaysi",
    "qanday",
    "kimga",
    "nechta",
    "necha",
    "bo'ladimi",
    "mumkinmi",
    "bormi",
    # Uzbek, Cyrillic
    "қачон",
    "қанча",
    "нега",
    "қаер",
    "қаерда",
    "қайси",
    "қандай",
    "нечта",
    "бўладими",
    "мумкинми",
    "борми",
    # Russian
    "когда",
    "сколько",
    "почему",
    "можно",
    "где",
    "кто",
    "какой",
    "какая",
    "какие",
)
QUESTION_SUFFIXES: tuple[str, ...] = ("mi", "ми")
# A final word in "-mi" reads as a question only in a short message — at most
# this many words — and only when the word is written in lower case (a name
# such as "Rahimi" is capitalised) or is one of the everyday forms below.
QUESTION_PARTICLE_MAX_WORDS = 8
QUESTION_PARTICLE_FORMS: frozenset[str] = frozenset(
    {
        # Latin
        "bormi",
        "yo'qmi",
        "mumkinmi",
        "bo'ladimi",
        "bo'ldimi",
        "keldimi",
        "bordimi",
        "ketdimi",
        "tushdimi",
        "yetdimi",
        "oldingizmi",
        "oldizmi",
        "jo'natdingizmi",
        "yubordingizmi",
        "to'ladingizmi",
        "ko'rdingizmi",
        "bildingizmi",
        "eshitdingizmi",
        "gaplashdingizmi",
        "keldingizmi",
        "tayyormi",
        "rostmi",
        "shundaymi",
        "to'g'rimi",
        # Cyrillic
        "борми",
        "йўқми",
        "мумкинми",
        "бўладими",
        "бўлдими",
        "келдими",
        "бордими",
        "кетдими",
        "тушдими",
        "етдими",
        "олдингизми",
        "олдизми",
        "жўнатдингизми",
        "юбордингизми",
        "тўладингизми",
        "кўрдингизми",
        "билдингизми",
        "эшитдингизми",
        "гаплашдингизми",
        "келдингизми",
        "тайёрми",
        "ростми",
        "шундайми",
        "тўғрими",
    }
)

_QUESTION_WORD = re.compile(
    r"(?<![\w'])(?:" + "|".join(re.escape(w) for w in QUESTION_WORDS) + r")(?![\w'])",
    re.IGNORECASE,
)
_LAST_WORD = re.compile(r"([\w']+)\W*$")


# Nouns whose stem ends in -m take the possessive -i and so end in "-mi"
# without asking anything: "uning rasmi", "ikkinchi qismi". Checked before
# the lowercase branch; the allow-list above never contains them.
POSSESSIVE_NOT_QUESTION: frozenset[str] = frozenset(
    {
        "rasmi",
        "qismi",
        "bo'limi",
        "hajmi",
        "yarmi",
        "hokimi",
        "tizimi",
        "hukmi",
        "ilmi",
        "ismi",
        "nomi",
        "jismi",
        "qalami",
        "jami",
        "bayrami",
        "muddatlarmi",
        "расми",
        "қисми",
        "бўлими",
        "ҳажми",
        "ярми",
        "ҳокими",
        "тизими",
        "ҳукми",
        "илми",
        "исми",
        "номи",
        "жисми",
        "қалами",
        "жами",
        "байрами",
    }
)


def _ends_in_question_particle(folded: str) -> bool:
    """The "-mi" rule, on apostrophe-folded text with its case intact."""
    if len(folded.split()) > QUESTION_PARTICLE_MAX_WORDS:
        return False
    last = _LAST_WORD.search(folded)
    if last is None:
        return False
    token = last.group(1)
    word = token.lower()
    if not any(word.endswith(s) and len(word) > len(s) + 2 for s in QUESTION_SUFFIXES):
        return False
    if word in POSSESSIVE_NOT_QUESTION:
        return False
    return token == word or word in QUESTION_PARTICLE_FORMS


def looks_like_question(text: str | None) -> bool:
    """Pure: does this message ask something? Nothing is fetched or scored."""
    if not text:
        return False
    folded = fold_apostrophes(text.strip())
    if folded.endswith("?"):
        return True
    if _QUESTION_WORD.search(folded):
        return True
    return _ends_in_question_particle(folded)


# --- ordering ------------------------------------------------------------------
#
# Loops are ranked by money at stake, and the stake is spread over several
# currencies. These weights fold every currency into a UZS-sized number FOR
# ORDERING ONLY — rough, deliberately, because they decide which line comes
# first and nothing else. No figure derived from them is ever shown: what the
# owner reads is the per-currency outstanding straight from SQL.
RANK_WEIGHT_UZS: dict[Currency, Decimal] = {
    Currency.UZS: Decimal(1),
    Currency.USD: Decimal(12_500),
    Currency.CNY: Decimal(1_750),
    Currency.KRW: Decimal(9),
    Currency.RUB: Decimal(140),
}

# How many unanswered incoming messages per chat are inspected for a question.
QUESTION_SCAN_PER_CHAT = 50

# interactions.metadata key nudges.mark_answered writes: "✅ Javob berdim".
# Read here, written there — the detector must not know how the mark is made.
ANSWERED_KEY = "answered"

KIND_QUESTION = "question"
KIND_UNDATED = "undated"
KIND_QUIET = "quiet"
KIND_MISSED = "missed_call"


def rank_stake(amount: Decimal | None, currency: Currency | None) -> Decimal:
    """The ordering weight of one sum; zero for no money."""
    if amount is None or currency is None:
        return Decimal(0)
    return Decimal(amount) * RANK_WEIGHT_UZS.get(currency, Decimal(1))


@dataclass(slots=True, kw_only=True)
class Loop:
    """What every open loop has: a kind, a stable ref, an age and a rank."""

    kind: str
    ref: str
    age: timedelta
    # Ordering only — see RANK_WEIGHT_UZS. Never render this number.
    stake_rank: Decimal
    person: Person | None

    @property
    def person_name(self) -> str:
        return self.person.display_name if self.person is not None else "?"


@dataclass(slots=True, kw_only=True)
class UnansweredQuestion(Loop):
    """Someone asked; nothing outgoing followed. ``text`` is raw — escape it."""

    interaction_id: int
    tg_chat_id: int
    chat_title: str | None
    is_group: bool
    text: str
    asked_at: datetime
    # Further incoming messages in that chat after the question, none answered.
    follow_ups: int


@dataclass(slots=True, kw_only=True)
class StaleCommitment(Loop):
    """An undated promise, debt or task nobody has touched for a week."""

    record_kind: str  # "debt" | "promise" | "task"
    record: Debt | Promise | Task
    description: str
    created_at: datetime
    last_touched_at: datetime
    untouched_for: timedelta
    # From SQL, for a debt row: what is still owed. None for a promise/task.
    outstanding: Decimal | None
    currency: Currency | None


@dataclass(slots=True, kw_only=True)
class QuietCounterparty(Loop):
    """Money or a promise is open with this person and the thread went cold."""

    last_contact_at: datetime
    days_quiet: int
    balances: list[DebtBalance]
    promises: list[Promise]


@dataclass(slots=True, kw_only=True)
class MissedCall(Loop):
    """Someone rang and nobody has been in touch since (build step 6).

    One per number: ``interaction_id`` and ``called_at`` are the oldest open
    missed call from it, ``attempts`` counts the missed/rejected rings from
    the same number since that one — the follow_ups analogue.
    """

    interaction_id: int
    called_at: datetime
    phone: str | None
    attempts: int


@dataclass(slots=True)
class OpenLoops:
    now: datetime
    questions: list[UnansweredQuestion] = field(default_factory=list)
    missed: list[MissedCall] = field(default_factory=list)
    stale: list[StaleCommitment] = field(default_factory=list)
    quiet: list[QuietCounterparty] = field(default_factory=list)
    # The union, most urgent first: bigger stake, then older.
    ordered: list[Loop] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.ordered


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(settings.tz)


def urgency(loop: Loop) -> tuple[Decimal, timedelta]:
    """Sort key: larger stake first, then older. Negated for ascending sort."""
    return (-loop.stake_rank, -loop.age)


# --- 1. unanswered questions -----------------------------------------------------


def _is_message() -> tuple:
    """Raw userbot messages: not the window summary row (direction ``na``)."""
    return (
        Interaction.source == InteractionSource.telegram_userbot,
        Interaction.tg_chat_id.isnot(None),
    )


def is_answered(interaction: Interaction) -> bool:
    """Pure meta read: did the owner tap "✅ Javob berdim" on this row?"""
    return bool((interaction.meta or {}).get(ANSWERED_KEY))


def question_floor(cutoff: datetime, max_days: int | None = None) -> datetime:
    """The oldest occurred_at the question scan reads: LOOP_QUESTION_MAX_DAYS
    before ``cutoff``. Older messages are not loops, and the bound is what
    lets ix_interactions_occurred_at / ix_interactions_chat_occurred drive
    the scan instead of a walk over every message ever stored."""
    days = settings.loop_question_max_days if max_days is None else max_days
    return cutoff - timedelta(days=days)


def _last_outgoing_per_chat(floor: datetime):
    """When the owner last wrote in each chat, looking back to ``floor``.

    An outgoing message older than the floor cannot matter: every incoming
    candidate is newer than the floor, so it is newer than that reply too.
    """
    return (
        sa.select(
            Interaction.tg_chat_id.label("tg_chat_id"),
            sa.func.max(Interaction.occurred_at).label("at"),
        )
        .where(*_is_message(), Interaction.direction == Direction.out)
        .where(Interaction.occurred_at >= floor)
        .group_by(Interaction.tg_chat_id)
        .subquery("last_out")
    )


def question_candidates(cutoff: datetime, *, max_days: int | None = None) -> sa.Select:
    """The SQL half of unanswered_questions: rows worth reading, per chat.

    Every incoming userbot message older than ``cutoff`` — and no older than
    ``max_days`` (LOOP_QUESTION_MAX_DAYS) before it — with nothing outgoing
    after it in the same chat: the oldest QUESTION_SCAN_PER_CHAT of them per
    chat, with the chat's title and type. Exposed so the plan can be
    inspected (EXPLAIN) without going through the detector.
    """
    floor = question_floor(cutoff, max_days)
    last_out = _last_outgoing_per_chat(floor)
    ranked = (
        sa.select(
            Interaction.id.label("id"),
            sa.func.row_number()
            .over(
                partition_by=Interaction.tg_chat_id,
                order_by=(Interaction.occurred_at, Interaction.id),
            )
            .label("rank"),
        )
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .outerjoin(last_out, last_out.c.tg_chat_id == Interaction.tg_chat_id)
        .where(*_is_message(), Interaction.direction == Direction.in_)
        .where(Interaction.occurred_at >= floor, Interaction.occurred_at <= cutoff)
        .where(sa.or_(last_out.c.at.is_(None), Interaction.occurred_at > last_out.c.at))
        .where(ChatMonitor.monitor_enabled.is_(True))
        .where(
            sa.or_(
                ChatMonitor.chat_type == ChatType.private,
                sa.and_(
                    ChatMonitor.chat_type == ChatType.group,
                    queries._addressed_to_owner(),
                ),
            )
        )
        .subquery("ranked")
    )
    return (
        sa.select(Interaction, ChatMonitor.title, ChatMonitor.chat_type)
        .options(selectinload(Interaction.person))
        .join(ranked, ranked.c.id == Interaction.id)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .where(ranked.c.rank <= QUESTION_SCAN_PER_CHAT)
        .order_by(Interaction.tg_chat_id, Interaction.occurred_at, Interaction.id)
    )


async def unanswered_questions(
    session: AsyncSession, *, now: datetime | None = None, hours: int | None = None
) -> list[UnansweredQuestion]:
    """Questions the owner never answered, oldest neglect first.

    Per chat: every incoming message after the owner's last outgoing one
    (or ever, if he never wrote there), older than ``hours`` and no older
    than LOOP_QUESTION_MAX_DAYS. The oldest of those that reads as a
    question is the loop; later ones are counted as ``follow_ups``. Private
    chats qualify as a whole; in a group only messages flagged ``meta.to_me``
    do, and the owner writing anything in that group afterwards counts as
    having answered. A row he marked answered ("✅ Javob berdim") resets the
    chat exactly like an outgoing reply: everything at or before it is
    handled, and the next question after it is the loop.
    """
    now = _now(now)
    hours = settings.loop_question_hours if hours is None else hours
    rows = await session.execute(question_candidates(now - timedelta(hours=hours)))

    found: dict[int, UnansweredQuestion] = {}
    for interaction, title, chat_type in rows.all():
        chat_id = interaction.tg_chat_id
        if is_answered(interaction):
            found.pop(chat_id, None)
            continue
        if chat_id in found:
            found[chat_id].follow_ups += 1
            continue
        text = interaction.raw_text or interaction.transcript
        if not looks_like_question(text):
            continue
        found[chat_id] = UnansweredQuestion(
            kind=KIND_QUESTION,
            ref=f"q{interaction.id}",
            age=now - interaction.occurred_at,
            stake_rank=Decimal(0),
            person=interaction.person,
            interaction_id=interaction.id,
            tg_chat_id=chat_id,
            chat_title=title,
            is_group=chat_type is ChatType.group,
            text=text,
            asked_at=interaction.occurred_at,
            follow_ups=0,
        )
    return sorted(found.values(), key=lambda q: q.asked_at)


# --- 2. ageing undated commitments -----------------------------------------------


def log_kinds(kind: str) -> tuple[str, str, str]:
    """The reminder_log kinds that count as touching an item.

    The same three reminders.py writes (its ping kind, ask_kind, ack_kind);
    a test keeps the two in step. Spelled here rather than imported because
    reminders imports this module.
    """
    return (kind, f"ask:{kind}", f"ack:{kind}")


def _last_log(kind: str, ref_expr) -> sa.ScalarSelect:
    return (
        sa.select(sa.func.max(ReminderLog.sent_at))
        .where(ReminderLog.kind.in_(log_kinds(kind)), ReminderLog.ref == ref_expr)
        .scalar_subquery()
    )


def _debt_log_ref():
    """reminders.debt_ref() in SQL: 'person:direction:currency'."""
    return (
        sa.cast(Debt.person_id, sa.Text)
        + ":"
        + sa.cast(Debt.direction, sa.Text)
        + ":"
        + sa.cast(Debt.currency, sa.Text)
    )


def last_history_at(record: Debt | Promise | Task) -> datetime | None:
    """When the owner last corrected the row, from its ``history`` list."""
    latest: datetime | None = None
    for entry in record.history or []:
        try:
            at = datetime.fromisoformat(str(entry["at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=settings.tz)
        if latest is None or at > latest:
            latest = at
    return latest


def last_touched(record: Debt | Promise | Task, last_log: datetime | None) -> datetime:
    """The newest of: created, last reminder sent/answered, last correction."""
    stamps = [record.created_at, last_log, last_history_at(record)]
    return max(s for s in stamps if s is not None)


async def _undated_debts(session: AsyncSession, cutoff: datetime):
    return (
        await session.execute(
            sa.select(
                Debt, Person, queries.OUTSTANDING, _last_log("debt", _debt_log_ref())
            )
            .join(Person, Person.id == Debt.person_id)
            .where(Debt.status != DebtStatus.settled)
            .where(Debt.due_date.is_(None))
            .where(Debt.created_at <= cutoff)
            .where(queries.OUTSTANDING > 0)
            .order_by(Debt.created_at, Debt.id)
        )
    ).all()


async def _undated_promises(session: AsyncSession, cutoff: datetime):
    return (
        await session.execute(
            sa.select(Promise, Person, _last_log("promise", sa.cast(Promise.id, sa.Text)))
            .join(Person, Person.id == Promise.person_id)
            .where(Promise.status == PromiseStatus.open)
            .where(Promise.due_date.is_(None))
            .where(Promise.created_at <= cutoff)
            .order_by(Promise.created_at, Promise.id)
        )
    ).all()


async def _undated_tasks(session: AsyncSession, cutoff: datetime):
    return (
        await session.execute(
            sa.select(Task, _last_log("task", sa.cast(Task.id, sa.Text)))
            .where(Task.status.in_([TaskStatus.todo, TaskStatus.doing]))
            .where(Task.due_date.is_(None))
            .where(Task.created_at <= cutoff)
            .order_by(Task.created_at, Task.id)
        )
    ).all()


async def stale_undated(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    days: int | None = None,
    kinds: tuple[str, ...] = ("debt", "promise", "task"),
) -> list[StaleCommitment]:
    """Open, undated, older than ``days`` and untouched for ``days``.

    Untouched means: no reminder_log row (ping, ask or ack) and no history
    entry on the row within the window. Oldest first. ``kinds`` narrows the
    tables — reminders asks about promises and tasks only; the brief shows
    debts too.
    """
    now = _now(now)
    days = settings.loop_undated_days if days is None else days
    cutoff = now - timedelta(days=days)
    out: list[StaleCommitment] = []

    def add(kind, record, person, description, last_log, outstanding=None, cur=None):
        touched = last_touched(record, last_log)
        if touched > cutoff:
            return
        out.append(
            StaleCommitment(
                kind=KIND_UNDATED,
                ref=record_ref(kind, record.id),
                age=now - record.created_at,
                stake_rank=rank_stake(outstanding, cur),
                person=person,
                record_kind=kind,
                record=record,
                description=description,
                created_at=record.created_at,
                last_touched_at=touched,
                untouched_for=now - touched,
                outstanding=outstanding,
                currency=cur,
            )
        )

    if "debt" in kinds:
        for debt, person, outstanding, last_log in await _undated_debts(session, cutoff):
            add(
                "debt",
                debt,
                person,
                debt.reason or "",
                last_log,
                Decimal(outstanding),
                debt.currency,
            )
    if "promise" in kinds:
        for promise, person, last_log in await _undated_promises(session, cutoff):
            add("promise", promise, person, promise.description, last_log)
    if "task" in kinds:
        for task, last_log in await _undated_tasks(session, cutoff):
            add("task", task, None, task.description, last_log)

    out.sort(key=lambda s: (s.created_at, s.ref))
    return out


# --- 3. quiet counterparties -----------------------------------------------------


def _last_of(column, *conditions) -> sa.ScalarSelect:
    return (
        sa.select(sa.func.max(column))
        .where(*conditions)
        .correlate(Person)
        .scalar_subquery()
    )


async def quiet_counterparties(
    session: AsyncSession, *, now: datetime | None = None, days: int | None = None
) -> list[QuietCounterparty]:
    """People something is open with, not heard from for ``days``.

    "Heard from" is the latest of any interaction attached to them (either
    direction, any source), a transaction with them, a debt or promise
    recorded about them, or a payment on one of their debts — the bot has
    no interaction row for a debt or a settlement the owner typed, but the
    row's own timestamp is proof of contact. Quietest first.
    """
    now = _now(now)
    days = settings.loop_quiet_days if days is None else days
    cutoff = now - timedelta(days=days)

    open_debt = sa.and_(Debt.status != DebtStatus.settled, queries.OUTSTANDING > 0)
    has_open = sa.or_(
        sa.exists().where(Debt.person_id == Person.id, open_debt),
        sa.exists().where(
            Promise.person_id == Person.id, Promise.status == PromiseStatus.open
        ),
    )
    # GREATEST skips NULLs in PostgreSQL, and has_open guarantees at least
    # one debt or promise row, so this is never NULL.
    last_contact = sa.func.greatest(
        _last_of(Interaction.occurred_at, Interaction.person_id == Person.id),
        _last_of(
            Transaction.occurred_at, Transaction.counterparty_person_id == Person.id
        ),
        _last_of(Debt.created_at, Debt.person_id == Person.id),
        _last_of(Promise.created_at, Promise.person_id == Person.id),
        _last_of(
            DebtPayment.paid_at,
            DebtPayment.debt_id == Debt.id,
            Debt.person_id == Person.id,
        ),
    )

    rows = (
        await session.execute(
            sa.select(Person, last_contact.label("last_contact"))
            .where(has_open)
            .where(last_contact <= cutoff)
            .order_by(last_contact, Person.id)
        )
    ).all()
    if not rows:
        return []

    balances: dict[int, list[DebtBalance]] = {}
    for balance in await queries.open_debts(session):
        balances.setdefault(balance.person.id, []).append(balance)
    promises: dict[int, list[Promise]] = {}
    for promise, person in await queries.open_promises(session, limit=1000):
        promises.setdefault(person.id, []).append(promise)

    out: list[QuietCounterparty] = []
    for person, last in rows:
        mine = balances.get(person.id, [])
        out.append(
            QuietCounterparty(
                kind=KIND_QUIET,
                ref=f"k{person.id}",
                age=now - last,
                stake_rank=sum(
                    (rank_stake(b.outstanding, b.currency) for b in mine), Decimal(0)
                ),
                person=person,
                last_contact_at=last,
                days_quiet=(now - last).days,
                balances=mine,
                promises=promises.get(person.id, []),
            )
        )
    return out


# --- 4. missed calls (build step 6) -----------------------------------------------

MISSED_CALL_TYPES = ("missed", "rejected")


def _phone_tail(phone: str | None) -> str | None:
    """The last 9 digits, as find_by_phone compares numbers."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if digits else None


def _tail_expr(column):
    """The same 9-digit tail in SQL, on a JSONB ``->> 'phone'`` value."""
    return sa.func.right(sa.func.regexp_replace(column, r"\D", "", "g"), 9)


def _is_missed_call_row(cls=Interaction):
    """A call-log event row for a ring nobody answered.

    Coalesced so a phone_call row with other media (a recording, or none at
    all) is a clean False, never SQL NULL — the predicate is negated below.
    """
    return sa.and_(
        cls.source == InteractionSource.phone_call,
        sa.func.coalesce(cls.media["type"].astext, "") == phone_events.MEDIA_CALL_LOG,
        sa.func.coalesce(cls.media["call_type"].astext, "").in_(MISSED_CALL_TYPES),
    )


def _answered_floor(rows: list[Interaction]) -> datetime | None:
    """When the owner last said "✅ Bog'landim" about this number.

    The mark resets the number the way an outgoing reply resets a chat in
    ``unanswered_questions``: every ring at or before it is handled, and the
    next ring after it opens a new loop.
    """
    floor: datetime | None = None
    for row in rows:
        if not is_answered(row):
            continue
        mark = (row.meta or {}).get(ANSWERED_KEY) or {}
        try:
            at = datetime.fromisoformat(str(mark.get("at")))
        except (TypeError, ValueError):
            at = None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=settings.tz)
        candidate = row.occurred_at if at is None else max(at, row.occurred_at)
        if floor is None or candidate > floor:
            floor = candidate
    return floor


async def missed_calls(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    minutes: int | None = None,
    max_days: int | None = None,
) -> list[MissedCall]:
    """Missed and rejected rings nobody has dealt with, oldest first.

    Note the difference from ``unanswered_questions``, which only an
    outgoing message in that same chat (or the ✅ mark) discharges: a missed
    call is closed by ANY later interaction with the same person, whatever
    its source or direction — a call back, an answered incoming call, a
    Telegram message, an SMS all prove the two were in touch. When the
    number matched nobody, any later phone_call interaction whose
    ``media->>'phone'`` carries the same 9-digit tail closes it instead
    (calling a stranger back still counts). A later missed or rejected ring
    never closes anything — it joins the loop as another attempt. The
    owner's ✅ ("Bog'landim") closes the number like the questions' mark.

    Bounded like ``question_floor``: nothing older than
    LOOP_MISSED_CALL_MAX_DAYS is read — so the occurred_at indexes drive the
    scan — and nothing younger than LOOP_MISSED_CALL_MINUTES is a loop yet;
    a fresh missed call may just mean he is on another call. One loop per
    number: the oldest open ring carries the count.
    """
    now = _now(now)
    minutes = settings.loop_missed_call_minutes if minutes is None else minutes
    max_days = settings.loop_missed_call_max_days if max_days is None else max_days
    cutoff = now - timedelta(minutes=minutes)
    floor = now - timedelta(days=max_days)

    later = aliased(Interaction)
    not_a_missed_ring = sa.not_(_is_missed_call_row(later))
    closed_by_person = sa.exists().where(
        later.person_id == Interaction.person_id,
        later.occurred_at > Interaction.occurred_at,
        not_a_missed_ring,
    )
    closed_by_tail = sa.exists().where(
        later.source == InteractionSource.phone_call,
        later.media["phone"].astext.isnot(None),
        _tail_expr(later.media["phone"].astext)
        == _tail_expr(Interaction.media["phone"].astext),
        later.occurred_at > Interaction.occurred_at,
        not_a_missed_ring,
    )

    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .options(selectinload(Interaction.person))
            .where(_is_missed_call_row())
            .where(Interaction.occurred_at >= floor)
            .where(Interaction.occurred_at <= cutoff)
            .where(
                sa.or_(
                    sa.and_(Interaction.person_id.isnot(None), sa.not_(closed_by_person)),
                    sa.and_(Interaction.person_id.is_(None), sa.not_(closed_by_tail)),
                )
            )
            .order_by(Interaction.occurred_at, Interaction.id)
        )
    )
    if not rows:
        return []

    groups: dict[object, list[Interaction]] = {}
    for row in rows:
        key: object = (
            ("person", row.person_id)
            if row.person_id is not None
            else ("tail", _phone_tail((row.media or {}).get("phone")) or f"i{row.id}")
        )
        groups.setdefault(key, []).append(row)

    # The person's open-debt stake, exactly as quiet_counterparties ranks it.
    stakes: dict[int, Decimal] = {}
    if any(row.person_id is not None for row in rows):
        for balance in await queries.open_debts(session):
            stakes[balance.person.id] = stakes.get(
                balance.person.id, Decimal(0)
            ) + rank_stake(balance.outstanding, balance.currency)

    out: list[MissedCall] = []
    for group in groups.values():
        answered_floor = _answered_floor(group)
        open_rows = [
            row
            for row in group
            if not is_answered(row)
            and (answered_floor is None or row.occurred_at > answered_floor)
        ]
        if not open_rows:
            continue
        oldest = open_rows[0]
        out.append(
            MissedCall(
                kind=KIND_MISSED,
                ref=f"m{oldest.id}",
                age=now - oldest.occurred_at,
                stake_rank=(
                    stakes.get(oldest.person_id, Decimal(0))
                    if oldest.person_id is not None
                    else Decimal(0)
                ),
                person=oldest.person,
                interaction_id=oldest.id,
                called_at=oldest.occurred_at,
                phone=(oldest.media or {}).get("phone"),
                attempts=len(open_rows),
            )
        )
    out.sort(key=lambda m: (m.called_at, m.ref))
    return out


# --- 5. all of it, by urgency -----------------------------------------------------


async def open_loops(session: AsyncSession, now: datetime | None = None) -> OpenLoops:
    """Every open loop, with ``ordered`` ranked by stake then age.

    The stake of a quiet counterparty is everything open with them; of a
    stale debt its outstanding row; of a missed call the caller's own open
    debts. Questions and promises carry none, so they follow the money and
    sort among themselves by age.
    """
    now = _now(now)
    loops = OpenLoops(
        now=now,
        questions=await unanswered_questions(session, now=now),
        missed=await missed_calls(session, now=now),
        stale=await stale_undated(session, now=now),
        quiet=await quiet_counterparties(session, now=now),
    )
    loops.ordered = sorted(
        [*loops.questions, *loops.missed, *loops.stale, *loops.quiet], key=urgency
    )
    return loops
