"""Scheduler process (APScheduler).

Jobs:
  * reminders    — hourly, on the hour; quiet-hours aware (Phase 1)
  * call_scan    — every minute; ingests new call recordings (Phase 2)
  * retention    — daily at 04:15; deletes audio past AUDIO_RETENTION_DAYS
  * embed        — every 2 min; backfills bge-m3 vectors for new memories
  * daily_report — cron at REPORT_TIME; composes and sends the day's report
  * gcal_pull    — every GCAL_PULL_MINUTES; Google → events (when authed)
  * gcal_push    — every 5 min; extracted events → Google (when authed)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from miya.bot import replies
from miya.bot.formatting import clip
from miya.config import settings
from miya.db.session import engine, session_scope
from miya.services import call_recordings, gcal, memories, reminders, reports
from miya.services.embeddings import EmbeddingError, get_embedder

log = logging.getLogger(__name__)


async def reminder_job(bot: Bot) -> None:
    """Ping the owner about anything due. Silent during quiet hours."""
    if reminders.in_quiet_hours():
        log.debug("inside quiet hours — skipping reminder sweep")
        return

    async with session_scope() as session:
        bundle = await reminders.collect_due(session)
        if bundle.is_empty():
            return

        body = replies.reminder(
            bundle.debts, bundle.promises, bundle.tasks, bundle.events
        )
        if not body:
            return

        await bot.send_message(settings.owner_telegram_id, f"⏰ <b>Eslatma</b>\n\n{body}")
        # Recorded only after a successful send, so a failed ping is retried.
        await reminders.mark_sent(session, bundle)

    log.info(
        "sent reminder: %d debts, %d promises, %d tasks, %d events",
        len(bundle.debts),
        len(bundle.promises),
        len(bundle.tasks),
        len(bundle.events),
    )


async def call_scan_job(bot: Bot) -> None:
    """Ingest new call recordings; tell the owner when one produced something."""
    async with session_scope() as session:
        # scan_directory commits per file, so everything below reads durable
        # state — a Telegram failure can no longer roll back paid Scribe work.
        results = await call_recordings.scan_directory(session)

    for result in results:
        filename = (result.interaction.meta or {}).get("filename", "?")
        if not result.ok:
            log.warning("recording %s flagged for review: %s", filename, result.error)
            continue
        log.info("ingested recording %s", filename)
        # Only interrupt the owner when the call actually produced facts —
        # a routine call lands silently and shows up in the daily report.
        if result.applied and not result.applied.is_empty():
            body = replies.confirmation(result.applied)
            try:
                await bot.send_message(
                    settings.owner_telegram_id,
                    f"📞 <b>Qo'ng'iroqdan yozib olindi</b>\n\n{body}",
                )
            except Exception:
                log.exception("could not notify owner about recording %s", filename)


async def retention_job() -> None:
    async with session_scope() as session:
        await call_recordings.purge_old_audio(session)


async def embed_job() -> None:
    """Backfill embeddings for memories created since the last tick."""
    async with session_scope() as session:
        try:
            embedded = await memories.embed_pending(session, get_embedder())
        except EmbeddingError as exc:
            # Rows stay NULL and are retried next tick; typical cause is the
            # api container still downloading/loading the model.
            log.warning("embedding backfill failed, will retry: %s", exc)
            return
    if embedded:
        log.info("embedded %d new memories", embedded)


async def report_job(bot: Bot) -> None:
    """Compose, store and deliver the daily report (cron at REPORT_TIME)."""
    async with session_scope() as session:
        content = await reports.generate_report(session)
    # The report is committed before the send: a Telegram failure costs the
    # notification, never the report itself (`/hisobot` re-reads it).
    try:
        await bot.send_message(
            settings.owner_telegram_id,
            clip(f"📊 <b>Kunlik hisobot</b>\n\n{content}"),
        )
    except Exception:
        log.exception("could not deliver the daily report")


async def gcal_pull_job() -> None:
    api = gcal.get_api()
    if api is None:
        log.debug("google calendar not configured — skipping pull")
        return
    async with session_scope() as session:
        changed = await gcal.pull(session, api)
    if changed:
        log.info("gcal pull: %d events created/updated", changed)


async def gcal_push_job() -> None:
    api = gcal.get_api()
    if api is None:
        return
    async with session_scope() as session:
        pushed = await gcal.push_pending(session, api)
    if pushed:
        log.info("gcal push: %d events created in Google Calendar", pushed)


async def run() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if not settings.assistant_bot_token or not settings.owner_telegram_id:
        raise SystemExit(
            "ASSISTANT_BOT_TOKEN and OWNER_TELEGRAM_ID must be set for the worker"
        )

    bot = Bot(
        token=settings.assistant_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        reminder_job,
        CronTrigger(minute=0, timezone=settings.timezone),
        args=[bot],
        id="reminders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        call_scan_job,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="call_scan",
        max_instances=1,  # a slow Scribe call must not overlap the next sweep
        coalesce=True,
    )
    scheduler.add_job(
        retention_job,
        CronTrigger(hour=4, minute=15, timezone=settings.timezone),
        id="retention",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        embed_job,
        IntervalTrigger(minutes=2),
        id="embed",
        max_instances=1,
        coalesce=True,
    )
    report_at = settings.report_time_parsed
    scheduler.add_job(
        report_job,
        CronTrigger(
            hour=report_at.hour, minute=report_at.minute, timezone=settings.timezone
        ),
        args=[bot],
        id="daily_report",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        gcal_pull_job,
        IntervalTrigger(minutes=settings.gcal_pull_minutes),
        id="gcal_pull",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        gcal_push_job,
        IntervalTrigger(minutes=5),
        id="gcal_push",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("worker started (tz=%s); jobs: %s", settings.timezone, scheduler.get_jobs())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()
        log.info("worker stopped")


def main() -> int:
    try:
        asyncio.run(run())
    except SystemExit as exc:
        if exc.code:
            print(exc.code, file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
