"""Backfill one chat's recent history (``make backfill CHAT=… DAYS=…``).

The always-on userbot deliberately reads **no** history — that restraint is
what keeps it defensible. This tool is the explicit, owner-initiated exception:
one named chat, a bounded number of days, run by hand. It lives outside
``miya/userbot/`` on purpose, since that package is asserted to contain no
history-reading calls at all.

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
from miya.userbot.main import display_name_of, ingest_message

log = logging.getLogger(__name__)


async def backfill(chat: str, days: int, *, limit: int = 2000) -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not (
        settings.telethon_api_id
        and settings.telethon_api_hash
        and settings.telethon_session
    ):
        raise SystemExit("TELETHON_* settings are required for backfill")

    since = datetime.now(settings.tz) - timedelta(days=days)
    client = TelegramClient(
        StringSession(settings.telethon_session),
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    await client.connect()
    stored = 0
    try:
        if not await client.is_user_authorized():
            raise SystemExit("TELETHON_SESSION is not authorised")

        entity = await client.get_entity(int(chat) if _is_id(chat) else chat)
        log.info(
            "backfilling %s from %s", display_name_of(entity), since.date().isoformat()
        )
        # The chat must still be monitor_enabled — backfill does not override
        # the owner's `/chats` choices, it only reaches further back in time.
        async for message in client.iter_messages(entity, limit=limit):
            if message.date.astimezone(settings.tz) < since:
                break
            if await ingest_message(client, message):
                stored += 1
    finally:
        await client.disconnect()
        await engine.dispose()

    log.info("backfill stored %d message(s)", stored)
    return stored


def _is_id(value: str) -> bool:
    return value.lstrip("-").isdigit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat", help="chat id, @username, or exact title")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=2000)
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
