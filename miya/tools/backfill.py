"""Backfill one chat's recent history.

The always-on userbot deliberately reads **no** history on its own — that
restraint is what keeps it defensible. Reading backwards happens only when the
owner asks for it, in one of two ways:

* by hand: ``make backfill CHAT=… DAYS=…`` (``main`` below), one named chat,
  a bounded number of days;
* with one tap: "Yangi guruh: … — o'qiymi?" → Ha in the bot records a request
  on the chat's monitor row (``chats.accept_join``), and the userbot's sweep
  performs it through ``backfill_chat`` on its next pass;
* for the owner's style (JV-14): ``/uslub yukla`` records a request on every
  monitored private chat (``chats.request_style_import``), and the sweep
  reads each one's newest messages through ``fetch_style_history``.

The Telethon history calls the owner asked for live here; this module
imports Telethon, so the userbot AST guard (``tests/test_userbot.py``) scans
it too and allows history reads only in the functions it names
(``catch_up_chat``, ``backfill_chat``, ``archive_chat``,
``fetch_style_history``), next to the userbot's own ``fetch_approved`` and
``sender.reconcile`` (the userbot's sweep delegates to this module rather
than calling ``iter_messages`` itself).

Messages already stored are skipped, so running it twice is harmless.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from miya.config import settings
from miya.db.session import engine
from miya.logs import setup_logging

log = logging.getLogger(__name__)

# One backfill reads at most this many messages, however busy the group.
DEFAULT_LIMIT = 2000


async def backfill_chat(
    client, chat: str | int, days: int, *, limit: int = DEFAULT_LIMIT
) -> int:
    """Read ``days`` of one chat back through the live ingestion path.

    ``client`` is a connected, authorised Telethon client. The chat must be
    ``monitor_enabled`` — ``ingest_message`` refuses anything else, so a
    backfill never overrides the owner's /chats choices, it only reaches
    further back in time. Returns how many messages were stored.
    """
    # Imported here, not at module level: the userbot's sweep imports this
    # module, and this module needs the userbot's ingestion path.
    from miya.userbot.main import display_name_of, ingest_message

    since = datetime.now(settings.tz) - timedelta(days=days)
    entity = await client.get_entity(_as_target(chat))
    log.info("backfilling %s from %s", display_name_of(entity), since.date().isoformat())
    stored = 0
    async for message in client.iter_messages(entity, limit=limit):
        if message.date.astimezone(settings.tz) < since:
            break
        if await ingest_message(client, message):
            stored += 1
    log.info("backfill stored %d message(s)", stored)
    return stored


# A FloodWait longer than this ends the pass; the next sweep resumes.
FLOOD_WAIT_MAX_SECONDS = 60


async def catch_up_chat(
    client, chat_id: int, *, after_id: int | None, since: datetime, limit: int
) -> tuple[int, int]:
    """Read one allowed chat forward from ``after_id`` (WP-21).

    Never reads before ``since`` (the moment the chat was switched on).
    Returns (stored, the newest message id seen); on a long FloodWait it
    returns what it has, and the next sweep continues from there. An owner's
    message read here takes its chat over as a live one does (JV-17), a
    sticker or GIF included: what they sent while the reader was down still
    cancels Jarvis's drafts.
    """
    from telethon.errors import FloodWaitError

    from miya.userbot.main import ingest_message, note_owner_message, note_owner_sticker

    kwargs = {"reverse": True, "limit": limit}
    if after_id:
        kwargs["min_id"] = after_id
    else:
        kwargs["offset_date"] = since
    stored = top = 0
    while True:
        try:
            async for message in client.iter_messages(chat_id, **kwargs):
                if message.date.astimezone(settings.tz) < since:
                    continue
                if await ingest_message(client, message):
                    stored += 1
                    if getattr(message, "out", False):
                        await note_owner_message(message)
                elif getattr(message, "out", False):
                    await note_owner_sticker(message)
                top = max(top, message.id)
            return stored, top
        except FloodWaitError as exc:
            if exc.seconds > FLOOD_WAIT_MAX_SECONDS:
                log.warning(
                    "catch-up of %s paused by FloodWait %ss", chat_id, exc.seconds
                )
                return stored, top
            await asyncio.sleep(exc.seconds)
            if top:
                kwargs.pop("offset_date", None)
                kwargs["min_id"] = top


@dataclass(slots=True)
class ArchiveCounts:
    """What one chat's archive import did (WP-76)."""

    title: str
    stored: int = 0
    transcribed: int = 0
    skipped: int = 0
    paused: bool = False


