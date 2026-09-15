"""Scheduler process (APScheduler).

Jobs:
  * reminders    — hourly, on the hour; quiet-hours aware; escalates and then
                   asks "Hali ochiqmi?" (see services/reminders.py)
  * call_scan    — every minute; ingests new call recordings (Phase 2)
  * retention    — daily at 04:15; deletes audio past AUDIO_RETENTION_DAYS
  * embed        — every 2 min; backfills bge-m3 vectors for new memories
  * daily_report — cron at REPORT_TIME; composes and sends the day's report
  * gcal_pull    — every GCAL_PULL_MINUTES; Google → events (when authed)
  * gcal_push    — every 5 min; extracted events → Google (when authed)
  * windows      — every 5 min; flushes userbot conversation windows (Phase 4)
                   and extracts the ``instant`` ones (private chats, group
                   messages aimed at the owner) on the spot, receipt included
  * batch_submit — every BATCH_FLUSH_HOURS; pending windows → Batch API
  * batch_poll   — every 15 min; applies finished batches, then tells the
                   owner what his chats put on the ledger (quiet-hours aware)
  * backup       — cron at BACKUP_TIME; encrypted pg_dump, 14-day retention
  * morning_brief — cron at MORNING_BRIEF_TIME; today's meetings, what is due,
                   and every open loop, deterministic, never skipped
  * nudges       — every 30 min; one short message per question nobody
                   answered, with ✅ Javob berdim / ⏰ Ertaga (quiet-hours aware)
  * new_chat_ask — every 2 min; "Yangi guruh: … — o'qiymi?" once per new
                   group or channel (quiet-hours aware)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import date, datetime, time, timedelta

import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from miya.bot import keyboards, notices, replies
from miya.bot.formatting import clip, escape
from miya.config import settings
from miya.db.models import DailyReport, Person, ReminderLog
from miya.db.session import engine, session_scope
from miya.services import (
    approvals,
    backup,
    batch,
    brief,
    call_recordings,
    chats,
    gcal,
    memories,
    nudges,
    reminders,
    reports,
    windows,
)
from miya.services.embeddings import EmbeddingError, get_embedder

log = logging.getLogger(__name__)


async def notify(bot: Bot, text: str, *, reply_markup=None) -> bool:
    """Send to the owner; retry as plain text if Telegram rejects the HTML.

    Report and reminder bodies carry names and descriptions that a
    counterparty controls, and the report itself is composed by a model. A
    single stray tag must not silently cost the owner his evening summary.
    ``reply_markup`` (the reminder buttons) rides along on both attempts.
    """
    try:
        await bot.send_message(
            settings.owner_telegram_id, clip(text), reply_markup=reply_markup
        )
        return True
    except Exception:
        log.warning("HTML send failed, retrying as plain text", exc_info=True)
    try:
        await bot.send_message(
            settings.owner_telegram_id,
            clip(text),
            parse_mode=None,
            reply_markup=reply_markup,
        )
        return True
    except Exception:
        log.exception("could not reach the owner at all")
        return False


async def reminder_job(bot: Bot) -> None:
    """Ping the owner about anything due. Silent during quiet hours.

    Two messages at most: the pings, with a ✅ / ✏️ / 🔄 row per line so an
    item can be closed or corrected from the reminder itself, and the
    "Hali ochiqmi?" question for items the pings are done with.
    """
    if reminders.in_quiet_hours():
        log.debug("inside quiet hours — skipping reminder sweep")
        return

    async with session_scope() as session:
        bundle = await reminders.collect_due(session)
        if bundle.is_empty():
            return

        body, rendered = replies.reminder_with_counts(
            bundle.debts, bundle.promises, bundle.tasks, bundle.events
        )
        if body:
            keyboard = keyboards.record_actions(
                replies.reminder_refs(
                    bundle.debts, bundle.promises, bundle.tasks, rendered
                )
            )
            if not await notify(
                bot, f"⏰ <b>Eslatma</b>\n\n{body}", reply_markup=keyboard
            ):
                return
            # Recorded only after a successful send, and only for what fit: a
            # clipped item must come back next hour, not be silently suppressed.
            await reminders.mark_sent(session, bundle, rendered=rendered)
            await session.commit()

        if bundle.questions:
            # Same rule as the pings: only what fit in the message is logged
            # as asked; the tail qualifies again next sweep.
            body, shown = replies.still_open_question_with_count(bundle.questions)
            shown = min(shown, keyboards.MAX_ROWS)
            keyboard = keyboards.question_keyboard(bundle.questions[:shown])
            if not await notify(bot, body, reply_markup=keyboard):
                return
            await reminders.mark_asked(session, bundle, rendered=shown)

    log.info(
        "sent reminder: %d debts, %d promises, %d tasks, %d events, %d questions",
        len(bundle.debts),
        len(bundle.promises),
        len(bundle.tasks),
        len(bundle.events),
        len(bundle.questions),
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
            await notify(bot, f"📞 <b>Qo'ng'iroqdan yozib olindi</b>\n\n{body}")


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
    await notify(bot, f"📊 <b>Kunlik hisobot</b>\n\n{content}")


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


async def window_job(bot: Bot) -> None:
    """Group the userbot's loose messages into extractable conversations —
    and extract the urgent ones now.

    A window from a private chat, or a group window holding a message aimed
    at the owner, is flagged ``instant`` by the flush and extracted on this
    same tick at full price; the rest waits for the half-price batch. The
    receipt for what landed goes out through the same queue the batch uses,
    so quiet hours still hold it rather than drop it.
    """
    async with session_scope() as session:
        flushed = await windows.flush_ready_windows(session)
    if flushed:
        log.info(
            "window flush produced %d window(s), %d instant",
            len(flushed),
            sum(1 for w in flushed if w.instant),
        )
    async with session_scope() as session:
        outcome = await batch.extract_instant(session)
    if outcome.applied:
        await chat_notice_job(bot)


# reminder_log kind for one morning brief sent; the ref is the day. This is
# what lets a restart spanning 09:00 know the brief never went out.
BRIEF_KIND = "brief"


async def brief_job(bot: Bot) -> bool:
    """The morning brief (cron at MORNING_BRIEF_TIME). Never skipped.

    Deterministic — SQL and the open-loops engine, no model call — so it
    arrives even with Anthropic down. Quiet hours are not consulted: 09:00
    is outside them by the owner's own choice, and a brief he asked for at a
    time he chose is not a notification to be suppressed. Returns whether it
    reached him; a sent brief is logged so the startup catch-up can tell a
    missed one from a delivered one.
    """
    async with session_scope() as session:
        data = await brief.gather(session)
        body = replies.morning_brief(data)
        due, stale = replies.morning_brief_refs(data)
    sent = await notify(bot, body, reply_markup=keyboards.brief_actions(due, stale))
    if sent:
        async with session_scope() as session:
            session.add(ReminderLog(kind=BRIEF_KIND, ref=data.day.isoformat()))
    return sent


async def _brief_is_missing(now: datetime) -> bool:
    """True when today's brief is due, still worth sending, and never went out.

    Due: MORNING_BRIEF_TIME has passed. Worth sending: outside quiet hours —
    the first start after a deploy, or a reboot at 23:45, must not put
    "Ertalabki xulosa" on his phone at night — and before REPORT_TIME, since
    a morning brief after the evening report is stale. Never went out: no
    brief logged for today, the case of a deploy or a reboot spanning 09:00.
    """
    due = datetime.combine(
        now.date(), settings.morning_brief_time_parsed, tzinfo=settings.tz
    )
    if now < due:
        return False
    if reminders.in_quiet_hours(now):
        return False
    report_due = datetime.combine(
        now.date(), settings.report_time_parsed, tzinfo=settings.tz
    )
    if now >= report_due:
        return False
    async with session_scope() as session:
        found = await session.scalar(
            sa.select(ReminderLog.id)
            .where(
                ReminderLog.kind == BRIEF_KIND, ReminderLog.ref == now.date().isoformat()
            )
            .limit(1)
        )
    return found is None


async def nudge_job(bot: Bot) -> None:
    """Nudge the owner about questions nobody answered (every 30 minutes).

    One short message per question, ✅ Javob berdim / ⏰ Ertaga under each,
    at most nudges.MAX_PER_SWEEP per sweep with one line for the rest —
    those come next sweep, nothing is dropped. Quiet-hours aware the way
    every other ping is: the sweep skips, the question keeps.
    """
    if reminders.in_quiet_hours():
        return

    async with session_scope() as session:
        due = await nudges.collect(session)
        if not due:
            return
        head, tail = due[: nudges.MAX_PER_SWEEP], due[nudges.MAX_PER_SWEEP :]
        sent: list = []
        for question in head:
            if not await notify(
                bot,
                replies.nudge(question),
                reply_markup=keyboards.nudge_actions(question.interaction_id),
            ):
                break
            sent.append(question)
        # Logged only for what went out, and committed at once: a crash
        # mid-sweep repeats at most one nudge and loses none.
        nudges.mark_nudged(session, sent)
        await session.commit()
        if tail and len(sent) == len(head):
            await notify(bot, replies.nudge_overflow(len(tail)))

    log.info("sent %d nudge(s), %d more waiting", len(sent), len(tail))


async def new_chat_ask_job(bot: Bot) -> None:
    """ "Yangi guruh: … — o'qiymi?" for every group or channel that started
    switched off, once each (the owner's decision: new groups on with one
    tap). Quiet-hours aware; a few per sweep so a first sync of fifty groups
    is not fifty messages at once.
    """
    if reminders.in_quiet_hours():
        return

    async with session_scope() as session:
        questions = []
        for monitor in await chats.awaiting_join_question(session):
            questions.append(
                (
                    monitor.id,
                    replies.new_group_question(
                        monitor.title, monitor.tg_chat_id, chat_type=monitor.chat_type
                    ),
                )
            )
            # Marked before the send, like the media question: asked twice
            # is worse than once lost, and /chats still lists it.
            chats.mark_asked(monitor)

    for monitor_id, body in questions:
        try:
            await bot.send_message(
                settings.owner_telegram_id,
                clip(body),
                reply_markup=keyboards.new_group_question(monitor_id),
            )
        except Exception:
            log.exception("could not ask about chat monitor %s", monitor_id)


async def media_ask_job(bot: Bot) -> None:
    """Ask the owner about attachments too big to fetch on spec (spec §6).

    Quiet-hours aware: a 300 MB video at 02:00 is not worth a notification,
    and the question keeps until morning — the file is not going anywhere.
    """
    async with session_scope() as session:
        expired = await approvals.expire_stale(session)
    if expired:
        log.info("expired %d unanswered media question(s)", expired)

    if reminders.in_quiet_hours():
        return

    async with session_scope() as session:
        pending = await approvals.awaiting_question(session)
        questions = []
        for interaction in pending:
            person = (
                await session.get(Person, interaction.person_id)
                if interaction.person_id
                else None
            )
            questions.append(
                (
                    interaction.id,
                    replies.media_question(
                        who=person.display_name if person else None,
                        media=dict(interaction.media or {}),
                        reason=approvals.reason_of(interaction),
                    ),
                )
            )
            # Marked before the send, not after: a question asked twice is
            # worse than one lost to a failed send, which the owner can see
            # is missing anyway.
            approvals.set_state(interaction, approvals.ASKED)

    for interaction_id, body in questions:
        try:
            await bot.send_message(
                settings.owner_telegram_id,
                clip(body),
                reply_markup=keyboards.media_approval(interaction_id),
            )
        except Exception:
            log.exception("could not ask about attachment %s", interaction_id)


async def batch_submit_job() -> None:
    async with session_scope() as session:
        await batch.submit_pending(session)


async def batch_poll_job(bot: Bot) -> None:
    async with session_scope() as session:
        outcome = await batch.collect_submitted(session)
    if outcome.applied or outcome.failed:
        log.info(
            "batch poll: %d applied (%d worth telling), %d retried, %d failed",
            outcome.applied,
            sum(1 for landed in outcome.windows if landed.notice),
            outcome.retried,
            outcome.failed,
        )
    # The receipts were parked with the rows; delivering them is a separate
    # step so quiet hours delay them without touching the extraction.
    await chat_notice_job(bot)


# Two jobs deliver receipts — window_job on its own tick and batch_poll_job —
# and their intervals are both anchored at scheduler start, so they coincide
# every fifteen minutes. APScheduler's max_instances is per job id, so nothing
# else stops the two from reading the queue together, before either has
# marked a receipt sent, and telling the owner the same thing twice.
_notice_lock = asyncio.Lock()


async def chat_notice_job(bot: Bot) -> None:
    """Tell the owner what his chats put on the ledger, one receipt per window.

    Quiet-hours aware the way reminders are, but nothing is re-derived: the
    receipts wait in the queue and go out on the first poll after quiet hours
    end. A worker restart in between changes nothing. The first MAX_DETAILED
    go out one by one; anything beyond that — a day's backlog after an outage
    — is folded into one summary rather than a hundred messages. One delivery
    at a time, whichever job asked for it.
    """
    async with _notice_lock:
        if reminders.in_quiet_hours():
            return

        async with session_scope() as session:
            queue = await batch.pending_notices(session)
            if not queue:
                return
            now = datetime.now(settings.tz)
            detailed = queue[: notices.MAX_DETAILED]
            rest = queue[notices.MAX_DETAILED :]

            for item in detailed:
                if not await notify(bot, item.text):
                    # Unreachable: everything left stays queued for the next poll.
                    return
                # Marked only after a successful send, and committed at once, so
                # a crash mid-sweep repeats at most one receipt and loses none.
                batch.mark_notified(item.interaction, now=now)
                await session.commit()

            if rest:
                summary = notices.overflow_summary([(q.chat, q.counts) for q in rest])
                if not await notify(bot, summary):
                    return
                for item in rest:
                    batch.mark_notified(item.interaction, now=now)

    log.info(
        "sent %d chat notice(s), %d more folded into a summary", len(detailed), len(rest)
    )


async def backup_job(bot: Bot) -> None:
    """Nightly encrypted pg_dump (spec §10). Silence means it worked."""
    result = await backup.create_backup()
    if result.ok or result.error == "no_recipient":
        return
    # A backup that stopped working is worth waking the owner for — it is the
    # only thing standing between a disk failure and losing everything.
    await notify(
        bot,
        "⚠️ <b>Zaxira nusxa muvaffaqiyatsiz</b>\n\n"
        f"<code>{escape(result.error or 'unknown')}</code>",
    )


def _last_scheduled(at: time, now: datetime) -> datetime:
    """The most recent moment `at` has already passed (today, else yesterday)."""
    todays = datetime.combine(now.date(), at, tzinfo=settings.tz)
    return todays if todays <= now else todays - timedelta(days=1)


async def _missed_report_day(now: datetime) -> date | None:
    """The day whose evening report never went out, if there is one.

    A report row alone does not prove the evening report ran: `/hisobot` at
    lunchtime writes one for the same date. The row must have been *created*
    after that day's REPORT_TIME to count.
    """
    due = _last_scheduled(settings.report_time_parsed, now)
    day = due.date()
    async with session_scope() as session:
        created_at = await session.scalar(
            sa.select(DailyReport.created_at).where(DailyReport.report_date == day)
        )
    if created_at is not None and created_at.astimezone(settings.tz) >= due:
        return None
    return day


async def _backup_is_missing(now: datetime) -> bool:
    """True when no backup exists from after the last scheduled backup time.

    An absolute "older than 25 hours" test misses the common case: the VPS is
    down at 03:30 and comes back at 04:00, when yesterday's backup is only
    24.5 hours old — so the run would be skipped and the gap would stretch to
    48 hours.
    """
    due = _last_scheduled(settings.backup_time_parsed, now)
    newest = max(
        (p.stat().st_mtime for p in backup.backup_dir().glob(f"*{backup.BACKUP_SUFFIX}")),
        default=0.0,
    )
    return newest < due.timestamp()


async def catch_up(bot: Bot) -> None:
    """Run anything the scheduler missed while the worker was down.

    The jobstore is in memory, so a deploy or a VPS reboot spanning 19:00
    silently loses that day's report, one spanning 09:00 loses the morning
    brief, and one spanning BACKUP_TIME loses a nightly backup. All three
    are cheap to detect on startup and worth recovering: the report and the
    brief are the owner's two rituals, and the backup is the only thing
    between a disk failure and losing everything.

    Every branch is guarded. This runs before the worker settles into its
    scheduling loop, and a startup path that can raise is a crash loop under
    `restart: unless-stopped`.
    """
    now = datetime.now(settings.tz)

    try:
        day = await _missed_report_day(now)
    except Exception:
        log.exception("catch-up could not check for a missed report")
        day = None
    if day is not None:
        # Uses the day, not "today": an outage from 18:00 to the next morning
        # loses *yesterday's* report, which is exactly the case worth saving.
        log.info("catch-up: the report for %s was missed — generating now", day)
        try:
            async with session_scope() as session:
                content = await reports.generate_report(session, day)
            await notify(bot, f"📊 <b>Kunlik hisobot</b> ({day})\n\n{content}")
        except Exception:
            log.exception("catch-up report failed")

    try:
        brief_missing = await _brief_is_missing(now)
    except Exception:
        log.exception("catch-up could not check for a missed brief")
        brief_missing = False
    if brief_missing:
        log.info("catch-up: today's morning brief was missed — sending now")
        try:
            await brief_job(bot)
        except Exception:
            log.exception("catch-up brief failed")

    if settings.backup_age_recipient:
        try:
            missing = await _backup_is_missing(now)
        except OSError:
            log.exception("catch-up could not read the backup directory")
            missing = False
        if missing:
            log.info("catch-up: no backup since the last scheduled time — running one")
            try:
                await backup_job(bot)
            except Exception:
                log.exception("catch-up backup failed")


async def run() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if not settings.assistant_bot_token or not settings.owner_telegram_id:
        raise SystemExit(
            "ASSISTANT_BOT_TOKEN and OWNER_TELEGRAM_ID must be set for the worker"
        )
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is empty — extraction and reports degrade")
    if not settings.elevenlabs_api_key:
        log.warning("ELEVENLABS_API_KEY is empty — call transcription will fail")

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
        misfire_grace_time=3600,
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
        misfire_grace_time=3600,
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
        misfire_grace_time=3600,
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
    # Userbot pipeline. These jobs are harmless with the userbot switched off:
    # with no messages there is nothing to window and nothing to submit.
    scheduler.add_job(
        window_job,
        IntervalTrigger(minutes=5),
        args=[bot],
        id="windows",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        media_ask_job,
        IntervalTrigger(minutes=2),
        id="media_ask",
        args=[bot],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        batch_submit_job,
        IntervalTrigger(hours=settings.batch_flush_hours),
        id="batch_submit",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        batch_poll_job,
        IntervalTrigger(minutes=15),
        args=[bot],
        id="batch_poll",
        max_instances=1,
        coalesce=True,
    )
    brief_at = settings.morning_brief_time_parsed
    scheduler.add_job(
        brief_job,
        CronTrigger(
            hour=brief_at.hour, minute=brief_at.minute, timezone=settings.timezone
        ),
        args=[bot],
        id="morning_brief",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        nudge_job,
        IntervalTrigger(minutes=30),
        args=[bot],
        id="nudges",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        new_chat_ask_job,
        IntervalTrigger(minutes=2),
        args=[bot],
        id="new_chat_ask",
        max_instances=1,
        coalesce=True,
    )
    backup_at = settings.backup_time_parsed
    scheduler.add_job(
        backup_job,
        CronTrigger(
            hour=backup_at.hour, minute=backup_at.minute, timezone=settings.timezone
        ),
        args=[bot],
        id="backup",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    log.info("worker started (tz=%s); jobs: %s", settings.timezone, scheduler.get_jobs())
    # Never fatal: the worker's whole job is to keep running.
    try:
        await catch_up(bot)
    except Exception:
        log.exception("startup catch-up failed; the scheduler carries on")

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
