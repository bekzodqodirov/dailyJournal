"""Channel-agnostic chat keys (docs/jarvis-plan.md §3.5).

Every Jarvis table names a conversation by one text key: ``tg:<marked id>``
for Telegram (the chat id as Telethon reports it, negative for groups),
``wa:<jid>`` for WhatsApp and ``mail:<thread id>`` for Gmail. The prefix
says the channel; nothing else in a key is interpreted outside its channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Channel = Literal["telegram", "whatsapp", "gmail"]

TELEGRAM: Final[Channel] = "telegram"
WHATSAPP: Final[Channel] = "whatsapp"
GMAIL: Final[Channel] = "gmail"
CHANNELS: Final[tuple[Channel, ...]] = (TELEGRAM, WHATSAPP, GMAIL)

PREFIX: Final[dict[Channel, str]] = {TELEGRAM: "tg", WHATSAPP: "wa", GMAIL: "mail"}
CHANNEL_OF_PREFIX: Final[dict[str, Channel]] = {p: c for c, p in PREFIX.items()}

# How the owner may name a channel in a command (``/jarvis yoq telegram``).
CHANNEL_WORDS: Final[dict[str, Channel]] = {
    "telegram": TELEGRAM,
    "tg": TELEGRAM,
    "телеграм": TELEGRAM,
    "whatsapp": WHATSAPP,
    "wa": WHATSAPP,
    "watsap": WHATSAPP,
    "vatsap": WHATSAPP,
    "gmail": GMAIL,
    "mail": GMAIL,
    "pochta": GMAIL,
    "email": GMAIL,
}


class BadChatKey(ValueError):
    """Not a ``<prefix>:<id>`` key of a known channel."""


@dataclass(frozen=True, slots=True)
class ChatKey:
    channel: Channel
    ident: str

    def __str__(self) -> str:
        return f"{PREFIX[self.channel]}:{self.ident}"


def telegram(chat_id: int) -> str:
    """``tg:<marked id>``."""
    return f"{PREFIX[TELEGRAM]}:{int(chat_id)}"


def whatsapp(jid: str) -> str:
    return f"{PREFIX[WHATSAPP]}:{_ident(jid)}"


def gmail(thread_id: str) -> str:
    return f"{PREFIX[GMAIL]}:{_ident(thread_id)}"


def _ident(value: str) -> str:
    ident = (value or "").strip()
    if not ident:
        raise BadChatKey("empty id")
    return ident


def parse(key: str) -> ChatKey:
    prefix, sep, ident = (key or "").partition(":")
    channel = CHANNEL_OF_PREFIX.get(prefix)
    if not sep or channel is None or not ident.strip():
        raise BadChatKey(key)
    if channel == TELEGRAM:
        try:
            int(ident)
        except ValueError:
            raise BadChatKey(key) from None
    return ChatKey(channel=channel, ident=ident)


def channel_of(key: str) -> Channel:
    return parse(key).channel


def tg_chat_id(key: str) -> int | None:
    """The Telegram chat id of a ``tg:`` key; None for any other channel."""
    parsed = parse(key)
    return int(parsed.ident) if parsed.channel == TELEGRAM else None


def channel_word(word: str) -> Channel | None:
    """A channel named in the owner's words, or None."""
    return CHANNEL_WORDS.get((word or "").strip().casefold())


__all__ = [
    "CHANNELS",
    "CHANNEL_WORDS",
    "GMAIL",
    "TELEGRAM",
    "WHATSAPP",
    "BadChatKey",
    "Channel",
    "ChatKey",
    "channel_of",
    "channel_word",
    "gmail",
    "parse",
    "telegram",
    "tg_chat_id",
    "whatsapp",
]
