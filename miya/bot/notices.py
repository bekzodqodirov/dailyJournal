"""Receipts for what a Telegram chat put on the ledger, in Uzbek.

The owner's own notes are confirmed on the spot with ``replies.confirmation``,
and a phone call that produced something is confirmed by the worker. A debt
the extractor pulled out of a group chat used to land in ``/qarz`` with no
message at all, and the owner first met it in the evening report as an
established fact. He decided nothing is written from a chat without him being
told, so every applied window that changed the ledger gets the same receipt —
with a header saying which chat, who, and when — and the batch poll delivers
them outside quiet hours.

Only the header and the overflow summary live here; the receipt lines are
``replies.confirmation``, deliberately not a second formatter.
"""

from __future__ import annotations

from datetime import datetime

from miya.bot import replies
from miya.bot.formatting import bullet_list, clip, clock, escape, short_date
from miya.config import settings
from miya.services.persistence import Applied

# One poll can apply a whole day of windows after an outage. Ten short receipts
# is fine to read one by one; beyond that the rest goes out as one summary.
MAX_DETAILED = 10

HEADER = "💬 <b>Telegramdan yozib olindi</b>"
UNKNOWN_CHAT = "Nomaʼlum chat"

COUNT_LABEL = {
    "debts": "qarz",
    "settlements": "to'lov",
    "questions": "savol",
    "promises": "va'da",
    "fulfilled": "bajarilgan va'da",
    "transactions": "pul harakati",
    "events": "uchrashuv",
    "tasks": "vazifa",
    "claims": "da'vo",
    "codes": "mijoz kodi",
}


def counts_of(applied: Applied) -> dict[str, int]:
    """How many of each kind landed — what the overflow summary is built from.

    A repayment both sides could have made, or a fulfilment that matched no
    promise clearly, is a *question* for the owner rather than a row; both
    are counted apart so the summary says one is waiting. Every field that
    makes ``Applied.is_empty()`` false is counted here — a window that only
    closed a promise is a receipt, and must not be summarised as one with no
    counts. A counterparty's claim is a question too, but its own kind: the
    summary says how many are waiting for a word, not that money moved.
    """
    counts = {
        "debts": len(applied.debts),
        "settlements": len(applied.settlements) + len(applied.unmatched_settlements),
        "questions": len(applied.ambiguous_settlements)
        + len(applied.unmatched_fulfilments)
        + len(applied.unknown_codes)
        + len(applied.identity_conflicts)
        + len(applied.owner_named),
        "promises": len(applied.promises),
        "fulfilled": len(applied.fulfilled),
        "transactions": len(applied.transactions) + len(applied.matched_transactions),
        "events": len(applied.events),
        "tasks": len(applied.tasks),
        "claims": len(applied.claims),
        "codes": len(applied.codes_learned),
    }
    return {kind: n for kind, n in counts.items() if n}


def source_of(chat_title: str | None, people: list[str]) -> tuple[str, str]:
    """``(chat, who)`` for the header, without saying a name twice.

    A private chat is titled after the person already; a chat the userbot has
    not synced yet is named after the first person instead of a bare id.
    """
    chat = chat_title or (people[0] if people else UNKNOWN_CHAT)
    who = ", ".join(name for name in people if name != chat)
    return chat, who


def window_notice(
    *,
    chat_title: str | None,
    people: list[str],
    ended_at: datetime,
    applied: Applied,
) -> str:
    """One receipt per applied window: where it came from, then what landed."""
    chat, who = source_of(chat_title, people)
    ended = ended_at.astimezone(settings.tz)
    source = f"<b>{escape(chat)}</b>"
    if who:
        source += f" · {escape(who)}"
    source += f" · {short_date(ended.date())} {clock(ended)}"
    return clip(f"{HEADER}\n{source}\n\n{replies.confirmation(applied)}")


def overflow_summary(items: list[tuple[str, dict[str, int]]]) -> str:
    """The receipts past MAX_DETAILED, folded into one message.

    ``items`` is ``(chat, counts)`` per window; the summary gives totals per
    chat and points at the commands that hold the detail, so a backlog after
    an outage costs the owner one message rather than a hundred.
    """
    per_chat: dict[str, dict[str, int]] = {}
    for chat, counts in items:
        totals = per_chat.setdefault(chat or UNKNOWN_CHAT, {})
        for kind, n in counts.items():
            totals[kind] = totals.get(kind, 0) + n

    lines = [
        f"<b>{escape(chat)}</b>: "
        + ", ".join(f"{n} {COUNT_LABEL.get(kind, kind)}" for kind, n in totals.items())
        for chat, totals in per_chat.items()
    ]
    return clip(
        f"📨 <b>Yana {len(items)} ta suhbatdan yozib olindi</b>\n"
        + bullet_list(lines, empty="—")
        + "\n<i>Batafsil: /qarz, /vada, /bugun</i>"
    )