async def archive_chat(
    client,
    chat_id: int,
    *,
    since: datetime,
    transcribe: bool = False,
    title: str = "",
) -> ArchiveCounts:
    """Import one allowed chat's history back to ``since`` for search only:
    stored processed, never extracted (WP-76). Newest first; messages
    already stored are skipped, so a stopped run resumes by running again."""
    from telethon.errors import FloodWaitError

    from miya.userbot.main import _SPOKEN_KINDS, ingest_message, kind_of

    counts = ArchiveCounts(title=title or str(chat_id))
    offset_id = 0
    while True:
        try:
            async for message in client.iter_messages(chat_id, offset_id=offset_id):
                if message.date.astimezone(settings.tz) < since:
                    return counts
                offset_id = message.id
                if not await ingest_message(
                    client, message, archive=True, transcribe_media=transcribe
                ):
                    continue
                counts.stored += 1
                if kind_of(message) in _SPOKEN_KINDS:
                    if transcribe:
                        counts.transcribed += 1
                    else:
                        counts.skipped += 1
            return counts
        except FloodWaitError as exc:
            if exc.seconds > FLOOD_WAIT_MAX_SECONDS:
                log.warning("archive of %s paused by FloodWait %ss", chat_id, exc.seconds)
                counts.paused = True
                return counts
            await asyncio.sleep(exc.seconds)


async def archive_private_chats(
    client, days: int, *, transcribe: bool = False
) -> list[ArchiveCounts]:
    """Every allowed private chat, ``days`` back (WP-76)."""
    import sqlalchemy as sa

    from miya.db.enums import ChatType
    from miya.db.models import ChatMonitor
    from miya.db.session import session_scope

    since = datetime.now(settings.tz) - timedelta(days=days)
    async with session_scope() as session:
        chats = list(
            (
                await session.execute(
                    sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title).where(
                        ChatMonitor.monitor_enabled.is_(True),
                        ChatMonitor.chat_type == ChatType.private,
                    )
                )
            ).all()
        )
    results = []
    for chat_id, title in chats:
        counts = await archive_chat(
            client, chat_id, since=since, transcribe=transcribe, title=title or ""
        )
        log.info(
            "archived %s: %d stored, %d transcribed, %d skipped",
            counts.title,
            counts.stored,
            counts.transcribed,
            counts.skipped,
        )
        results.append(counts)
    return results


# The style history import (JV-14): per chat, at most this many of the
# newest messages are read (the owner's lines and the client lines they
# answer), and a run stops trying after this many messages that failed to
# store, as the chat's failure.
STYLE_HISTORY_LIMIT = 300
STYLE_HISTORY_MAX_ERRORS = 5


class StyleImportError(RuntimeError):
    """The chat's history could not be stored; the sweep counts a failure."""


@dataclass(slots=True)
class StyleHistory:
    """What one chat's style import read (JV-14)."""

    stored: int = 0
    seen: int = 0
    failed: int = 0
    # A long FloodWait ended the pass: the chat is not done, and Telegram
    # asked for this many seconds before the next history call.
    paused: bool = False
    wait_seconds: int = 0


