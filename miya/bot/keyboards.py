"""Inline keyboards for the assistant bot.

Callback payloads are deliberately terse — Telegram caps `callback_data` at 64
bytes, and a chat list page carries one payload per button.

    ch:t:<monitor_id>:<field>   toggle one flag on one chat
    ch:p:<page>                 jump to a page of the chat list
    md:y|n:<interaction_id>     read / skip an oversized attachment
    rec:<action>:<ref>          act on one record: d12 / p7 / t3 (see below)
    rec:qa|qs:q<interaction_id> a nudged question: answered / snooze till morning
    rec:ma|ms:m<interaction_id> a missed call: got in touch / snooze till morning
    ng:y|n:<monitor_id>         "Yangi guruh / kanal: … — o'qiymi?": read it / not
    cl:y|n|e:<claim_id>         a counterparty's claim: write it / drop it / correct it
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from miya.bot.formatting import claim_ref, ref
from miya.db.enums import ChatType
from miya.db.models import ChatMonitor

PAGE_SIZE = 8
TITLE_WIDTH = 22

# Short codes keep the payload inside the 64-byte limit.
FIELD_CODES = {"m": "monitor_enabled", "v": "vision_enabled", "d": "docs_enabled"}

CHAT_ICON = {ChatType.private: "👤", ChatType.group: "👥", ChatType.channel: "📢"}


@dataclass(slots=True)
class ChatsPage:
    monitors: list[ChatMonitor]
    page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // PAGE_SIZE))  # ceil


def _short(title: str | None, tg_chat_id: int) -> str:
    text = (title or f"chat {tg_chat_id}").strip()
    return text if len(text) <= TITLE_WIDTH else text[: TITLE_WIDTH - 1] + "…"


def chats_keyboard(page: ChatsPage) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for monitor in page.monitors:
        icon = CHAT_ICON.get(monitor.chat_type, "💬")
        state = "✅" if monitor.monitor_enabled else "❌"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{state} {icon} {_short(monitor.title, monitor.tg_chat_id)}",
                    callback_data=f"ch:t:{monitor.id}:m",
                ),
                InlineKeyboardButton(
                    text="👁" + ("✅" if monitor.vision_enabled else "❌"),
                    callback_data=f"ch:t:{monitor.id}:v",
                ),
                InlineKeyboardButton(
                    text="📄" + ("✅" if monitor.docs_enabled else "❌"),
                    callback_data=f"ch:t:{monitor.id}:d",
                ),
            ]
        )

    if page.pages > 1:
        nav = [
            InlineKeyboardButton(
                text="◀️",
                callback_data=f"ch:p:{(page.page - 1) % page.pages}",
            ),
            InlineKeyboardButton(
                text=f"{page.page + 1}/{page.pages}", callback_data="ch:noop"
            ),
            InlineKeyboardButton(
                text="▶️",
                callback_data=f"ch:p:{(page.page + 1) % page.pages}",
            ),
        ]
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def media_approval(interaction_id: int) -> InlineKeyboardMarkup:
    """Yes/no for one oversized attachment.

    The interaction id is the whole payload: the answer has to survive the
    worker restarting between question and tap, so nothing about it may live
    in memory.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ O'qi", callback_data=f"md:y:{interaction_id}"
                ),
                InlineKeyboardButton(
                    text="✖️ Kerak emas", callback_data=f"md:n:{interaction_id}"
                ),
            ]
        ]
    )


# --- one record: close, correct, flip ---------------------------------------
#
# rec:d:<ref>   ✅ Bajarildi — kept (done / settled in full)
# rec:b:<ref>   ✅ Bajarildi — on a "Hali ochiqmi?" debt line: settle every
#                              row of the balance this ref belongs to
# rec:e:<ref>   ✏️ Tuzat     — shows the /tuzat syntax with the ref filled in
# rec:f:<ref>   🔄 Teskari   — debts only: flip who owes whom
# rec:c:<ref>   Yop          — closed without counting as kept
# rec:o:<ref>   Ha           — still open; nudge again in a week
# rec:r:<ref>   ↩️ Qaytar    — undo a close (the one button on the outcome)
#
# The ref is the whole payload, like the media approval: the tap has to
# survive a restart between the message and the press.

