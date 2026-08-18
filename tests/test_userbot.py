"""Userbot safety and classification (spec §7B).

Telethon is never connected here: the message objects are stand-ins with the
attributes Telethon exposes, which is all the classification code touches. The
first test is the important one — it is what keeps the userbot passive.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from miya.db.enums import ChatType
from miya.services.media_policy import MediaKind
from miya.userbot import main as userbot

USERBOT_PACKAGE = Path(userbot.__file__).parent

# Anything that would write to Telegram, mark chats read, or bulk-download
# history. The spec is explicit: the userbot reads, and does nothing else.
FORBIDDEN_CALLS = {
    "send_message",
    "send_file",
    "send_read_acknowledge",
    "edit_message",
    "delete_messages",
    "forward_messages",
    "get_messages",  # would pull history; only live events are allowed
    "iter_messages",
}


def _called_attributes(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


@pytest.mark.parametrize("path", sorted(USERBOT_PACKAGE.glob("*.py")))
def test_the_userbot_never_writes_to_telegram(path):
    called = _called_attributes(path.read_text())
    assert not (called & FORBIDDEN_CALLS), (
        f"{path.name} calls {sorted(called & FORBIDDEN_CALLS)} — the userbot "
        "must stay strictly read-only (spec §7B)"
    )


def test_the_kill_switch_stops_the_process_before_it_connects(monkeypatch):
    """USERBOT_ENABLED=false must return without touching Telethon at all."""
    import asyncio

    from miya.config import settings

    monkeypatch.setattr(settings, "userbot_enabled", False)

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("TelegramClient was constructed with the switch off")

    monkeypatch.setattr(userbot, "TelegramClient", _explode)
    asyncio.run(userbot.run())


def test_missing_credentials_fail_loudly_instead_of_prompting(monkeypatch):
    import asyncio

    from miya.config import settings

    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_session", "")

    with pytest.raises(SystemExit):
        asyncio.run(userbot.run())


# --- classification ----------------------------------------------------------


def _message(**kwargs) -> SimpleNamespace:
    fields = {
        "sticker": None,
        "gif": None,
        "voice": None,
        "audio": None,
        "video_note": None,
        "photo": None,
        "video": None,
        "document": None,
    }
    return SimpleNamespace(**{**fields, **kwargs})


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_message(), MediaKind.text),
        (_message(voice=object()), MediaKind.voice),
        (_message(video_note=object()), MediaKind.video_note),
        (_message(photo=object()), MediaKind.photo),
        (_message(video=object(), document=object()), MediaKind.video),
        (_message(document=object()), MediaKind.document),
        (_message(sticker=object()), MediaKind.sticker),
        # A GIF arrives as a document with an animation attribute; it must not
        # be mistaken for a business document.
        (_message(gif=object(), document=object()), MediaKind.sticker),
    ],
)
def test_message_classification(message, expected):
    assert userbot.kind_of(message) is expected


def test_chat_types_map_from_telethon_entities():
    from telethon.tl.types import Channel, Chat, User

    user = User(id=1)
    group = Chat(
        id=2, title="Ish", photo=None, participants_count=3, date=None, version=1
    )
    supergroup = Channel(id=3, title="Katta guruh", photo=None, date=None, megagroup=True)
    channel = Channel(id=4, title="Kanal", photo=None, date=None, broadcast=True)

    assert userbot.chat_type_of(user) is ChatType.private
    assert userbot.chat_type_of(group) is ChatType.group
    assert userbot.chat_type_of(supergroup) is ChatType.group
    assert userbot.chat_type_of(channel) is ChatType.channel


def test_display_names_fall_back_through_what_telegram_gave_us():
    assert userbot.display_name_of(
        SimpleNamespace(first_name="Akmal", last_name="G")
    ) == ("Akmal G")
    assert (
        userbot.display_name_of(
            SimpleNamespace(first_name=None, last_name=None, username="akmal_gz")
        )
        == "akmal_gz"
    )
    anonymous = SimpleNamespace(first_name=None, last_name=None, username=None, id=77)
    assert userbot.display_name_of(anonymous) == "tg:77"
