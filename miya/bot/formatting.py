"""Uzbek formatting for everything the owner reads.

All owner-facing text in MIYA is Uzbek (spec §14); this module is the single
place that decides how money, dates and names are rendered.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    PromiseMadeBy,
    PromiseStatus,
    TaskPriority,
    TaskStatus,
)

if TYPE_CHECKING:  # annotations only: formatting never imports the services
    from miya.services.loops import (
        QuietCounterparty,
        StaleCommitment,
        UnansweredQuestion,
    )
    from miya.services.queries import DebtBalance

MONTHS_SHORT = [
    "yan",
    "fev",
    "mar",
    "apr",
    "may",
    "iyn",
    "iyl",
    "avg",
    "sen",
    "okt",
    "noy",
    "dek",
]
MONTHS_FULL = [
    "yanvar",
    "fevral",
    "mart",
    "aprel",
    "may",
    "iyun",
    "iyul",
    "avgust",
    "sentabr",
    "oktabr",
    "noyabr",
    "dekabr",
]

CURRENCY_LABEL = {
    Currency.UZS: "so'm",
    Currency.USD: "$",
    Currency.CNY: "¥",
    Currency.KRW: "₩",
    Currency.RUB: "rubl",
}

PRIORITY_LABEL = {
    TaskPriority.low: "past",
    TaskPriority.med: "o'rta",
    TaskPriority.high: "yuqori",
}


def _trim(value: Decimal) -> str:
    """Drop trailing zeros: 5.00 -> '5', 5.50 -> '5.5'."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def money(amount: Decimal, currency: Currency) -> str:
    """Compact Uzbek money.

    Large sums read the way the owner says them — "5 mln so'm", "500 ming so'm".
    Small ones and foreign currencies stay exact, because a rounded USD figure
    is more confusing than a long one.
    """
    amount = Decimal(amount)
    label = CURRENCY_LABEL.get(currency, currency.value)

    if currency is Currency.UZS:
        if amount >= 1_000_000:
            return f"{_trim(amount / 1_000_000)} mln {label}"
        if amount >= 1_000:
            return f"{_trim(amount / 1_000)} ming {label}"
        return f"{_trim(amount)} {label}"

    if currency in (Currency.USD, Currency.CNY, Currency.KRW):
        return f"{label}{_trim(amount)}"
    return f"{_trim(amount)} {label}"


def usd(amount: Decimal) -> str:
    """API spend, where fractions of a cent still matter (`/xarajat`).

    `money()` would round $0.0034 down to "$0" and make a cost report useless,
    so small figures keep four decimals and larger ones keep two.
    """
    amount = Decimal(amount)
    if amount and abs(amount) < Decimal("0.01"):
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def short_date(value: date | None) -> str:
    if value is None:
        return "muddatsiz"
    return f"{value.day}-{MONTHS_SHORT[value.month - 1]}"


def full_date(value: date) -> str:
    return f"{value.day}-{MONTHS_FULL[value.month - 1]} {value.year}"


def clock(value: datetime) -> str:
    return value.astimezone(settings.tz).strftime("%H:%M")


def relative_day(value: date, *, today: date | None = None) -> str:
    """'bugun' / 'ertaga' / '3 kun kechikdi' / a plain date."""
    today = today or datetime.now(settings.tz).date()
    delta = (value - today).days
    if delta == 0:
        return "bugun"
    if delta == 1:
        return "ertaga"
    if delta == 2:
        return "indinga"
    if delta == -1:
        return "kecha"
    if delta < 0:
        return f"{abs(delta)} kun kechikdi"
    if delta <= 7:
        return f"{delta} kundan keyin"
    return short_date(value)


def age_label(delta: timedelta) -> str:
    """'25 daqiqa' / '3 soat' / '2 kun' — how long something has been waiting.

    Lives here, not in replies.py, because the report's data block needs it
    too and reports.py must not import the bot's reply module (that chain
    pulls in the loops engine and the query layer).
    """
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60} daqiqa"
    if seconds < 86400:
        return f"{seconds // 3600} soat"
    return f"{seconds // 86400} kun"


