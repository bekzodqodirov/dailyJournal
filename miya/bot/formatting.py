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
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
    TaskPriority,
    TaskStatus,
)

if TYPE_CHECKING:  # annotations only: formatting never imports the services
    from miya.services.claims import ClaimView
    from miya.services.loops import (
        MissedCall,
        QuietCounterparty,
        StaleCommitment,
        UnansweredQuestion,
    )
    from miya.services.queries import DebtBalance, TimelineEntry

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


def day_label(value: datetime, *, today: date | None = None) -> str:
    """'12-sen' for this year, '12-sen 2025' for an older one.

    A person's history spans years; a bare day-month would make last year's
    call look like last week's.
    """
    local = value.astimezone(settings.tz).date()
    today = today or datetime.now(settings.tz).date()
    label = short_date(local)
    return label if local.year == today.year else f"{label} {local.year}"


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

REF_PREFIX = {"debt": "d", "promise": "p", "task": "t", "transaction": "x"}
KIND_OF_PREFIX = {prefix: kind for kind, prefix in REF_PREFIX.items()}

_REF = re.compile(r"^\s*#?([dptx])(\d{1,9})\s*$", re.IGNORECASE)


def ref(kind: str, record_id: int | None) -> str:
    """'d12' for debt 12. Empty for an unsaved row, so a line never shows 'dNone'."""
    if record_id is None:
        return ""
    return f"{REF_PREFIX[kind]}{record_id}"


def ref_of(record) -> str:
    """The reference of an ORM row, by its table."""
    return ref(kind_of(record), record.id)


def kind_of(record) -> str:
    return {
        "debts": "debt",
        "promises": "promise",
        "tasks": "task",
        "transactions": "transaction",
    }[record.__tablename__]


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
    if kind == "transaction":
        return transaction_line(record, person, handle=handle, markup=markup)
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


def transaction_line(txn, person=None, *, handle: str = "", markup: bool = True) -> str:
    """One money row: direction, amount, what, when, card, who, and 🗑 when
    it was voided (WP-13)."""
    income = txn.type.value == "income"
    what = txn.description or txn.category or "—"
    card = f" · karta *{txn.card_last4}" if txn.card_last4 else ""
    who = f" · 👤 {_text(person.display_name, markup)}" if person is not None else ""
    void = " · 🗑 o'chirilgan" if txn.voided_at is not None else ""
    return (
        f"{'📈' if income else '📉'} {handle}{'Kirim' if income else 'Chiqim'}: "
        f"{money(txn.amount, txn.currency)} · {_text(what, markup)} · "
        f"{day_label(txn.occurred_at)} {clock(txn.occurred_at)}{card}{who}{void}"
    )


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


def missed_line(m: MissedCall, *, markup: bool = True) -> str:
    """One missed call nobody dealt with: who rang, how long ago, how often.

        📵 <b>Akmal</b> qo'ng'iroq qildi — javobsiz · 2 soat oldin (2 marta)

    An unknown caller renders as the number the phone reported. The name and
    the number both come off the handset, so they are escaped like any other
    counterparty-controlled text; ``markup=False`` is the plain form for the
    report's data block, which escapes the finished line itself.
    """
    if m.person is not None:
        who = _name(m.person_name, markup)
    else:
        who = _text(m.phone or "noma'lum raqam", markup)
    tail = f" ({m.attempts} marta)" if m.attempts > 1 else ""
    if markup:
        return f"📵 {who} qo'ng'iroq qildi — javobsiz · {age_label(m.age)} oldin{tail}"
    return f"📵 {who} qo'ng'iroq qildi — javobsiz, {age_label(m.age)} oldin{tail}"


# --- a counterparty's claim: one question line ---------------------------------
#
# What someone *else* asserted — "you owe me", "I paid you back", "you
# promised" — is asked, never written silently (docs/owner-decisions.md,
# "Counterparty claims"). The line names who said it, restates the item from
# the owner's point of view, and ends in a question. Rendered with markup on
# the receipt, the brief and the one-question message, and plain for the
# report's data block, like the open-loop lines above.

CLAIM_REF_PREFIX = "c"

# The transaction's own type does not travel in the view yet; until it does
# a counterparty's money is asked about as "pul harakati" (see the view).
_TXN_LABEL = {"income": "kirim", "expense": "chiqim"}


def claim_ref(claim_id: int) -> str:
    """'c12' — the handle the question and `/tuzat c12` use."""
    return f"{CLAIM_REF_PREFIX}{claim_id}"


def _claim_money(view: ClaimView) -> str:
    """The amount as said, or an honest gap when the payload lacks one."""
    if view.amount is None or view.currency is None:
        return "noma'lum summa"
    return money(view.amount, view.currency)


def _claim_body(view: ClaimView, markup: bool) -> str:
    """What was claimed, from the owner's point of view, without the question."""
    detail = f" ({quote(view.description, markup=markup)})" if view.description else ""
    kind = view.kind
    if kind == "debt":
        amount = _claim_money(view)
        if view.direction is DebtDirection.they_owe_me:
            return f"u senga {amount} qarz{detail}"
        if view.direction is DebtDirection.i_owe_them:
            return f"sen unga {amount} qarzsan{detail}"
        return f"orangizda {amount} qarz bor{detail}"
    if kind == "settlement":
        amount = _claim_money(view)
        if view.direction is DebtDirection.i_owe_them:
            # A settlement's direction names the debt it closes: the owner
            # owed, so the counterparty says the owner paid him.
            return f"sen unga {amount} to'lagansan{detail}"
        return f"{amount} to'ladi{detail}"
    if kind == "transaction":
        label = _TXN_LABEL.get(getattr(view, "txn_type", None) or "", "pul harakati")
        return f"{_claim_money(view)} {label}{detail}"
    if kind == "promise":
        due = f" ({short_date(view.due)})" if view.due else ""
        return f"sen va'da bergansan — {quote(view.description, markup=markup)}{due}"
    # fulfilment: the counterparty says he did what he had promised
    return quote(view.description, markup=markup)


