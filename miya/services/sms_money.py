"""Deterministic parsing of payment/bank SMS (build step 6).

A bank SMS is the bank's own record of money moving — not a counterparty
claim — so it never goes through extraction or the claim gate and never
spends a model token. This module is the whole parser: pure functions, no
session, no I/O, and rules narrow enough that they NEVER guess. A missed
transaction is recoverable (the SMS is stored and /tekshir shows the
low-confidence ones); an invented one poisons /xarajat.

The shape it reads is the one Payme, Click and the Uzbek banks actually
send: one clause names the direction (oplata / postuplenie / ...), the
amount sits in that clause or on a Summa:/Сумма: line, the balance lives
in its own clause behind ostatok/остаток and is *never* the amount, the
card is a ``*1234`` mask and the merchant is a labelled line or the
trailing ALL-CAPS run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from miya.config import settings
from miya.db.enums import Currency, TransactionType
from miya.services.records import _CURRENCY_OF
from miya.services.text import fold_apostrophes

# --- who counts as a bank -----------------------------------------------------
#
# Normalised (casefold, every space and punctuation mark dropped), so
# "Kapital Bank", "KAPITALBANK" and "kapitalbank" are one sender. The short
# numbers are the card processors' own service numbers (8600 = Uzcard,
# 9860 = Humo). Extend per install through PAYMENT_SMS_SENDERS.
DEFAULT_SENDERS: frozenset[str] = frozenset(
    {
        "payme",
        "click",
        "uzum",
        "uzumbank",
        "uzcard",
        "humo",
        "paynet",
        "apelsin",
        "kapitalbank",
        "ipoteka",
        "ipotekabank",
        "asaka",
        "asakabank",
        "hamkorbank",
        "agrobank",
        "infinbank",
        "tbc",
        "tbcbank",
        "anorbank",
        "aloqabank",
        "trastbank",
        "ipakyuli",
        "ipakyulibank",
        "sqb",
        "nbu",
        "xalqbank",
        "davrbank",
        "turonbank",
        "8600",
        "9860",
        "3700",
    }
)

HIGH = "high"
LOW = "low"

# The direction keyword picks the clause the amount must come from. Matched
# as substrings of the casefolded, apostrophe-folded clause, so "to'lovlar"
# and "Оплата:" both count. Expense is checked first within a clause.
EXPENSE_WORDS: tuple[str, ...] = (
    "oplata",
    "оплата",
    "to'lov",
    "pokupka",
    "покупка",
    "xarid",
    "spisanie",
    "списание",
    "snyatie",
    "снятие",
    "chiqim",
    "perevod na",
    "перевод на",
)
INCOME_WORDS: tuple[str, ...] = (
    "postuplenie",
    "поступление",
    "popolnenie",
    "пополнение",
    "zachislenie",
    "зачисление",
    "kirim",
    "tushum",
    "perevod ot",
    "перевод от",
)
# A clause carrying one of these holds the balance, never the amount.
BALANCE_WORDS: tuple[str, ...] = (
    "balans",
    "баланс",
    "ostatok",
    "остаток",
    "qoldiq",
    "dostupno",
    "доступно",
)
# The explicit amount label, when the direction clause has no figure.
AMOUNT_LABELS: tuple[str, ...] = ("summa:", "сумма:")

# merchant keyword → /xarajat category. Matched on whole tokens, never
# substrings: "MAGAZIN" must not become "gaz".
CATEGORY_KEYWORDS: dict[str, str] = {
    "korzinka": "oziq-ovqat",
    "makro": "oziq-ovqat",
    "havas": "oziq-ovqat",
    "taxi": "transport",
    "yandex": "transport",
    "uklon": "transport",
    "gaz": "kommunal",
    "svet": "kommunal",
    "elektr": "kommunal",
    "suv": "kommunal",
    "kommunal": "kommunal",
}
DEFAULT_CATEGORY = "other"

# Tokens an ALL-CAPS run may contain without being a merchant: card brands
# and currency codes ride next to the mask in most bank formats.
_NOT_MERCHANT: frozenset[str] = frozenset(
    {
        "UZS",
        "USD",
        "EUR",
        "RUB",
        "CNY",
        "KRW",
        "UZCARD",
        "HUMO",
        "VISA",
        "MASTERCARD",
        "MIR",
        "SMS",
    }
)

# NBSP and the thin/narrow/figure spaces banks use as thousand separators.
_SPACE_TRANSLATION = dict.fromkeys(map(ord, "    "), " ")

# Clauses: lines, semicolons, and a full stop followed by whitespace or the
# end — a decimal mark is followed by a digit and survives the split.
_CLAUSES = re.compile(r"[;\n]+|\.(?=\s|$)")

# Card mask, dates and times are masked out before the amount search so
# "*1234", "16.09.2026" and "14:30" can never be read as money.
_CARD_MASK = re.compile(r"\d{0,6}\*{1,4}\d{4}")
_DATE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")

_LAST4 = re.compile(r"\*{1,4}(\d{4})(?!\d)")

# A money figure: groups of three behind space/dot/comma/apostrophe
# separators, or a plain run, either with an optional 1-2 digit decimal
# part behind a dot or a comma. Not glued to letters, digits, '№' or '#'
# (a cheque number is not an amount).
_AMOUNT = re.compile(
    r"(?<![\w.,№#])"
    r"(\d{1,3}(?:[ .,']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"(?![\d%])"
)

_MERCHANT_LABEL = re.compile(r"(?:joy|место|merchant)\s*:\s*(.+)", re.IGNORECASE)
# An ALL-CAPS run (Latin or Cyrillic), dots and dashes allowed: KORZINKA.UZ,
# YANDEX.TAXI. At least three characters so stray initials stay out.
_CAPS_RUN = re.compile(r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ0-9.&'\-]{2,}(?:\s+[A-ZА-ЯЁ0-9.&'\-]{2,})*")


@dataclass(slots=True)
class ParsedPayment:
    """One bank SMS, read deterministically.

    ``confidence`` is HIGH only when both the direction and the amount were
    read with certainty; at LOW the ``amount`` is always None and ``type``
    is a placeholder — the row goes to /tekshir, nothing is written.
    """

    type: TransactionType
    amount: Decimal | None
    currency: Currency
    card_last4: str | None
    merchant: str | None
    balance_after: Decimal | None
    sender: str
    confidence: str


def normalise_sender(sender: str) -> str:
    """Casefold and drop everything that is not a letter or a digit."""
    return "".join(ch for ch in (sender or "").casefold() if ch.isalnum())


def known_senders() -> frozenset[str]:
    """The built-in senders plus PAYMENT_SMS_SENDERS, normalised the same way."""
    extra = {
        normalise_sender(part)
        for part in re.split(r"[,\s]+", settings.payment_sms_senders)
    }
    return DEFAULT_SENDERS | {s for s in extra if s}


def is_payment_sender(sender: str) -> bool:
    return normalise_sender(sender) in known_senders()


def category_of(merchant: str | None, body: str = "") -> str:
    """The /xarajat category for a payment, from whole-token keywords."""
    tokens = {
        token
        for token in re.split(r"[\W_]+", f"{merchant or ''} {body}".casefold())
        if token
    }
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in tokens:
            return category
    return DEFAULT_CATEGORY


def _to_decimal(text: str) -> Decimal | None:
    """'25 000' / '1.250.000' / '50000.00' / '1 250 000,50' → Decimal, or None.

    With both marks present the last one is the decimal mark; a single mark
    followed by exactly three digits is a thousand separator ("1.250" is one
    thousand two hundred and fifty), anything else a decimal mark.
    """
    cleaned = text.replace(" ", "").replace("'", "")
    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(".") > cleaned.rfind(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        if cleaned.count(",") == 1 and len(tail) != 3:
            cleaned = f"{head}.{tail}"
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        head, _, tail = cleaned.rpartition(".")
        if not (cleaned.count(".") == 1 and len(tail) != 3):
            cleaned = cleaned.replace(".", "")
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _mask(clause: str) -> str:
    """Blank out card masks, dates and times so they cannot become amounts."""
    for pattern in (_CARD_MASK, _DATE, _TIME):
        clause = pattern.sub(" ", clause)
    return clause


def _currency_near(clause: str, start: int, end: int) -> Currency | None:
    """The currency named right next to a figure: '$50', '25 000 сум'."""
    before = clause[:start].rstrip()
    if before and before[-1] in "$¥₩₽":
        return _CURRENCY_OF.get(before[-1])
    tokens = clause[end:].split()
    token = tokens[0].strip(".,:;()!").casefold() if tokens else ""
    return _CURRENCY_OF.get(token) if token else None


def _amount_in(clause: str) -> tuple[Decimal | None, Currency | None]:
    """The first plausible figure in a clause, and its adjacent currency."""
    masked = _mask(clause)
    for match in _AMOUNT.finditer(masked):
        value = _to_decimal(match.group(1))
        if value is None:
            continue
        return value, _currency_near(masked, match.start(1), match.end(1))
    return None, None


def _merchant_of(clauses: list[str], body: str) -> str | None:
    """A labelled merchant line, else the trailing ALL-CAPS run."""
    for clause in clauses:
        match = _MERCHANT_LABEL.search(clause)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    candidate: str | None = None
    for match in _CAPS_RUN.finditer(_CARD_MASK.sub(" ", body)):
        kept = []
        for token in match.group(0).split():
            core = token.strip(".&'-")
            # A digit-only token next to a caps name is an amount, not a
            # part of the merchant; card brands and currency codes drop too.
            if not core or core.isdigit() or core.upper() in _NOT_MERCHANT:
                continue
            kept.append(token)
        if not any(sum(ch.isalpha() for ch in t) >= 2 for t in kept):
            continue
        candidate = " ".join(kept).rstrip(".&'-") or None
    return candidate


def parse(sender: str, body: str) -> ParsedPayment | None:
    """One SMS in, one deterministic reading out.

    None for a sender that is not a bank. LOW (amount None) when the sender
    matched but the direction or the amount could not be read with
    certainty — including when the only figure sits in a balance clause.
    """
    if not is_payment_sender(sender):
        return None

    text = fold_apostrophes(body or "").translate(_SPACE_TRANSLATION)
    clauses = [c.strip() for c in _CLAUSES.split(text) if c and c.strip()]
    folded = [c.casefold() for c in clauses]
    barred = [any(word in low for word in BALANCE_WORDS) for low in folded]

    txn_type: TransactionType | None = None
    direction_idx: int | None = None
    for i, low in enumerate(folded):
        if any(word in low for word in EXPENSE_WORDS):
            txn_type, direction_idx = TransactionType.expense, i
            break
        if any(word in low for word in INCOME_WORDS):
            txn_type, direction_idx = TransactionType.income, i
            break

    balance_after: Decimal | None = None
    for i, clause in enumerate(clauses):
        if barred[i]:
            balance_after, _ = _amount_in(clause)
            if balance_after is not None:
                break

    amount: Decimal | None = None
    currency: Currency | None = None
    if direction_idx is not None and not barred[direction_idx]:
        amount, currency = _amount_in(clauses[direction_idx])
    if amount is None:
        for i, low in enumerate(folded):
            if barred[i] or not any(label in low for label in AMOUNT_LABELS):
                continue
            amount, currency = _amount_in(clauses[i])
            if amount is not None:
                break

    # The LAST star-adjacent run: "UZCARD *8600**1234" ends in the card,
    # not the 8600 BIN it happens to start with.
    card_matches = list(_LAST4.finditer(text))
    card_last4 = card_matches[-1].group(1) if card_matches else None
    merchant = _merchant_of(clauses, text)

    if txn_type is not None and amount is not None:
        return ParsedPayment(
            type=txn_type,
            amount=amount,
            currency=currency or Currency.UZS,
            card_last4=card_last4,
            merchant=merchant,
            balance_after=balance_after,
            sender=sender,
            confidence=HIGH,
        )
    return ParsedPayment(
        # A placeholder at LOW: nothing reads it, nothing is written from it.
        type=txn_type or TransactionType.expense,
        amount=None,
        currency=currency or Currency.UZS,
        card_last4=card_last4,
        merchant=merchant,
        balance_after=balance_after,
        sender=sender,
        confidence=LOW,
    )
