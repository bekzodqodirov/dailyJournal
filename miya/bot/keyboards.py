"""Inline keyboards for the assistant bot.

Callback payloads are deliberately terse — Telegram caps `callback_data` at 64
bytes, and a chat list page carries one payload per button.

    ch:t:<monitor_id>:<field>   toggle one flag on one chat
    ch:p:<page>                 jump to a page of the chat list
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