ACTION_DONE = "d"
ACTION_SETTLE_BALANCE = "b"
ACTION_EDIT = "e"
ACTION_FLIP = "f"
ACTION_CLOSE = "c"
ACTION_OPEN = "o"
ACTION_REOPEN = "r"
# rec:v:x<id>  🗑 O'chir — void a money row (WP-13); ↩️ Qaytar undoes it
ACTION_VOID = "v"
# rec:py:<ref>:<n> / rec:pn:<ref>:<n>  Ha / Yo'q to "Yangi odam … yaratilsinmi?"
# after /tuzat named someone MIYA does not know. The name itself would not
# always fit in 64 bytes, so <n> indexes the history entry that holds it.
ACTION_PERSON_YES = "py"
ACTION_PERSON_NO = "pn"
PERSON_ANSWERS = (ACTION_PERSON_YES, ACTION_PERSON_NO)
# rec:qa:q<id> ✅ Javob berdim — the nudged question is answered for good
# rec:qs:q<id> ⏰ Ertalab eslat — snooze it until the next morning brief
# The ref is "q" + the interaction id: not a record ref, and never parsed as
# one — the handler routes these two actions before it looks for a d/p/t.
ACTION_QUESTION_ANSWERED = "qa"
ACTION_QUESTION_SNOOZE = "qs"
QUESTION_ANSWERS = (ACTION_QUESTION_ANSWERED, ACTION_QUESTION_SNOOZE)
# rec:ma:m<id> ✅ Bog'landim — the missed call was returned; the loop closes
# rec:ms:m<id> ⏰ Ertalab eslat — snooze it until the next morning brief
# The ref is "m" + the interaction id of the loop's oldest open ring
# (build step 6), routed like the question pair above.
ACTION_MISSED_ANSWERED = "ma"
ACTION_MISSED_SNOOZE = "ms"
MISSED_ANSWERS = (ACTION_MISSED_ANSWERED, ACTION_MISSED_SNOOZE)

# Telegram caps an inline keyboard well above this, but a reminder that
# needs more rows than this is already unreadable; the tail comes back next
# sweep anyway (the clip logic marks only what was shown).
MAX_ROWS = 25


def record_row(
    kind: str, record_id: int, *, labelled: bool
) -> list[InlineKeyboardButton]:
    """Bajarildi / Tuzat (/ Teskari) for one row.

    ``labelled`` adds the ref to each button: needed as soon as a message
    carries more than one row, or the owner cannot tell which ✅ is which.
    """
    handle = ref(kind, record_id)
    suffix = f" {handle}" if labelled else ""
    if kind == "transaction":
        # A money row is corrected or voided, never "done".
        return [
            InlineKeyboardButton(
                text=f"✏️ Tuzat{suffix}", callback_data=f"rec:{ACTION_EDIT}:{handle}"
            ),
            InlineKeyboardButton(
                text=f"🗑 O'chir{suffix}", callback_data=f"rec:{ACTION_VOID}:{handle}"
            ),
        ]
    row = [
        InlineKeyboardButton(
            text=f"✅ Bajarildi{suffix}", callback_data=f"rec:{ACTION_DONE}:{handle}"
        ),
        InlineKeyboardButton(
            text=f"✏️ Tuzat{suffix}", callback_data=f"rec:{ACTION_EDIT}:{handle}"
        ),
    ]
    if kind == "debt":
        row.append(
            InlineKeyboardButton(
                text=f"🔄 Teskari{suffix}", callback_data=f"rec:{ACTION_FLIP}:{handle}"
            )
        )
    return row


def question_row(
    kind: str,
    record_id: int,
    *,
    labelled: bool,
    label: str | None = None,
    number: int | None = None,
) -> list[InlineKeyboardButton]:
    """Ha / Bajarildi (/ Yop) — the "Hali ochiqmi?" answer row.

    A debt line is a balance, and the owner thinks in balances per person:
    its ✅ settles every row of that balance, and there is no Yop — money is
    settled or corrected, never voided, so a Yop button on a debt could
    never do anything. ``label`` overrides the ref shown on the buttons,
    e.g. "d12, d15" for a two-row balance.
    """
    handle = ref(kind, record_id)
    suffix = f" {label or handle}" if labelled else ""
    done = ACTION_SETTLE_BALANCE if kind == "debt" else ACTION_DONE
    if number is not None:
        texts = (f"{number} Ha, ochiq", f"{number} ✅ Bajarildi", f"{number} ✖️ Yop")
    else:
        texts = (f"Ha{suffix}", f"✅ Bajarildi{suffix}", f"✖️ Yop{suffix}")
    row = [
        InlineKeyboardButton(text=texts[0], callback_data=f"rec:{ACTION_OPEN}:{handle}"),
        InlineKeyboardButton(text=texts[1], callback_data=f"rec:{done}:{handle}"),
    ]
    if kind != "debt":
        row.append(
            InlineKeyboardButton(
                text=texts[2], callback_data=f"rec:{ACTION_CLOSE}:{handle}"
            )
        )
    return row


