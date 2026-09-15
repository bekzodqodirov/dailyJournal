"""Backfill one chat's recent history.

The always-on userbot deliberately reads **no** history on its own — that
restraint is what keeps it defensible. Reading backwards happens only when the
owner asks for it, in one of two ways:

* by hand: ``make backfill CHAT=… DAYS=…`` (``main`` below), one named chat,
  a bounded number of days;
* with one tap: "Yangi guruh: … — o'qiymi?" → Ha in the bot records a request
  on the chat's monitor row (``chats.accept_join``), and the userbot's sweep
  performs it through ``backfill_chat`` on its next pass.

The Telethon history calls live here, outside ``miya/userbot/``, so that
package stays asserted to read history in exactly one reviewable place (its
sweep delegates to this module rather than calling ``iter_messages`` itself).

Messages already stored are skipped, so running it twice is harmless.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta

from miya.config import settings
from miya.db.session import engine

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


def _as_target(chat: str | int) -> str | int:
    """A chat id stays an int (Telethon needs the type); a title or @name a str."""
    if isinstance(chat, int):
        return chat
    return int(chat) if _is_id(chat) else chat


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
    parser.add_argument("chat", help="chat id, @username, or exact title")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        stored = asyncio.run(backfill(args.chat, args.days, limit=args.limit))
    except SystemExit as exc:
        print(exc.code, file=sys.stderr)
        return 1
    print(f"{stored} message(s) stored; the worker will window them shortly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
