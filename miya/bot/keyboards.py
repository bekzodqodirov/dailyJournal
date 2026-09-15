"""Inline keyboards for the assistant bot.

Callback payloads are deliberately terse — Telegram caps `callback_data` at 64
bytes, and a chat list page carries one payload per button.

    ch:t:<monitor_id>:<field>   toggle one flag on one chat
    ch:p:<page>                 jump to a page of the chat list
    md:y|n:<interaction_id>     read / skip an oversized attachment
    rec:<action>:<ref>          act on one record: d12 / p7 / t3 (see below)
    rec:qa|qs:q<interaction_id> a nudged question: answered / snooze till morning
    ng:y|n:<monitor_id>         "Yangi guruh / kanal: … — o'qiymi?": read it / not
    cl:y|n|e:<claim_id>         a counterparty's claim: write it / drop it / correct it
"""

from __future__ import annotations

from dataclasses import dataclass

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
    kind: str, record_id: int, *, labelled: bool, label: str | None = None
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
    row = [
        InlineKeyboardButton(
            text=f"Ha{suffix}", callback_data=f"rec:{ACTION_OPEN}:{handle}"
        ),
        InlineKeyboardButton(
            text=f"✅ Bajarildi{suffix}", callback_data=f"rec:{done}:{handle}"
        ),
    ]
    if kind != "debt":
        row.append(
            InlineKeyboardButton(
                text=f"✖️ Yop{suffix}", callback_data=f"rec:{ACTION_CLOSE}:{handle}"
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


def brief_actions(
    due: list[tuple[str, int]],
    stale: list[tuple[str, int]],
    claim_ids: list[int] | tuple[int, ...] = (),
) -> InlineKeyboardMarkup | None:
    """The morning brief's buttons: a ✅ / ✏️ row per due row, a Ha /
    Bajarildi / Yop row per undated one that has been sitting — the same rows
    the reminder and the "Hali ochiqmi?" question carry, so he acts from the
    brief the way he acts from those — and a Ha / Yo'q / Tuzat row per claim
    still waiting for his word. Always labelled: the brief carries many
    rows, and a bare ✅ would not say which."""
    due = [(k, i) for k, i in due if i is not None]
    stale = [(k, i) for k, i in stale if i is not None and (k, i) not in due]
    rows = [record_row(k, i, labelled=True) for k, i in due]
    rows += [question_row(k, i, labelled=True) for k, i in stale]
    rows += [claim_row(i) for i in claim_ids if i is not None]
    rows = rows[:MAX_ROWS]
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
CLAIM_PREFIX = "cl"


def claim_row(claim_id: int) -> list[InlineKeyboardButton]:
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
