"""Deterministic reading of payment texts: bank SMS and payment-app pushes.

A bank SMS is the bank's own record of money moving — not a counterparty
claim — so it never goes through extraction or the claim gate and never
spends a model token. This module is the whole reader: pure functions, no
session, no I/O, and rules narrow enough that they NEVER guess. A missed
transaction is recoverable (the text is stored and /tekshir shows it); an
invented one poisons every total.

Every text gets a verdict:

* ``BOOK`` — a completed payment with a direction, an amount and evidence
  that it really happened (a card mask, a balance line or a success word).
  Only this writes a transaction.
* ``REVIEW`` — readable but not certain: declined, reversed, pending, a
  reminder, an advert that names a card, conflicting directions, or no
  evidence. It waits in /tekshir with whatever figures could be read, so
  the owner can book it with one tap.
* ``IGNORE`` — not a payment at all: a one-time code, an advert, an
  information text with neither direction nor amount.

The shape it reads is the one Payme, Click and the Uzbek banks actually
send: a direction word names the clause, the amount sits in that clause or
on a Summa:/Сумма: line, the balance lives in its own clause behind
ostatok/остаток and is *never* the amount, the card is a ``*1234`` mask and
the merchant is a labelled line or the trailing ALL-CAPS run. GS client
codes (GS367) and waybill numbers (YW26-004715) are masked before the amount
search and reported separately.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from miya.config import settings
from miya.db.enums import Currency, TransactionType
from miya.services import codes
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


class Verdict(str, enum.Enum):
    BOOK = "book"
    REVIEW = "review"
    IGNORE = "ignore"


# Stable reason codes (rendered in /tekshir by a later package).
OTP = "otp"
OTP_CONFLICT = "otp_conflict"
DECLINED = "declined"
REVERSAL = "reversal"
PENDING = "pending"
REMINDER = "reminder"
FUTURE_DATE = "future_date"
ADVERT = "advert"
ADVERT_WITH_EVIDENCE = "advert_with_evidence"
INFO = "info"
CONFLICT = "conflict"
NO_DIRECTION = "no_direction"
NO_AMOUNT = "no_amount"
NO_EVIDENCE = "no_evidence"
OK = "ok"

# --- vocabularies -------------------------------------------------------------
#
# Matched on the normalised text (apostrophes folded, casefolded, ё→е), each
# anchored at a word start so a marker never fires inside a name. A marker
# written with a trailing ``\b`` must also end a word: "kod\b" is the code,
# never the surname "Kodirov".

OTP_MARKERS: tuple[str, ...] = (
    r"kod\b",
    r"kodi\b",
    r"kodni\b",
    r"kodingiz\b",
    r"код\b",
    r"кода\b",
    r"коди\b",
    r"code\b",
    "parol",
    "пароль",
    r"otp\b",
    "tasdiqlash kodi",
    "hech kimga",
    "ҳеч кимга",
    "nikomu",
    "никому",
    "ne soobshch",
    "не сообщ",
)
DECLINED_MARKERS: tuple[str, ...] = (
    "rad etil",
    "rad qilin",
    "amalga oshmadi",
    "amalga oshirilmadi",
    "o'tmadi",
    "otmadi",
    "muvaffaqiyatsiz",
    "xatolik",
    "bajarilmadi",
    "yetarli emas",
    "otklon",
    r"otkaz\b",
    "ne proshl",
    "ne proshel",
    "ne vypoln",
    "ne udal",
    "neudach",
    "oshibk",
    "nedostatochno",
    "отклон",
    r"отказ\b",
    "не прошл",
    "не прошел",
    "не выполн",
    "не удал",
    "неудач",
    "ошибк",
    "недостаточно",
    "рад этил",
    "амалга ошмади",
    "ўтмади",
    "муваффақиятсиз",
    "хатолик",
    "етарли эмас",
    "declin",
    "fail",
    "unsuccessful",
    "insufficient",
    "reject",
)
REVERSAL_MARKERS: tuple[str, ...] = (
    "bekor qil",
    "qaytarild",
    "qaytarilg",
    "otmen",
    "vozvrat",
    "vozvrashch",
    "storno",
    "annul",
    "отмен",
    "возврат",
    "возвращ",
    "сторно",
    "аннул",
    "бекор қил",
    "қайтарил",
    "refund",
    "revers",
    "chargeback",
    "cancel",
)
PENDING_MARKERS: tuple[str, ...] = (
    "blokirov",
    r"xold\b",
    r"hold\b",
    "v obrabotk",
    "obrabatyva",
    "ozhida",
    "kutilmoqda",
    "jarayonda",
    "ko'rib chiqil",
    "rezerv",
    "заблокир",
    "блокиров",
    "холд",
    "в обработк",
    "обрабатыва",
    "ожида",
    "резерв",
    "кутилмоқда",
    "жараёнда",
    "pending",
    "processing",
)
REMINDER_MARKERS: tuple[str, ...] = (
    "eslatma",
    "eslatamiz",
    "to'lov sanasi",
    "to'lov muddati",
    "to'lash muddati",
    "muddati o'tgan",
    "to'lang",
    "to'lashni unutmang",
    "yechiladi",
    "yechib olinadi",
    "keyingi to'lov",
    "navbatdagi to'lov",
    "oylik to'lov",
    "muddatli to'lov",
    "nasiya",
    "qarzdorlik",
    "qarzingiz",
    "napomin",
    "oplatite",
    "neobxodimo oplatit",
    "neobhodimo oplatit",
    "sleduyushchiy platezh",
    "ocherednoy platezh",
    "budet spisan",
    "zadolzhenn",
    "prosroch",
    "rassrochk",
    "напомин",
    "оплатите",
    "необходимо оплатить",
    "следующий плат",
    "очередной плат",
    "будет списан",
    "задолженн",
    "просроч",
    "рассрочк",
    "эслатма",
    "эслатамиз",
    "тўлов санаси",
    "тўлов муддати",
    "тўланг",
    "ечилади",
    "насия",
    "қарздорлик",
    "reminder",
    "due date",
    "will be charged",
    "installment",
    "instalment",
    "overdue",
)
ADVERT_MARKERS: tuple[str, ...] = (
    "keshbek",
    "cashback",
    "aksiya",
    "chegirma",
    "bonus",
    "sovg'a",
    "yutib ol",
    "yutuq",
    "promokod",
    "maxsus taklif",
    "taklif",
    "batafsil",
    "yuklab ol",
    "ulaning",
    "obuna",
    "skidk",
    "akci",
    "aktsi",
    "podar",
    "vyigr",
    "rozygr",
    "predlozheni",
    "podrobn",
    "кешбэк",
    "кэшбэк",
    "кешбек",
    "скидк",
    "акци",
    "подар",
    "выигр",
    "розыгр",
    "промокод",
    "предложени",
    "подробн",
    "чегирма",
    "совға",
    "ютиб ол",
    "таклиф",
    "батафсил",
    "promo",
    "offer",
    "discount",
    "gift",
    r"win\b",
)
# Advert shapes that are not words. Links are deliberately NOT here: real
# receipts carry cheque links.
ADVERT_PATTERNS: tuple[str, ...] = (
    r"(?:so'm|som|sum|сум|сўм)gacha",
    r"\bgacha\b",
    r"\bдо\s+\d",
    r"\d\s*%",
)
# Evidence that the payment completed.
SUCCESS_MARKERS: tuple[str, ...] = (
    "muvaffaqiyatli",
    "amalga oshirildi",
    "o'tkazildi",
    "otkazildi",
    "yechildi",
    "yechib olindi",
    "tushdi",
    "qabul qilindi",
    "to'landi",
    "tolandi",
    "to'ldirildi",
    "toldirildi",
    "uspeshn",
    "proveden",
    "spisan",
    "zachislen",
    "postupil",
    "oplachen",
    "vypolnen",
    "perevedeno",
    "perechislen",
    "успешн",
    "проведен",
    "списан",
    "зачислен",
    "поступил",
    "оплачен",
    "выполнен",
    "переведен",
    "перечислен",
    "муваффақиятли",
    "амалга оширилди",
    "ўтказилди",
    "ечилди",
    "тушди",
    "қабул қилинди",
    "тўланди",
    "тўлдирилди",
    "success",
    "completed",
    r"paid\b",
    "received",
    "credited",
    "debited",
)

# Direction. "To'lov" (payment) is neutral — a payment received is also a
# to'lov — so it is only a weak hint of an expense, used when nothing
# stronger speaks.
STRONG_EXPENSE: tuple[str, ...] = (
    "oplata",
    "оплата",
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
    "yechildi",
    "yechib olindi",
    "ечилди",
    "hisobingizdan",
    "hisobdan",
    "kartadan",
    "kartangizdan",
    "spisan",
    "списан",
    "debited",
    "purchase",
)
WEAK_EXPENSE: tuple[str, ...] = (
    "to'lov",
    "tolov",
    "тўлов",
    "platezh",
    "платеж",
    "payment",
)
STRONG_INCOME: tuple[str, ...] = (
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
    "postupil",
    "поступил",
    "zachislen",
    "зачислен",
    "tushdi",
    "тушди",
    "kelib tushdi",
    "qabul qilindi",
    "қабул қилинди",
    "hisobingizga",
    "kartangizga",
    "hisobiga",
    "to'ldirildi",
    "credited",
    "received",
)
# Kept for the callers and tests that name them.
EXPENSE_WORDS = STRONG_EXPENSE + WEAK_EXPENSE
INCOME_WORDS = STRONG_INCOME

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
_SPACE_TRANSLATION = dict.fromkeys(map(ord, "    "), " ")

# Clauses: lines, semicolons, and a full stop followed by whitespace or the
# end — a decimal mark is followed by a digit and survives the split.
_CLAUSES = re.compile(r"[;\n]+|\.(?=\s|$)")

# Card masks: "*1234", "****1234", "8600****1234", "•• 1234", "XXXX 1234".
_MASK_CHARS = r"(?:[*•·…]{1,4}|(?<![A-Za-z])[xX]{2,4})"
_CARD_MASK = re.compile(r"\d{0,6}" + _MASK_CHARS + r" ?\d{4}(?!\d)")
_LAST4 = re.compile(_MASK_CHARS + r" ?(\d{4})(?!\d)")
_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
# GS client codes and waybill numbers: never an amount, always reported.
# GS client codes come from services/codes.py (WP-29): one regex everywhere.
_WAYBILL = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{2}-\d{4,8})(?!\d)", re.IGNORECASE)

# A money figure: groups of three behind space/dot/comma/apostrophe
# separators, or a plain run, either with an optional 1-2 digit decimal
# part behind a dot or a comma. Not glued to letters, digits, '№' or '#'
# (a cheque number is not an amount), nor to a leading dash (the tail of a
# waybill or a range).
_AMOUNT = re.compile(
    r"(?<![\w.,№#\-])"
    r"(\d{1,3}(?:[ .,']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"(?![\d%])"
)
# A code-shaped number: 4-8 plain digits, no separators, no decimal part.
_CODE_NUMBER = re.compile(r"(?<![\w.,'\-])(\d{4,8})(?![\w]|[.,]\d)")

_MERCHANT_LABEL = re.compile(r"(?:joy|место|merchant)\s*:\s*(.+)", re.IGNORECASE)
# An ALL-CAPS run (Latin or Cyrillic), dots and dashes allowed: KORZINKA.UZ,
# YANDEX.TAXI. At least three characters so stray initials stay out.
_CAPS_RUN = re.compile(r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ0-9.&'\-]{2,}(?:\s+[A-ZА-ЯЁ0-9.&'\-]{2,})*")


def _compile(markers: tuple[str, ...]) -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for marker in markers:
        word_end = marker.endswith(r"\b")
        stem = marker[:-2] if word_end else marker
        pattern = r"(?<![\w'])" + re.escape(stem) + (r"(?![\w'])" if word_end else "")
        compiled.append((stem, re.compile(pattern)))
    return compiled


_OTP = _compile(OTP_MARKERS)
_DECLINED = _compile(DECLINED_MARKERS)
_REVERSAL = _compile(REVERSAL_MARKERS)
_PENDING = _compile(PENDING_MARKERS)
_REMINDER = _compile(REMINDER_MARKERS)
_ADVERT = _compile(ADVERT_MARKERS)
_ADVERT_SHAPES = [(p, re.compile(p)) for p in ADVERT_PATTERNS]
_SUCCESS = _compile(SUCCESS_MARKERS)
_STRONG_EXPENSE = _compile(STRONG_EXPENSE)
_WEAK_EXPENSE = _compile(WEAK_EXPENSE)
_STRONG_INCOME = _compile(STRONG_INCOME)
_BALANCE = _compile(BALANCE_WORDS)
# Every marker family, for the hygiene test.
ALL_MARKERS = (
    _OTP
    + _DECLINED
    + _REVERSAL
    + _PENDING
    + _REMINDER
    + _ADVERT
    + _SUCCESS
    + _STRONG_EXPENSE
    + _WEAK_EXPENSE
    + _STRONG_INCOME
)


@dataclass(slots=True)
class ParsedPayment:
    """One payment text, read deterministically.

    ``verdict`` decides what happens to it; ``confidence`` is HIGH exactly
    when the verdict is BOOK (older callers read that). ``amount`` and
    ``type`` are filled whenever they could be read, even under REVIEW, so
    /tekshir can offer one-tap booking; under IGNORE they may be None.
    """

    type: TransactionType
    amount: Decimal | None
    currency: Currency
    card_last4: str | None
    merchant: str | None
    balance_after: Decimal | None
    sender: str
    confidence: str
    verdict: Verdict = Verdict.REVIEW
    reason: str = NO_EVIDENCE
    markers: tuple[str, ...] = field(default_factory=tuple)
    gs_codes: tuple[str, ...] = field(default_factory=tuple)
    waybills: tuple[str, ...] = field(default_factory=tuple)


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


def _norm(text: str) -> str:
    """Folded apostrophes, plain spaces, casefold, ё→е, runs of spaces
    collapsed. Newlines stay: they split clauses."""
    text = fold_apostrophes(text or "").translate(_SPACE_TRANSLATION)
    text = text.casefold().replace("ё", "е")
    return re.sub(r"[ \t]+", " ", text)


def _hits(compiled: list[tuple[str, re.Pattern[str]]], text: str) -> list[str]:
    return [stem for stem, pattern in compiled if pattern.search(text)]


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
    """Blank out card masks, GS codes, waybills, dates and times so they can
    never become amounts."""
    for pattern in (_CARD_MASK, _WAYBILL, codes.client_code_re(), _DATE, _TIME):
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


def _amounts_in(clause: str) -> list[tuple[Decimal, Currency | None]]:
    masked = _mask(clause)
    found = []
    for match in _AMOUNT.finditer(masked):
        value = _to_decimal(match.group(1))
        if value is not None:
            found.append((value, _currency_near(masked, match.start(1), match.end(1))))
    return found


def _amount_in(clause: str) -> tuple[Decimal | None, Currency | None]:
    """The first plausible figure in a clause, and its adjacent currency."""
    found = _amounts_in(clause)
    return found[0] if found else (None, None)


def _code_shaped(text: str) -> bool:
    """A standalone 4-8 digit run with no separator and no currency next to
    it, once cards, GS codes, waybills, dates and times are blanked out."""
    masked = _mask(text)
    for match in _CODE_NUMBER.finditer(masked):
        if _currency_near(masked, match.start(1), match.end(1)) is None:
            return True
    return False


def _gs_codes(text: str) -> tuple[str, ...]:
    return tuple(codes.find_client_codes(text))


def _waybills(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.group(1).upper() for m in _WAYBILL.finditer(text)))


def _future_date(text: str, received_at: datetime | None) -> bool:
    if received_at is None:
        return False
    local = (
        received_at.astimezone(settings.tz).date()
        if received_at.tzinfo
        else received_at.date()
    )
    for match in _DATE.finditer(text):
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        try:
            when = date(year, month, day)
        except ValueError:
            continue
        if when > local:
            return True
    return False


def _merchant_of(clauses: list[str], body: str) -> str | None:
    """A labelled merchant line, else the trailing ALL-CAPS run."""
    for clause in clauses:
        match = _MERCHANT_LABEL.search(clause)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    candidate: str | None = None
    cleaned = _CARD_MASK.sub(" ", body)
    cleaned = _WAYBILL.sub(" ", codes.client_code_re().sub(" ", cleaned))
    for match in _CAPS_RUN.finditer(cleaned):
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


def read(
    body: str, *, received_at: datetime | None = None, sender: str = ""
) -> ParsedPayment:
    """One payment text in, one deterministic reading out. No sender gate:
    payment-app notifications come here directly."""
    text = fold_apostrophes(body or "").translate(_SPACE_TRANSLATION)
    normalised = _norm(text)
    clauses = [c.strip() for c in _CLAUSES.split(text) if c and c.strip()]
    folded = [_norm(c) for c in clauses]
    barred = [bool(_hits(_BALANCE, low)) for low in folded]

    # --- direction ---------------------------------------------------------
    strong_out = _hits(_STRONG_EXPENSE, normalised)
    weak_out = _hits(_WEAK_EXPENSE, normalised)
    strong_in = _hits(_STRONG_INCOME, normalised)
    conflict = bool(strong_out and strong_in)
    txn_type: TransactionType | None = None
    if strong_in and not strong_out:
        txn_type = TransactionType.income
    elif (strong_out and not strong_in) or (not conflict and weak_out):
        txn_type = TransactionType.expense

    # --- balance and amount --------------------------------------------------
    balance_after: Decimal | None = None
    for i, clause in enumerate(clauses):
        if barred[i]:
            balance_after, _ = _amount_in(clause)
            if balance_after is not None:
                break

    direction_families = _STRONG_EXPENSE + _WEAK_EXPENSE + _STRONG_INCOME
    amount: Decimal | None = None
    currency: Currency | None = None
    for i, low in enumerate(folded):
        if barred[i] or not _hits(direction_families, low):
            continue
        amount, currency = _amount_in(clauses[i])
        break
    if amount is None:
        for i, low in enumerate(folded):
            if barred[i] or not any(label in low for label in AMOUNT_LABELS):
                continue
            amount, currency = _amount_in(clauses[i])
            if amount is not None:
                break
    if amount is None:
        figures = [
            f for i, c in enumerate(clauses) if not barred[i] for f in _amounts_in(c)
        ]
        if len(figures) == 1 and figures[0][1] is not None:
            amount, currency = figures[0]

    # The LAST mask-adjacent run: "UZCARD *8600**1234" ends in the card,
    # not the 8600 BIN it happens to start with.
    card_matches = list(_LAST4.finditer(text))
    card_last4 = card_matches[-1].group(1) if card_matches else None
    merchant = _merchant_of(clauses, text)

    # --- markers and evidence ------------------------------------------------
    otp = _hits(_OTP, normalised)
    declined = _hits(_DECLINED, normalised)
    reversal = _hits(_REVERSAL, normalised)
    pending = _hits(_PENDING, normalised)
    reminder = _hits(_REMINDER, normalised)
    advert = _hits(_ADVERT, normalised) + [
        p for p, rx in _ADVERT_SHAPES if rx.search(normalised)
    ]
    success = _hits(_SUCCESS, normalised)
    has_balance = any(barred)
    evidence = bool(card_last4 or has_balance or success)
    code_number = bool(otp) and _code_shaped(text)
    markers = tuple(
        dict.fromkeys(otp + declined + reversal + pending + reminder + advert + success)
    )

    # --- the rule order: the first hit wins ----------------------------------
    if code_number and not success and not has_balance:
        verdict, reason = Verdict.IGNORE, OTP
    elif code_number:
        verdict, reason = Verdict.REVIEW, OTP_CONFLICT
    elif declined:
        verdict, reason = Verdict.REVIEW, DECLINED
    elif reversal:
        verdict, reason = Verdict.REVIEW, REVERSAL
    elif pending:
        verdict, reason = Verdict.REVIEW, PENDING
    elif reminder:
        verdict, reason = Verdict.REVIEW, REMINDER
    elif _future_date(text, received_at):
        verdict, reason = Verdict.REVIEW, FUTURE_DATE
    elif advert and not evidence:
        verdict = Verdict.REVIEW if settings.payment_adverts_to_review else Verdict.IGNORE
        reason = ADVERT
    elif advert:
        verdict, reason = Verdict.REVIEW, ADVERT_WITH_EVIDENCE
    elif txn_type is None and amount is None and not conflict:
        verdict, reason = Verdict.IGNORE, INFO
    elif conflict:
        verdict, reason = Verdict.REVIEW, CONFLICT
    elif txn_type is None:
        verdict, reason = Verdict.REVIEW, NO_DIRECTION
    elif amount is None:
        verdict, reason = Verdict.REVIEW, NO_AMOUNT
    elif not evidence:
        verdict, reason = Verdict.REVIEW, NO_EVIDENCE
    else:
        verdict, reason = Verdict.BOOK, OK

    return ParsedPayment(
        # A placeholder when no direction was read: nothing is written from it.
        type=txn_type or TransactionType.expense,
        amount=amount,
        currency=currency or Currency.UZS,
        card_last4=card_last4,
        merchant=merchant,
        balance_after=balance_after,
        sender=sender,
        confidence=HIGH if verdict is Verdict.BOOK else LOW,
        verdict=verdict,
        reason=reason,
        markers=markers,
        gs_codes=_gs_codes(text),
        waybills=_waybills(text),
    )


def parse(
    sender: str, body: str, *, received_at: datetime | None = None
) -> ParsedPayment | None:
    """One SMS in, one reading out; None for a sender that is not a bank."""
    if not is_payment_sender(sender):
        return None
    return read(body, received_at=received_at, sender=sender)