def reopen_actions(refs: list[tuple[str, int]]) -> InlineKeyboardMarkup | None:
    """One "↩️ Qaytar" per closed row — the outcome message's only button."""
    refs = [(k, i) for k, i in refs if i is not None][:MAX_ROWS]
    if not refs:
        return None
    labelled = len(refs) > 1
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Qaytar" + (f" {ref(k, i)}" if labelled else ""),
                    callback_data=f"rec:{ACTION_REOPEN}:{ref(k, i)}",
                )
            ]
            for k, i in refs
        ]
    )


def record_actions(refs: list[tuple[str, int]]) -> InlineKeyboardMarkup | None:
    """One action row per (kind, id); None when there is nothing to act on."""
    refs = [(k, i) for k, i in refs if i is not None][:MAX_ROWS]
    if not refs:
        return None
    labelled = len(refs) > 1
    return InlineKeyboardMarkup(
        inline_keyboard=[record_row(k, i, labelled=labelled) for k, i in refs]
    )


def question_actions(refs: list[tuple[str, int]]) -> InlineKeyboardMarkup | None:
    refs = [(k, i) for k, i in refs if i is not None][:MAX_ROWS]
    if not refs:
        return None
    labelled = len(refs) > 1
    return InlineKeyboardMarkup(
        inline_keyboard=[question_row(k, i, labelled=labelled) for k, i in refs]
    )


def question_keyboard(questions) -> InlineKeyboardMarkup | None:
    """One answer row per "Hali ochiqmi?" line.

    A question about a debt balance spans several rows (d12, d15) but gets
    one answer row, keyed by its first ref and labelled with all of them —
    the owner answers for the balance, not per row.
    """
    rows = []
    for question in questions[:MAX_ROWS]:
        refs = [(k, i) for k, i in question.refs if i is not None]
        if not refs:
            continue
        kind, first = refs[0]
        rows.append((kind, first, ", ".join(ref(k, i) for k, i in refs)))
    if not rows:
        return None
    labelled = len(rows) > 1
    return InlineKeyboardMarkup(
        inline_keyboard=[
            question_row(kind, first, labelled=labelled, label=label)
            for kind, first, label in rows
        ]
    )


def new_person_question(handle: str, index: int) -> InlineKeyboardMarkup:
    """Ha / Yo'q under "Yangi odam 'Sardor' yaratilsinmi?"."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ha", callback_data=f"rec:{ACTION_PERSON_YES}:{handle}:{index}"
                ),
                InlineKeyboardButton(
                    text="Yo'q", callback_data=f"rec:{ACTION_PERSON_NO}:{handle}:{index}"
                ),
            ]
        ]
    )


def without(
    markup: InlineKeyboardMarkup | None, handle: str
) -> InlineKeyboardMarkup | None:
    """The same keyboard minus every row that acts on ``handle``.

    After a row is closed its buttons must go, but the other rows on the same
    confirmation or reminder still have work to do.
    """
    if markup is None:
        return None
    rows = [
        row
        for row in markup.inline_keyboard
        if not any((b.callback_data or "").endswith(f":{handle}") for b in row)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --- open loops: the nudge, the brief, a new group ----------------------------


def question_ref(interaction_id: int) -> str:
    return f"q{interaction_id}"


def parse_question_ref(handle: str) -> int | None:
    """'q12' → 12; anything else → None."""
    if handle[:1] == "q" and handle[1:].isdigit():
        return int(handle[1:])
    return None


def nudge_actions(interaction_id: int) -> InlineKeyboardMarkup:
    """✅ Javob berdim / ⏰ Ertalab eslat under one nudged question.

    "Ertalab", not "Ertaga": tapped at 08:00 the nudge comes back at today's
    brief, so the label names the brief rather than a day.
    """
    handle = question_ref(interaction_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Javob berdim",
                    callback_data=f"rec:{ACTION_QUESTION_ANSWERED}:{handle}",
                ),
                InlineKeyboardButton(
                    text="⏰ Ertalab eslat",
                    callback_data=f"rec:{ACTION_QUESTION_SNOOZE}:{handle}",
                ),
            ]
        ]
    )


def missed_ref(interaction_id: int) -> str:
    """'m12' — the loop's oldest open ring, by its interaction id."""
    return f"m{interaction_id}"


