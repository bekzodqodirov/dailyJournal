"""Assistant bot entrypoint (aiogram 3, long polling)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types.error_event import ErrorEvent

from miya.bot.handlers import build_reject_router, router
from miya.config import settings
from miya.db.session import engine

log = logging.getLogger(__name__)

ERROR_REPLY = "⚠️ Xatolik yuz berdi — birozdan keyin qaytadan urinib ko'ring."


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    # Registered last: only reached when the owner filter above rejected the update.
    dp.include_router(build_reject_router())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        # Without this, an unhandled exception (the database being down,
        # foremost) means the owner's message just disappears and a pressed
        # button spins forever. The data may be lost; the silence must not be.
        log.exception("unhandled error in an update", exc_info=event.exception)
        update = event.update
        callback = getattr(update, "callback_query", None)
        message = getattr(update, "message", None)
        try:
            if callback is not None:
                await callback.answer(ERROR_REPLY, show_alert=True)
            elif message is not None:
                await message.answer(ERROR_REPLY, parse_mode=None)
        except Exception:
            log.exception("could not even report the error to the owner")

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
    # Not fatal — extraction falls back to needs_review and reports fall back
    # to their data block — but the owner deserves one loud line, not a
    # mystery three jobs deep.
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is empty — extraction and RAG will fail")
    if not settings.elevenlabs_api_key:
        log.warning("ELEVENLABS_API_KEY is empty — transcription will fail")

    bot = Bot(
        token=settings.assistant_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    # A container regularly starts before the network is up; one transient
    # DNS failure must not burn a restart-policy attempt.
    for attempt in range(5):
        try:
            me = await bot.get_me()
            break
        except Exception as exc:
            wait = 2**attempt * 5
            log.warning("get_me failed (%s); retrying in %ds", exc, wait)
            await asyncio.sleep(wait)
    else:
        raise SystemExit("could not reach Telegram after 5 attempts")
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