def claim_line(view: ClaimView, *, markup: bool = True) -> str:
    """One question per claim: who said it, what, and "to'g'rimi?".

        ❓ c12 Akmal aytdi: u senga 5 mln so'm qarz («sabab») — to'g'rimi?

    ``markup=False`` is the same line with no tags and no escaping, for the
    report's data block. Never raises: the view is built to tolerate a
    partial payload, and every gap reads as one here too.
    """
    handle = claim_ref(view.id)
    handle = f"<code>{handle}</code>" if markup else handle
    who = _name(view.person_name or "Kimdir", markup)
    question = "va'dasi bajarilganmi?" if view.kind == "fulfilment" else "to'g'rimi?"
    repeats = getattr(view, "repeats", 0)
    suffix = f" (+{repeats} takror)" if repeats else ""
    evidence = getattr(view, "evidence", None)
    if view.state == "auto" and evidence is not None:
        line = f"✅ {handle} {who} aytdi: {_claim_body(view, markup)} — bank tasdiqladi"
    else:
        line = (
            f"❓ {handle} {who} aytdi: {_claim_body(view, markup)} — {question}{suffix}"
        )
    if evidence is not None:
        line += "\n" + claim_evidence_line(evidence, markup=markup)
    return line


def claim_evidence_line(txn, *, markup: bool = True) -> str:
    """The bank row under a claim (WP-44); the figures are the SQL row's."""
    sign = "+" if txn.type.value == "income" else "−"
    when = txn.occurred_at.astimezone(settings.tz)
    handle = ref("transaction", txn.id)
    handle = f"<code>{handle}</code>" if markup else handle
    return (
        f"    💳 Bank: {sign}{money(txn.amount, txn.currency)} · "
        f"{short_date(when.date())} {clock(when)} {handle}"
    )


# --- one person's history: one line per contact ----------------------------
#
# The timeline (/kim, /tarix) shows where each contact came from with an
# emoji and a word, so a call and a chat are told apart at a glance.

SOURCE_EMOJI = {
    InteractionSource.phone_call: "📞",
    InteractionSource.phone_sms: "✉️",
    InteractionSource.telegram_userbot: "💬",
    InteractionSource.assistant_bot: "✍️",
    InteractionSource.manual: "✍️",
    InteractionSource.receipt_photo: "🧾",
    InteractionSource.calendar: "📅",
    InteractionSource.phone_notification: "🔔",
}

SOURCE_WORD = {
    InteractionSource.phone_call: "qo'ng'iroq",
    InteractionSource.phone_sms: "sms",
    InteractionSource.telegram_userbot: "telegram",
    InteractionSource.assistant_bot: "yozuv",
    InteractionSource.manual: "yozuv",
    InteractionSource.receipt_photo: "chek",
    InteractionSource.calendar: "uchrashuv",
    InteractionSource.phone_notification: "ilova",
}

# A DM line shown on its own (``/tarix`` with a direction, the RAG tool)
# needs to say who said it; a note the owner typed into the bot does not.
_SPEAKER = {Direction.out: "sen", Direction.in_: "u"}

TIMELINE_TEXT_LIMIT = 200
TIMELINE_NO_TEXT = "[matn yo'q]"


def timeline_line(entry: TimelineEntry, *, markup: bool = True) -> str:
    """'12-sen · 📞 qo'ng'iroq · summary…' — one contact, one line.

    ``markup=False`` is the same line with no escaping, for a plain-text
    block. The text is someone's words or an extraction summary: folded to
    one line, cut with an ellipsis, and escaped for Telegram HTML.
    """
    emoji = SOURCE_EMOJI.get(entry.source, "•")
    word = SOURCE_WORD.get(entry.source, entry.source.value)
    body = " ".join((entry.text or "").split())
    if len(body) > TIMELINE_TEXT_LIMIT:
        body = body[: TIMELINE_TEXT_LIMIT - 1] + "…"
    if not body:
        body = TIMELINE_NO_TEXT
    speaker = ""
    if entry.source is InteractionSource.telegram_userbot and entry.direction in _SPEAKER:
        speaker = f"{_SPEAKER[entry.direction]}: "
    return f"{day_label(entry.when)} · {emoji} {word} · {speaker}{_text(body, markup)}"


# --- the question queue's count line (WP-19) -----------------------------------
#
# Here, not in replies: reports.py renders it too and must not import replies.


def queue_line(queue, *, markup: bool = True) -> str | None:
    """'❓ Yana 10 ta savol navbatda (10 tasi pul bo'yicha) — /savollar', or
    None when nothing waits. The same text with or without markup."""
    if queue is None or queue.waiting <= 0:
        return None
    if queue.money > 0:
        return (
            f"❓ Yana {queue.waiting} ta savol navbatda "
            f"({queue.money} tasi pul bo'yicha) — /savollar"
        )
    return f"❓ Yana {queue.waiting} ta savol navbatda — /savollar"