def parse_missed_ref(handle: str) -> int | None:
    """'m12' → 12; anything else → None."""
    if handle[:1] == "m" and handle[1:].isdigit():
        return int(handle[1:])
    return None


def missed_row(
    interaction_id: int, *, labelled: bool = True, number: int | None = None
) -> list[InlineKeyboardButton]:
    """✅ Bog'landim / ⏰ Ertalab eslat for one missed-call loop."""
    handle = missed_ref(interaction_id)
    if number is not None:
        return [
            InlineKeyboardButton(
                text=f"{number} ✅ Bog'landim",
                callback_data=f"rec:{ACTION_MISSED_ANSWERED}:{handle}",
            ),
            InlineKeyboardButton(
                text=f"{number} ⏰ Ertalab",
                callback_data=f"rec:{ACTION_MISSED_SNOOZE}:{handle}",
            ),
        ]
    suffix = f" {handle}" if labelled else ""
    return [
        InlineKeyboardButton(
            text=f"✅ Bog'landim{suffix}",
            callback_data=f"rec:{ACTION_MISSED_ANSWERED}:{handle}",
        ),
        InlineKeyboardButton(
            text=f"⏰ Ertalab eslat{suffix}",
            callback_data=f"rec:{ACTION_MISSED_SNOOZE}:{handle}",
        ),
    ]


def missed_actions(interaction_id: int) -> InlineKeyboardMarkup:
    """The two buttons under one missed-call nudge (build step 6).

    Unlabelled: the nudge is one message about one caller, so a bare
    ✅ Bog'landim cannot be misread. The brief's rows are labelled instead.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[missed_row(interaction_id, labelled=False)]
    )


def brief_actions(due: list[tuple[str, int]]) -> InlineKeyboardMarkup | None:
    """The morning brief's buttons: a ✅ / ✏️ (/ 🔄) row per due row, the
    same rows a reminder carries. Always labelled — the brief carries many
    rows. Questions are not here (WP-19): the brief tells, and the numbered
    question batch sent after it asks."""
    due = [(k, i) for k, i in due if i is not None]
    rows = [record_row(k, i, labelled=True) for k, i in dict.fromkeys(due)][:MAX_ROWS]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def new_group_question(monitor_id: int) -> InlineKeyboardMarkup:
    """Ha / Yo'q under "Yangi guruh: … — o'qiymi?" (or "Yangi kanal: …").

    The monitor id is the whole payload — a channel and a group get the same
    two buttons, and the handler answers with the row's own title.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha", callback_data=f"ng:y:{monitor_id}"),
                InlineKeyboardButton(text="✖️ Yo'q", callback_data=f"ng:n:{monitor_id}"),
            ]
        ]
    )


# --- a counterparty's claim: Ha / Yo'q / Tuzat ----------------------------------
#
# cl:y:<id>   ✅ Ha      — write it, exactly as the extraction would have
# cl:n:<id>   ✖️ Yo'q    — drop it; nothing is written
# cl:e:<id>   ✏️ Tuzat   — shows the /tuzat syntax with c<id> filled in
#
# The claim id is the whole payload, like every other button here: the tap
# has to survive a restart between the question and the press. Always
# labelled — a claim sits next to record rows on a receipt and next to other
# claims on the brief, so a bare "Ha" would not say which.

ACTION_CLAIM_YES = "y"
ACTION_CLAIM_NO = "n"
ACTION_CLAIM_EDIT = "e"
ACTION_CLAIM_UNDO = "u"
CLAIM_PREFIX = "cl"


