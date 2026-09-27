"""WP-13: every command the owner can type is in Telegram's "/" menu."""

from __future__ import annotations

import re

from aiogram.filters import Command

from miya.bot import handlers, replies

NOT_IN_MENU = {"start", "help", "va_da", "process"}


def _commands() -> list[str]:
    names = []
    for handler in handlers.router.message.handlers:
        for flt in handler.filters or []:
            if isinstance(flt.callback, Command):
                names.append(flt.callback.commands[0])
    return names


def test_every_command_has_a_menu_entry():
    menu = {name for name, _ in replies.COMMAND_MENU}
    missing = [c for c in _commands() if c not in NOT_IN_MENU and c not in menu]
    assert missing == []
    assert {"pul", "ochir"} <= menu


def test_menu_entries_are_valid_for_telegram():
    for name, description in replies.COMMAND_MENU:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 1 <= len(description) <= 256, name
