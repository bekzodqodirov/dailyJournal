"""Assistant bot entrypoint (aiogram 3, long polling)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from miya.bot.handlers import build_reject_router, router
from miya.config import settings
from miya.db.session import engine

log = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    # Registered last: only reached when the owner filter above rejected the update.
    dp.include_router(build_reject_router())
    return dp


async def run() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if not settings.assistant_bot_token:
        raise SystemExit("ASSISTANT_BOT_TOKEN is not set")
    if not settings.owner_telegram_id:
        raise SystemExit(
            "OWNER_TELEGRAM_ID is not set — refusing to start an unrestricted bot"
        )

    bot = Bot(
        token=settings.assistant_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    me = await bot.get_me()
    log.info(
        "assistant bot @%s ready (owner=%s)", me.username, settings.owner_telegram_id
    )

    try:
        # Drop updates queued while the bot was down; MIYA logs the present.
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