def claim_row(claim_id: int, *, number: int | None = None) -> list[InlineKeyboardButton]:
    if number is not None:
        return [
            InlineKeyboardButton(
                text=f"{number} {label}",
                callback_data=f"{CLAIM_PREFIX}:{action}:{claim_id}",
            )
            for label, action in (
                ("✅ Ha", ACTION_CLAIM_YES),
                ("✖️ Yo'q", ACTION_CLAIM_NO),
                ("✏️ Tuzat", ACTION_CLAIM_EDIT),
            )
        ]
    handle = claim_ref(claim_id)
    return [
        InlineKeyboardButton(
            text=f"✅ Ha {handle}",
            callback_data=f"{CLAIM_PREFIX}:{ACTION_CLAIM_YES}:{claim_id}",
        ),
        InlineKeyboardButton(
            text=f"✖️ Yo'q {handle}",
            callback_data=f"{CLAIM_PREFIX}:{ACTION_CLAIM_NO}:{claim_id}",
        ),
        InlineKeyboardButton(
            text=f"✏️ Tuzat {handle}",
            callback_data=f"{CLAIM_PREFIX}:{ACTION_CLAIM_EDIT}:{claim_id}",
        ),
    ]


def claim_actions(claim_ids: list[int]) -> InlineKeyboardMarkup | None:
    """One Ha / Yo'q / Tuzat row per claim; None when there is none."""
    ids = [i for i in claim_ids if i is not None][:MAX_ROWS]
    if not ids:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[claim_row(i) for i in ids])


def claim_ids_in(markup: InlineKeyboardMarkup | None) -> list[int]:
    """The claims a keyboard carries a Ha / Yo'q / Tuzat row for, in row order.

    Only what was actually shown may be marked as asked, and a keyboard has
    a ceiling: the handler reads the built keyboard rather than re-deriving
    which claims should have fitted.
    """
    head = f"{CLAIM_PREFIX}:{ACTION_CLAIM_YES}:"
    ids: list[int] = []
    for row in markup.inline_keyboard if markup else []:
        for button in row:
            data = button.callback_data or ""
            if data.startswith(head) and data[len(head) :].isdigit():
                ids.append(int(data[len(head) :]))
    return ids


