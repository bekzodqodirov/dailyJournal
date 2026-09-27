"""The amount guard on every assistant answer (WP-58; binding rule 1).

An answer may show a figure only where it came from: outside «…» it must be
one the SQL tools returned (a balance, a total); inside «…» it may also be
one somebody actually said, as the stored messages record it. Anything else
is replaced with ‹summa tekshirilmadi›. A counterparty's words are quotes,
never a ledger.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

AMOUNT_UNVERIFIED = "‹summa tekshirilmadi›"

_SCALE = {
    "mln": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "млн": Decimal(1_000_000),
    "mlrd": Decimal(1_000_000_000),
    "milliard": Decimal(1_000_000_000),
    "ming": Decimal(1_000),
    "тыс": Decimal(1_000),
    "k": Decimal(1_000),
}
_CURRENCY = {
    "usd": "USD",
    "dollar": "USD",
    "$": "USD",
    "доллар": "USD",
    "so'm": "UZS",
    "so‘m": "UZS",
    "som": "UZS",
    "sum": "UZS",
    "uzs": "UZS",
    "сум": "UZS",
    "yuan": "CNY",
    "cny": "CNY",
    "¥": "CNY",
    "юань": "CNY",
    "rubl": "RUB",
    "руб": "RUB",
    "rub": "RUB",
    "won": "KRW",
    "krw": "KRW",
}
_NUMBER = r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_SCALES = "|".join(sorted(map(re.escape, _SCALE), key=len, reverse=True))
_CURRENCIES = "|".join(
    sorted((re.escape(c) for c in _CURRENCY if c not in "$¥"), key=len, reverse=True)
)
MONEY_RE = re.compile(
    rf"(?P<pre>[$¥])\s?(?P<n1>{_NUMBER})(?:\s?(?P<s1>{_SCALES})\b)?"
    rf"|(?<![\w.,])(?P<n2>{_NUMBER})\s?(?P<s2>{_SCALES})?(?:\s?(?P<c2>{_CURRENCIES}))?"
    rf"(?![\w])",
    re.IGNORECASE,
)


def _number(raw: str) -> Decimal | None:
    raw = re.sub(r"[   ]", "", raw)
    if raw.count(",") == 1 and "." not in raw:
        raw = raw.replace(",", ".")
    raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def money_mentions(text: str) -> list[tuple[tuple[int, int], Decimal, str | None]]:
    """Every figure in ``text`` that reads as money: a scale word, a
    currency, or a big enough plain number (a stored 5000000.00)."""
    found = []
    for match in MONEY_RE.finditer(text or ""):
        if match.group("pre"):
            value = _number(match.group("n1"))
            scale = match.group("s1")
            currency = _CURRENCY[match.group("pre")]
        else:
            value = _number(match.group("n2"))
            scale = match.group("s2")
            word = (match.group("c2") or "").lower()
            currency = _CURRENCY.get(word)
            if not scale and not currency:
                # A bare number is money only when it looks like one: a
                # ledger figure (5000000.00) or grouped thousands — never a
                # year (2026) or a phone number (+998…, ten digits or more).
                raw = match.group("n2")
                digits = re.sub(r"\D", "", re.split(r"[.,]", raw)[0])
                grouped = bool(re.search(r"\d[ \u00a0\u202f]\d{3}", raw))
                before = text[match.start() - 1 : match.start()] if match.start() else ""
                if before == "+" or len(digits) >= 10:
                    continue
                if not grouped and len(digits) < 5:
                    continue
        if value is None:
            continue
        if scale:
            value *= _SCALE[scale.lower()]
        found.append((match.span(), value.normalize(), currency))
    return found


def _values(texts) -> set[Decimal]:
    return {value for text in texts for _, value, _ in money_mentions(text)}


def _quoted_spans(answer: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r"«[^»]*»", answer)]


def guard_amounts(answer: str, *, sql_texts, quote_texts) -> str:
    """``answer`` with every unsupported figure replaced."""
    sql = _values(sql_texts)
    quoted = _values(quote_texts) | sql
    quotes = _quoted_spans(answer)
    replaced = 0
    out, last = [], 0
    for (start, end), value, _currency in money_mentions(answer):
        inside = any(q0 <= start and end <= q1 for q0, q1 in quotes)
        allowed = quoted if inside else sql
        if value in allowed:
            continue
        out.append(answer[last:start])
        out.append(AMOUNT_UNVERIFIED)
        last = end
        replaced += 1
    if replaced:
        log.warning("amount guard replaced %d figure(s) in an answer", replaced)
    out.append(answer[last:])
    return "".join(out)