def debt_line(
    person_name: str,
    direction: DebtDirection,
    amount: Decimal,
    currency: Currency,
    due: date | None,
) -> str:
    """One line of a debt list, from the owner's point of view."""
    arrow = "→ senga" if direction is DebtDirection.they_owe_me else "← sen"
    tail = f" · {relative_day(due)}" if due else ""
    return f"{person_name} {arrow}: {money(amount, currency)}{tail}"


# --- record references ------------------------------------------------------
#
# Every listed debt, promise and task carries a short handle — d12, p7, t3 —
# so the owner can name one row in `/bajarildi d12` or `/tuzat p7 ertaga`.
# The letter is the kind, the number is the row's primary key: stable across
# restarts and never reused, unlike a position in a list.

REF_PREFIX = {"debt": "d", "promise": "p", "task": "t"}
KIND_OF_PREFIX = {prefix: kind for kind, prefix in REF_PREFIX.items()}

_REF = re.compile(r"^\s*#?([dpt])(\d{1,9})\s*$", re.IGNORECASE)


def ref(kind: str, record_id: int | None) -> str:
    """'d12' for debt 12. Empty for an unsaved row, so a line never shows 'dNone'."""
    if record_id is None:
        return ""
    return f"{REF_PREFIX[kind]}{record_id}"


def ref_of(record) -> str:
    """The reference of an ORM row, by its table."""
    return ref(kind_of(record), record.id)


def kind_of(record) -> str:
    return {"debts": "debt", "promises": "promise", "tasks": "task"}[record.__tablename__]


def parse_ref(text: str) -> tuple[str, int] | None:
    """'d12' / 'D12' / '#d12' → ('debt', 12); anything else → None."""
    match = _REF.match(text or "")
    if match is None:
        return None
    return KIND_OF_PREFIX[match.group(1).lower()], int(match.group(2))


def tag(kind: str, record_id: int | None) -> str:
    """The reference as it appears at the end of a list line: ' [d12]'."""
    handle = ref(kind, record_id)
    return f" <code>{handle}</code>" if handle else ""


def tags(kind: str, ids: list[int]) -> str:
    """Several rows folded into one line (a debt balance): ' [d12, d15]'."""
    handles = [ref(kind, i) for i in ids if i is not None]
    return f" <code>{', '.join(handles)}</code>" if handles else ""