def applied_actions(
    record_refs: list[tuple[str, int]], claim_ids: list[int]
) -> InlineKeyboardMarkup | None:
    """A receipt's buttons: the record rows, then a row per claim it asks.

    Record rows come first and are labelled as soon as the keyboard carries
    more than one row of any kind — a claim row is always labelled, so a
    lone ✅ Bajarildi next to "✅ Ha c12" would read wrong.
    """
    records = [(k, i) for k, i in record_refs if i is not None][:MAX_ROWS]
    room = max(0, MAX_ROWS - len(records))
    ids = [i for i in claim_ids if i is not None][:room]
    if not records and not ids:
        return None
    labelled = len(records) + len(ids) > 1
    rows = [record_row(k, i, labelled=labelled) for k, i in records]
    rows += [claim_row(i) for i in ids]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def without_claim(
    markup: InlineKeyboardMarkup | None, claim_id: int
) -> InlineKeyboardMarkup | None:
    """The same keyboard minus the row that asks about ``claim_id``.

    An answered claim's buttons must go, but the record rows and the other
    claims on the same receipt, brief or list still have work to do.
    """
    if markup is None:
        return None
    suffix = f":{claim_id}"
    rows = [
        row
        for row in markup.inline_keyboard
        if not any(
            (b.callback_data or "").startswith(f"{CLAIM_PREFIX}:")
            and (b.callback_data or "").endswith(suffix)
            for b in row
        )
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --- /tekshir: one tap per money text, a dismiss per seen row (WP-14) ---------
#
# rv:e:<id>  📉 Chiqim #n   — book the text as an expense
# rv:i:<id>  📈 Kirim #n    — book it as income
# rv:n:<id>  ✖️ Pul emas #n — not a payment; nothing is booked
# rv:nold    ✖️ …eskilarini — every money review row older than 7 days
# rv:ok:<id> ✔️ Ko'rdim #n  — a processed row the owner has seen

REVIEW_PREFIX = "rv"
REVIEW_EXPENSE = "e"
REVIEW_INCOME = "i"
REVIEW_NOT_MONEY = "n"
REVIEW_OLD = "nold"
REVIEW_SEEN = "ok"
REVIEW_OLD_DAYS = 7


def _money_buttons(row, number: int) -> list[InlineKeyboardButton]:
    buttons = []
    if ((row.media or {}).get("money") or {}).get("amount"):
        buttons += [
            InlineKeyboardButton(
                text=f"📉 Chiqim #{number}",
                callback_data=f"{REVIEW_PREFIX}:{REVIEW_EXPENSE}:{row.id}",
            ),
            InlineKeyboardButton(
                text=f"📈 Kirim #{number}",
                callback_data=f"{REVIEW_PREFIX}:{REVIEW_INCOME}:{row.id}",
            ),
        ]
    buttons.append(
        InlineKeyboardButton(
            text=f"✖️ Pul emas #{number}",
            callback_data=f"{REVIEW_PREFIX}:{REVIEW_NOT_MONEY}:{row.id}",
        )
    )
    return buttons


def money_review(
    money_rows, other_rows=(), ignored_rows=(), *, now=None
) -> InlineKeyboardMarkup | None:
    """The /tekshir keyboard, numbered like replies.review_report's lines."""
    rows: list[list[InlineKeyboardButton]] = []
    number = 1
    for row in money_rows:
        rows.append(_money_buttons(row, number))
        number += 1
    for row in other_rows:
        if row.processed:
            # An unprocessed row's exit is /qayta; dismissing a recording
            # would let retention delete its audio.
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✔️ Ko'rdim #{number}",
                        callback_data=f"{REVIEW_PREFIX}:{REVIEW_SEEN}:{row.id}",
                    )
                ]
            )
        number += 1
    for row in ignored_rows:
        rows.append(_money_buttons(row, number))
        number += 1
    rows = rows[:MAX_ROWS]
    if now is not None and any(
        now - row.occurred_at > timedelta(days=REVIEW_OLD_DAYS) for row in money_rows
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text="✖️ 7 kundan eskilarini «pul emas»",
                    callback_data=f"{REVIEW_PREFIX}:{REVIEW_OLD}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --- one message, several questions (WP-18) ------------------------------------
#
# The question job sends up to QUESTION_BATCH_MAX questions as one numbered
# message. The payloads are the single-question ones, unchanged; only the
# button text gains the line number, and every handler removes its own row
# only (without_prefixed), so answering #2 leaves #1 and #3 in place.


def nudge_row(interaction_id: int, *, number: int) -> list[InlineKeyboardButton]:
    handle = question_ref(interaction_id)
    return [
        InlineKeyboardButton(
            text=f"{number} ✅ Javob berdim",
            callback_data=f"rec:{ACTION_QUESTION_ANSWERED}:{handle}",
        ),
        InlineKeyboardButton(
            text=f"{number} ⏰ Ertalab",
            callback_data=f"rec:{ACTION_QUESTION_SNOOZE}:{handle}",
        ),
    ]


def media_row(interaction_id: int, *, number: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=f"{number} ✅ O'qi", callback_data=f"md:y:{interaction_id}"
        ),
        InlineKeyboardButton(
            text=f"{number} ✖️ Kerak emas", callback_data=f"md:n:{interaction_id}"
        ),
    ]


def money_row(interaction_id: int, *, number: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=f"{number} 📉 Chiqim",
            callback_data=f"{REVIEW_PREFIX}:{REVIEW_EXPENSE}:{interaction_id}",
        ),
        InlineKeyboardButton(
            text=f"{number} 📈 Kirim",
            callback_data=f"{REVIEW_PREFIX}:{REVIEW_INCOME}:{interaction_id}",
        ),
        InlineKeyboardButton(
            text=f"{number} ✖️ Pul emas",
            callback_data=f"{REVIEW_PREFIX}:{REVIEW_NOT_MONEY}:{interaction_id}",
        ),
    ]


def question_item_row(item, number: int) -> list[InlineKeyboardButton]:
    """The answer row of one queued question (questions.Pending)."""
    subject = item.subject
    if item.kind == "claim":
        return claim_row(subject.id, number=number)
    if item.kind == "missed":
        return missed_row(subject.interaction_id, number=number)
    if item.kind == "nudge":
        return nudge_row(subject.interaction_id, number=number)
    if item.kind == "media":
        return media_row(subject.id, number=number)
    if item.kind == "money":
        return money_row(subject.id, number=number)
    # still_open: a debt question is keyed by the first row of its balance.
    kind, record_id = subject.refs[0]
    return question_row(kind, record_id, labelled=False, number=number)


