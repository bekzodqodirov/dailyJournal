"""Assistant bot entrypoint (aiogram 3, long polling)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiogram.types.error_event import ErrorEvent
from sqlalchemy import exc as sa_exc

from miya.bot import replies
from miya.bot.formatting import clip
from miya.bot.handlers import build_reject_router, router
from miya.config import settings
from miya.db.session import engine, session_scope
from miya.services import health, reminders

log = logging.getLogger(__name__)

ERROR_REPLY = "⚠️ Xatolik yuz berdi — birozdan keyin qaytadan urinib ko'ring."

# --- the watchdog (build step 5) --------------------------------------------
#
# The worker is the only process that speaks unprompted, so the worker dying
# is the one failure nothing would ever report. The bot is the second pair
# of eyes: every WATCHDOG_MINUTES it beats its own heartbeat, reads the
# worker's, and tells the owner when that one has gone quiet — through the
# same alert ledger the worker's health job uses, so neither repeats the
# other and one recovery notice goes out when the worker is back. The bot
# never imports the worker: it only shares the database with it.

WATCHDOG_MINUTES = 5
WATCHDOG_PROBLEM = "worker_silent"
DB_DOWN_KEY = "db_down"

# When the database itself does not answer the ledger cannot be consulted,
# so "Baza javob bermayapti" is throttled in memory: {key: last sent at}.
# Lost on restart, which is right — a restarted bot should say it again.
_memo: dict[str, datetime] = {}
# What every "bot" heartbeat says about this process (the detail is replaced
# whole on each beat, so the watchdog must repeat the startup facts).
_identity: dict[str, object] = {}


async def _send(bot: Bot, text: str) -> bool:
    """Send to the owner; retry as plain text if Telegram rejects the HTML.

    The same shape as the worker's notify(): True only when Telegram took
    the message, so the caller records only what was actually delivered.
    """
    try:
        await bot.send_message(settings.owner_telegram_id, clip(text))
        return True
    except Exception:
        log.warning("HTML send failed, retrying as plain text", exc_info=True)
    try:
        await bot.send_message(settings.owner_telegram_id, clip(text), parse_mode=None)
        return True
    except Exception:
        log.exception("could not reach the owner at all")
        return False


async def _beat(now: datetime | None = None) -> None:
    async with session_scope() as session:
        await health.beat(session, "bot", detail=dict(_identity), now=now)


async def watchdog_tick(bot: Bot, *, now: datetime | None = None) -> None:
    """One pass: beat, judge the worker, say what changed. Never raises.

    Alerts and recoveries go through health.alerts_due, so a problem is
    repeated no more often than ALERT_REPEAT_HOURS and recovery is said
    once, whichever process (this one or the worker's health job) wrote the
    last row. Only the worker's key is this watchdog's to speak for — the
    ledger also holds keys the worker alerted on, and their recovery is the
    worker's to report.

    A silent worker is a warning, and warnings wait out the quiet hours:
    nothing is sent and nothing is marked, so the first tick after them says
    it. The database being down is critical and is said at any hour.
    """
    now = now or datetime.now(settings.tz)
    quiet = reminders.in_quiet_hours(now)
    try:
        async with session_scope() as session:
            await health.beat(session, "bot", detail=dict(_identity), now=now)
            rows = await health.beats(session)
            worker = health.component_of("worker", rows.get("worker"), now=now)
            found: list[health.Problem] = []
            if worker.stale:
                found.append(
                    health.Problem(
                        WATCHDOG_PROBLEM,
                        "warning",
                        replies.worker_silent_alert(worker),
                    )
                )
            due, _recovered = await health.alerts_due(session, found, now=now)
            if quiet and due:
                log.info("watchdog: inside quiet hours — holding the worker notice")
                due = []
            for problem in due:
                if await _send(bot, problem.text):
                    health.mark_alerted(session, [problem.key], now=now)
            # The recovery is the worker's to announce: its health job runs
            # again the moment it is back and reads the same ledger, so two
            # tickers never race to say "tiklandi" twice.
    except (sa_exc.DBAPIError, sa_exc.OperationalError, OSError, TimeoutError):
        log.exception("watchdog: the database did not answer")
        last = _memo.get(DB_DOWN_KEY)
        repeat = timedelta(hours=settings.alert_repeat_hours)
        due_again = last is None or now - last > repeat
        if due_again and await _send(bot, replies.DB_DOWN_ALERT):
            _memo[DB_DOWN_KEY] = now
        return
    if _memo.pop(DB_DOWN_KEY, None) is not None:
        # The last tick could not reach the database and said so; this one
        # could. The in-memory memo is the only record, so the recovery is
        # not recorded either — losing it costs one repeated line at worst.
        await _send(bot, health.recovery_text(DB_DOWN_KEY))


async def watchdog_loop(bot: Bot) -> None:
    """Sleep first: the startup beat in run() already covered this minute."""
    while True:
        await asyncio.sleep(WATCHDOG_MINUTES * 60)
        await watchdog_tick(bot)


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
    # The bot embeds through the api with this token; a blank or short one
    # would only surface later as a mystery 401/503.
    if problem := settings.api_token_problem():
        raise SystemExit(problem)
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
    _identity["username"] = me.username
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in replies.COMMAND_MENU]
        )
    except TelegramAPIError:
        # The menu is a convenience; a network blip must not stop the bot.
        log.warning("could not set the command menu", exc_info=True)
    try:
        await _beat()
    except Exception:
        # The database being down at startup is exactly what the watchdog
        # will report in five minutes; it must not stop the bot from polling.
        log.exception("could not write the startup heartbeat")

    watchdog = asyncio.create_task(watchdog_loop(bot), name="watchdog")
    try:
        # Deliberately NOT drop_pending_updates. Container restarts are routine
        # here — a rebuild, a crash, a VPS reboot — and anything the owner sent
        # during that window is a note, a debt or a promise that exists nowhere
        # else. Dropping it would break the one guarantee the whole system
        # rests on: nothing the owner said is ever silently lost.
        #
        # The backlog cannot run away: Telegram keeps undelivered updates for
        # 24 hours, and this bot serves exactly one person, so the worst case
        # is one day of the owner's own messages — which is precisely the data
        # that must not be thrown away.
        await dp.start_polling(bot, drop_pending_updates=False)
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
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