def escape(text: str) -> str:
    """Neutralise HTML so a contact's name cannot break Telegram parse_mode=HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bullet_list(lines: list[str], *, empty: str) -> str:
    if not lines:
        return empty
    return "\n".join(f"• {line}" for line in lines)


# Telegram rejects messages over 4096 chars; leave margin for the HTML tags.
TELEGRAM_LIMIT = 3900


def clip(text: str, *, limit: int = TELEGRAM_LIMIT) -> str:
    """Hard-cap a message body so one oversized reply can never fail to send.

    Cuts on a line boundary where possible, so an open HTML tag is not split
    mid-entity (all our markup is single-line).
    """
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut] + "\n…<i>(qisqartirildi)</i>"


# --- one record, one line ----------------------------------------------------

_DEBT_STATUS = {
    DebtStatus.settled: " · ✅ yopilgan",
    DebtStatus.partially_paid: " · qisman to'langan",
}
_PROMISE_STATUS = {
    PromiseStatus.done: " · ✅ bajarilgan",
    PromiseStatus.cancelled: " · ✖️ yopilgan",
    PromiseStatus.broken: " · ✖️ buzilgan",
}
_TASK_STATUS = {
    TaskStatus.done: " · ✅ bajarilgan",
    TaskStatus.dropped: " · ✖️ yopilgan",
    TaskStatus.doing: " · jarayonda",
}


def _text(value: str, markup: bool) -> str:
    """A contact's name or a message's words: escaped for Telegram HTML,
    verbatim for a plain-text block (the report's data block escapes the
    whole finished line itself)."""
    return escape(value) if markup else value


def _name(value: str, markup: bool) -> str:
    return f"<b>{escape(value)}</b>" if markup else value


def record_line(kind: str, record, person=None, *, markup: bool = True) -> str:
    """How one debt / promise / task reads on its own, ref first.

    Used for the corrected line after `/tuzat`, the button outcomes, and the
    "Hali ochiqmi?" question — one shape, so the owner learns it once.
    ``markup=False`` is the same line with no HTML tags and no escaping.
    """
    handle = ""
    if record.id is not None:
        handle = ref(kind, record.id)
        handle = f"<code>{handle}</code> " if markup else f"{handle} "
    name = _text(person.display_name, markup) if person is not None else "?"
    if kind == "debt":
        body = debt_line(
            name, record.direction, record.amount, record.currency, record.due_date
        )
        return f"💰 {handle}{body}{_DEBT_STATUS.get(record.status, '')}"
    if kind == "promise":
        who = "Men" if record.made_by is PromiseMadeBy.me else "U"
        due = f" · {relative_day(record.due_date)}" if record.due_date else " · muddatsiz"
        return (
            f"🤝 {handle}{who} — {name}: {_text(record.description, markup)}{due}"
            f"{_PROMISE_STATUS.get(record.status, '')}"
        )
    due = f" · {relative_day(record.due_date)}" if record.due_date else " · muddatsiz"
    status = _TASK_STATUS.get(record.status, "")
    return f"✔️ {handle}{_text(record.description, markup)}{due}{status}"


# --- open loops: one line each ----------------------------------------------
#
# The same three rows render twice: with markup for the owner (the morning
# brief, the nudge) and plain for the report's data block, which the model
# reads and which the fallback report sends as-is. ``markup=False`` means no
# HTML tags and no escaping — the report escapes the finished line itself —
# and the report's explicit words ("javobsiz", "qarzingiz") where the owner's
# line relies on bold and arrows. They live here rather than in replies.py so
# that reports.py can import them without pulling in the bot's reply module.


def quote(text: str, limit: int = 120, *, markup: bool = True) -> str:
    """Someone's words in «…», whitespace folded, cut with an ellipsis."""
    body = " ".join((text or "").split())
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return f"«{_text(body, markup)}»"


def question_line(q: UnansweredQuestion, *, markup: bool = True) -> str:
    """One unanswered question: who, where (a group), how long, the words."""
    where = f" ({_text(q.chat_title, markup)})" if q.is_group and q.chat_title else ""
    tail = f" (+{q.follow_ups} xabar)" if q.follow_ups else ""
    age = f" · {age_label(q.age)}:" if markup else f", {age_label(q.age)} javobsiz:"
    who = _name(q.person_name, markup)
    return f"{who}{where}{age} {quote(q.text, markup=markup)}{tail}"


def stale_line(s: StaleCommitment, *, markup: bool = True) -> str:
    """An undated debt / promise / task and how long nobody moved it."""
    line = record_line(s.record_kind, s.record, s.person, markup=markup)
    if s.record_kind == "debt" and s.outstanding is not None and s.currency is not None:
        # The row's own amount may be partly paid: say what is still owed.
        line = line.replace(
            money(s.record.amount, s.record.currency), money(s.outstanding, s.currency), 1
        )
    return f"{line} · {age_label(s.untouched_for)}dan beri harakat yo'q"


def _quiet_balance(b: DebtBalance, markup: bool) -> str:
    if markup:
        side = "→ senga " if b.direction is DebtDirection.they_owe_me else "← sen "
        return side + money(b.outstanding, b.currency) + tags("debt", b.ids)
    side = "(sizdan qarzi)" if b.direction is DebtDirection.they_owe_me else "(qarzingiz)"
    return f"{money(b.outstanding, b.currency)} {side}"


def quiet_line(q: QuietCounterparty, *, markup: bool = True) -> str:
    """Who went quiet, for how long, and what is open with them."""
    open_items = [_quiet_balance(b, markup) for b in q.balances]
    if q.promises:
        open_items.append(f"{len(q.promises)} ta {'' if markup else 'ochiq '}va'da")
    items = "; ".join(open_items) if open_items else "—"
    if markup:
        return f"{_name(q.person_name, True)} · {q.days_quiet} kun jim: {items}"
    return f"{q.person_name}: {q.days_quiet} kun jim — {items}"
