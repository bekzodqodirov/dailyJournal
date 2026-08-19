"""Uzbek formatting for everything the owner reads.

All owner-facing text in MIYA is Uzbek (spec §14); this module is the single
place that decides how money, dates and names are rendered.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from miya.config import settings
from miya.db.enums import Currency, DebtDirection, TaskPriority

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