def question_batch(items) -> InlineKeyboardMarkup | None:
    rows = [question_item_row(item, n) for n, item in enumerate(items, 1)]
    rows = rows[:MAX_ROWS]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def without_prefixed(
    markup: InlineKeyboardMarkup | None, prefix: str, ident
) -> InlineKeyboardMarkup | None:
    """The keyboard minus the rows of one (prefix, id): md:y:55 never takes
    cl:y:55 with it."""
    if markup is None:
        return None
    ident = str(ident)

    def hit(button) -> bool:
        parts = (button.callback_data or "").split(":")
        return parts[0] == prefix and parts[-1] == ident

    rows = [row for row in markup.inline_keyboard if not any(hit(b) for b in row)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def is_numbered(markup: InlineKeyboardMarkup | None) -> bool:
    if markup is None:
        return False
    return any(
        (b.text or "")[:1].isdigit() for row in markup.inline_keyboard for b in row
    )


def group_digest(monitors) -> InlineKeyboardMarkup | None:
    """One ✅ {marks}{title} / ✖️ row per listed group, and a last
    "✖️ Qolganlari kerak emas" that declines whatever is still listed."""
    rows = []
    for m in monitors:
        marks = ("📣 " if m.addressed_at else "") + ("✍️ " if m.owner_active_at else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ {marks}{_short(m.title, m.tg_chat_id)}",
                    callback_data=f"ng:y:{m.id}",
                ),
                InlineKeyboardButton(text="✖️", callback_data=f"ng:n:{m.id}"),
            ]
        )
    if not rows:
        return None
    rows.append(
        [InlineKeyboardButton(text="✖️ Qolganlari kerak emas", callback_data="ng:r")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_ids_in(markup: InlineKeyboardMarkup | None) -> list[int]:
    """The monitors a digest still lists, read off its ✅ buttons."""
    if markup is None:
        return []
    ids = []
    for row in markup.inline_keyboard:
        for button in row:
            parts = (button.callback_data or "").split(":")
            if parts[:2] == ["ng", "y"] and parts[2].lstrip("-").isdigit():
                ids.append(int(parts[2]))
    return ids


# --- client codes (WP-33) ------------------------------------------------------


def code_move(row_id: int, person_id: int) -> InlineKeyboardMarkup:
    """ "✅ Ha, o'tkaz" / "Yo'q" under "GS367 hozir boshqa odamda"."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ha, o'tkaz", callback_data=f"kod:mv:{row_id}:{person_id}"
                ),
                InlineKeyboardButton(text="Yo'q", callback_data="kod:no"),
            ]
        ]
    )


def code_suggestions(rows) -> InlineKeyboardMarkup | None:
    """One row per suggestion: [✅ GS367 → Akmal] [✖️]."""
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ {row.code} → {row.person.display_name[:20]}",
                    callback_data=f"kod:sy:{row.id}",
                ),
                InlineKeyboardButton(text="✖️", callback_data=f"kod:sn:{row.id}"),
            ]
            for row in rows
        ]
    )


def client_import(interaction_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ha, yoz", callback_data=f"kod:imp:{interaction_id}"
                ),
                InlineKeyboardButton(text="Bekor", callback_data="kod:impno"),
            ]
        ]
    )


def with_money_splits(
    markup: InlineKeyboardMarkup | None, splits: list[tuple[int, int]]
) -> InlineKeyboardMarkup | None:
    """One [➕ Bu boshqa to'lov] row per typed payment matched to a bank row."""
    if not splits:
        return markup
    rows = list(markup.inline_keyboard) if markup is not None else []
    rows += [
        [
            InlineKeyboardButton(
                text="➕ Bu boshqa to'lov", callback_data=f"mx:split:{iid}:{index}"
            )
        ]
        for iid, index in splits
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def undo_row(claim_id: int) -> list[InlineKeyboardButton]:
    """↩️ Qayta so'ra c12: an automatic answer undone (WP-45)."""
    return [
        InlineKeyboardButton(
            text=f"↩️ Qayta so'ra {claim_ref(claim_id)}",
            callback_data=f"{CLAIM_PREFIX}:{ACTION_CLAIM_UNDO}:{claim_id}",
        )
    ]


def auto_resolved_keyboard(claim_ids, monitor_ids) -> InlineKeyboardMarkup | None:
    rows = [undo_row(i) for i in list(claim_ids)[:MAX_ROWS]]
    rows += [
        [InlineKeyboardButton(text=f"✅ O'qiy boshla #{i}", callback_data=f"ng:y:{i}")]
        for i in monitor_ids
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