async def fetch_style_history(
    client, chat_id: int, *, limit: int = STYLE_HISTORY_LIMIT
) -> StyleHistory:
    """Read one allowed private chat's newest ``limit`` messages for the
    owner's style (JV-14): stored as archive rows, searchable, never
    extracted and never asked about; media is kept as metadata only (nothing
    is downloaded, transcribed or read by a model). Read-only:
    nothing is sent, marked read or changed in Telegram.

    Messages already stored are skipped. A message that fails to store is
    logged with its ids and the rest go on; more than
    STYLE_HISTORY_MAX_ERRORS of them raise ``StyleImportError``. A FloodWait
    up to FLOOD_WAIT_MAX_SECONDS is slept through and the read resumes
    where it stopped; a longer one ends the pass with ``paused`` and the
    wait in ``wait_seconds``.
    """
    from telethon.errors import FloodWaitError

    from miya.userbot.main import ingest_message

    counts = StyleHistory()
    offset_id = 0
    while True:
        try:
            async for message in client.iter_messages(
                chat_id, limit=max(limit - counts.seen, 0), offset_id=offset_id
            ):
                offset_id = message.id
                counts.seen += 1
                try:
                    stored = await ingest_message(
                        client,
                        message,
                        archive=True,
                        transcribe_media=False,
                        read_media=False,
                    )
                except Exception as exc:
                    counts.failed += 1
                    log.error(
                        "style import could not store a message",
                        extra={
                            "tg_chat_id": chat_id,
                            "tg_message_id": getattr(message, "id", None),
                            "error": type(exc).__name__,
                        },
                    )
                    if counts.failed > STYLE_HISTORY_MAX_ERRORS:
                        raise StyleImportError(str(chat_id)) from exc
                    continue
                if stored:
                    counts.stored += 1
            return counts
        except FloodWaitError as exc:
            if exc.seconds > FLOOD_WAIT_MAX_SECONDS:
                log.warning(
                    "style import paused by FloodWait",
                    extra={"tg_chat_id": chat_id, "seconds": exc.seconds},
                )
                counts.paused = True
                counts.wait_seconds = exc.seconds
                return counts
            await asyncio.sleep(exc.seconds)
            if counts.seen >= limit:
                return counts


ARCHIVE_SUMMARY = (
    "📥 {chat}: {n} ta eski xabar arxivga olindi (qarz/va'da sifatida "
    "o'qilmadi, faqat qidiruv uchun)."
)


def _as_target(chat: str | int) -> str | int:
    """A chat id stays an int (Telethon needs the type); a title or @name a str."""
    if isinstance(chat, int):
        return chat
    return int(chat) if _is_id(chat) else chat


async def _connected():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not (
        settings.telethon_api_id
        and settings.telethon_api_hash
        and settings.telethon_session
    ):
        raise SystemExit("TELETHON_* settings are required for backfill")
    client = TelegramClient(
        StringSession(settings.telethon_session),
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("TELETHON_SESSION is not authorised")
    return client


async def archive(days: int, *, transcribe: bool) -> list[ArchiveCounts]:
    """The command-line archive import: every allowed private chat."""
    client = await _connected()
    try:
        return await archive_private_chats(client, days, transcribe=transcribe)
    finally:
        await client.disconnect()
        await engine.dispose()


async def backfill(chat: str, days: int, *, limit: int = DEFAULT_LIMIT) -> int:
    """The command-line form: connect, backfill one chat, disconnect."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not (
        settings.telethon_api_id
        and settings.telethon_api_hash
        and settings.telethon_session
    ):
        raise SystemExit("TELETHON_* settings are required for backfill")

    client = TelegramClient(
        StringSession(settings.telethon_session),
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("TELETHON_SESSION is not authorised")
        return await backfill_chat(client, chat, days, limit=limit)
    finally:
        await client.disconnect()
        await engine.dispose()


def _is_id(value: str) -> bool:
    return value.lstrip("-").isdigit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat", nargs="?", help="chat id, @username, or exact title")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--archive",
        action="store_true",
        help="store for search only: no extraction, no questions (WP-76)",
    )
    parser.add_argument(
        "--all-private", action="store_true", help="every allowed private chat"
    )
    parser.add_argument(
        "--transcribe", action="store_true", help="also transcribe voice (costs money)"
    )
    args = parser.parse_args(argv)

    setup_logging("backfill")
    if args.archive:
        if not args.days or not args.all_private:
            print("--archive needs --all-private and --days N", file=sys.stderr)
            return 2
        try:
            results = asyncio.run(archive(args.days, transcribe=args.transcribe))
        except SystemExit as exc:
            print(exc.code, file=sys.stderr)
            return 1
        for counts in results:
            tail = " (FloodWait: qayta ishga tushiring)" if counts.paused else ""
            print(
                f"{counts.title}: {counts.stored} stored, {counts.transcribed} "
                f"transcribed, {counts.skipped} skipped{tail}"
            )
        return 0
    if not args.chat:
        print("a chat is required (or --archive --all-private)", file=sys.stderr)
        return 2
    try:
        stored = asyncio.run(backfill(args.chat, args.days or 7, limit=args.limit))
    except SystemExit as exc:
        print(exc.code, file=sys.stderr)
        return 1
    print(f"{stored} message(s) stored; the worker will window them shortly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
