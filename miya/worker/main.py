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
  * backup       — cron at BACKUP_TIME; encrypted pg_dump -Fc, 14-day
                   retention, and a copy to the owner's Telegram in ≤45 MB
                   pieces (build step 5)
  * morning_brief — cron at MORNING_BRIEF_TIME; today's meetings, what is due,
                   and every open loop, deterministic, never skipped
  * questions    — every 5 min; the one sender of tap-requests: counterparty
                   claims, money texts, missed calls, "Hali ochiqmi?",
                   unanswered questions, new groups and big files, ranked
                   money first and held to QUESTION_BUDGET_PER_DAY
                   (quiet-hours aware; WP-18)
  * money_notices — every minute; a receipt with ✏️ Tuzat / 🗑 O'chir per
                   payment the phone booked, folded for bursts, one summary
                   for a first import (quiet-hours aware; WP-15)
  * profile_refresh — every 30 min; rewrites the written profile of up to
                   PROFILE_REFRESH_PER_RUN people whose activity is newer
                   than their profile (sends nothing; build step 4)
  * heartbeat    — every minute; the worker's own liveness row (build step 5)
  * health       — every 5 min; one status, the problems it shows, one alert
                   per problem per ALERT_REPEAT_HOURS and one recovery notice
                   when it clears; critical ones ignore quiet hours

Every job's outcome is also recorded as a ``job:<id>`` heartbeat by an
APScheduler listener, so /holat can name the job that last failed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import signal
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from miya.bot import keyboards, notices, replies
from miya.bot.formatting import clip, clock, escape, short_date
from miya.config import settings
from miya.db.models import DailyReport, Person, ReminderLog
from miya.db.session import engine, session_scope
from miya.services import (
    backup,
    batch,
    brief,
    call_recordings,
    claims,
    gcal,
    health,
    memories,
    money_notices,
    profiles,
    questions,
    reminders,
    reports,
    windows,
)
from miya.services.embeddings import EmbeddingError, get_embedder

log = logging.getLogger(__name__)


async def deliver(
    bot: Bot, text: str, *, reply_markup=None, silent: bool = False
) -> tuple[bool, Message | None]:
    """Send to the owner; retry as plain text if Telegram rejects the HTML.

    Report and reminder bodies carry names and descriptions that a
    counterparty controls, and the report itself is composed by a model. A
    single stray tag must not silently cost the owner his evening summary.
    ``reply_markup`` (the reminder buttons) rides along on both attempts.
    Returns (delivered, the sent message) — the message id goes into
    question_log.
    """
    try:
        sent = await bot.send_message(
            settings.owner_telegram_id,
            clip(text),
            reply_markup=reply_markup,
            disable_notification=silent,
        )
        return True, sent
    except Exception:
        log.warning("HTML send failed, retrying as plain text", exc_info=True)
    try:
        sent = await bot.send_message(
            settings.owner_telegram_id,
            clip(text),
            parse_mode=None,
            reply_markup=reply_markup,
            disable_notification=silent,
        )
        return True, sent
    except Exception:
        log.exception("could not reach the owner at all")
        return False, None


async def notify_message(
    bot: Bot, text: str, *, reply_markup=None, silent: bool = False
) -> Message | None:
    ok, sent = await deliver(bot, text, reply_markup=reply_markup, silent=silent)
    return sent if ok else None


async def notify(bot: Bot, text: str, *, reply_markup=None, silent: bool = False) -> bool:
    ok, _ = await deliver(bot, text, reply_markup=reply_markup, silent=silent)
    return ok


async def reminder_job(bot: Bot) -> None:
    """Ping the owner about anything due. Silent during quiet hours.

    One message: the pings, with a ✅ / ✏️ / 🔄 row per line so an item can
    be closed or corrected from the reminder itself. "Hali ochiqmi?" is a
    tap-request and goes through the question queue (question_job).
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


async def report_job(bot: Bot, *, now: datetime | None = None) -> None:
    """Compose, store and deliver the daily report (cron at REPORT_TIME),
    then the evening's question batch (WP-19)."""
    now = now or datetime.now(settings.tz)
    day = now.astimezone(settings.tz).date()
    async with session_scope() as session:
        content = await reports.generate_report(session, day)
    # The report is committed before the send: a Telegram failure costs the
    # notification, never the report itself (`/hisobot` re-reads it).
    if await notify(bot, f"{reports.report_header(day)}\n\n{content}"):
        await _slot_questions(
            bot,
            slot=questions.SLOT_EVENING,
            via=questions.VIA_EVENING,
            header=replies.QUESTIONS_EVENING_HEADER,
            now=now,
        )


async def _slot_questions(
    bot: Bot, *, slot: str, via: str, header: str, now: datetime
) -> int:
    """The batch after the brief or the report. A failure here never costs
    the message it follows."""
    try:
        async with session_scope() as session:
            await questions.auto_resolve(session, now=now)
            await session.commit()
            pushable = await questions.collect(session, now=now, for_push=True)
            picked = questions.plan(
                pushable,
                spent=await questions.spent_today(session, now=now),
                slot=slot,
                now=now,
                last_push=None,
                brief_sent=True,
                interrupting=False,
            )
            if not picked:
                return 0
            return await send_questions(
                bot, session, picked, via=via, header=header, now=now
            )
    except Exception:
        log.exception("the %s question batch failed", slot)
        return 0


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
BRIEF_KIND = brief.BRIEF_KIND


async def brief_job(bot: Bot, *, now: datetime | None = None) -> bool:
    """The morning brief (cron at MORNING_BRIEF_TIME). Never skipped.

    Deterministic — SQL and the open-loops engine, no model call — so it
    arrives even with Anthropic down. Quiet hours are not consulted: 09:00
    is outside them by the owner's own choice, and a brief he asked for at a
    time he chose is not a notification to be suppressed. Returns whether it
    reached him; a sent brief is logged so the startup catch-up can tell a
    missed one from a delivered one.

    The brief tells and carries only the due rows' buttons; right after it,
    one numbered batch asks the QUESTION_BRIEF_SLOTS most important
    questions (WP-19), and one line counts what else waits in /savollar.
    """
    now = now or datetime.now(settings.tz)
    async with session_scope() as session:
        data = await brief.gather(session, now=now)
        await questions.auto_resolve(session, now=now)
        await session.commit()
        pushable = await questions.collect(session, now=now, for_push=True)
        picked = questions.plan(
            pushable,
            spent=await questions.spent_today(session, now=now),
            slot=questions.SLOT_BRIEF,
            now=now,
            last_push=None,
            brief_sent=True,
            interrupting=False,
        )
        data.queue = questions.summarise(
            await questions.collect(session, now=now, for_push=False), picked
        )
        body = replies.morning_brief(data)
        due, _ = replies.morning_brief_refs(data)
        keyboard = keyboards.brief_actions(due)
    sent = await notify(bot, body, reply_markup=keyboard)
    if sent:
        async with session_scope() as session:
            session.add(ReminderLog(kind=BRIEF_KIND, ref=data.day.isoformat()))
        if picked:
            await _slot_questions(
                bot,
                slot=questions.SLOT_BRIEF,
                via=questions.VIA_BRIEF,
                header=replies.QUESTIONS_BRIEF_HEADER,
                now=now,
            )
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


async def send_questions(
    bot: Bot,
    session,
    items: list[questions.Pending],
    *,
    via: str,
    header: str,
    now: datetime,
) -> int:
    """Send planned questions and record what the owner really saw (WP-18).

    The groups digest goes as its own message; everything else as one
    numbered batch. Nothing is recorded for a message that did not go out:
    the items qualify again on the next tick. Returns the tap-requests
    recorded.
    """
    spent = await questions.spent_today(session, now=now)
    sent = 0
    rest = [p for p in items if p.kind != questions.KIND_GROUPS]
    for item in items:
        if item.kind != questions.KIND_GROUPS:
            continue
        body = replies.group_digest(item.subject)
        ok, msg = await deliver(
            bot, body, reply_markup=keyboards.group_digest(item.subject)
        )
        if not ok:
            return sent
        await questions.record_shown(
            session,
            [item],
            via=via,
            now=now,
            tg_message_id=getattr(msg, "message_id", None),
        )
        await session.commit()
        sent += len(item.subject)
    if not rest:
        return sent
    people = await _people_of(session, rest)
    body, shown = replies.question_batch(
        rest,
        header=header,
        used=spent + sent + len(rest),
        budget=settings.question_budget_per_day,
        people=people,
    )
    if shown == 0:
        return sent
    ok, msg = await deliver(
        bot, body, reply_markup=keyboards.question_batch(rest[:shown])
    )
    if not ok:
        return sent
    await questions.record_shown(
        session,
        rest[:shown],
        via=via,
        now=now,
        tg_message_id=getattr(msg, "message_id", None),
    )
    await session.commit()
    return sent + shown


async def _people_of(session, items) -> dict[int, Person]:
    ids = {
        p.subject.person_id
        for p in items
        if p.kind == questions.KIND_MEDIA and p.subject.person_id is not None
    }
    if not ids:
        return {}
    rows = await session.scalars(sa.select(Person).where(Person.id.in_(ids)))
    return {person.id: person for person in rows}


async def question_job(bot: Bot, *, now: datetime | None = None) -> None:
    """The one sender of tap-requests (WP-18; owner answer 3: 5-10 a day).

    Every five minutes: settle what needs no tap, collect everything that
    wants one, and let questions.plan decide what fits today's budget, the
    evening reserve and the gap between pushes. Urgent money comes first;
    the rest waits for the brief, the evening report and /savollar, where
    nothing is ever lost.
    """
    now = now or datetime.now(settings.tz)
    if reminders.in_quiet_hours(now):
        return
    async with session_scope() as session:
        resolved = await questions.auto_resolve(session, now=now)
        await session.commit()
        if any(resolved.values()):
            log.info("questions resolved without asking: %s", resolved)
        pending = await questions.collect(session, now=now, for_push=True)
        if not pending:
            return
        picked = questions.plan(
            pending,
            spent=await questions.spent_today(session, now=now),
            slot=questions.SLOT_DAY,
            now=now,
            last_push=await questions.last_push_at(session, now=now),
            brief_sent=await questions.brief_sent_today(session, now=now),
        )
        if not picked:
            return
        shown = await send_questions(
            bot,
            session,
            picked,
            via=questions.VIA_PUSH,
            header=replies.QUESTIONS_HEADER,
            now=now,
        )
    log.info("asked %d question(s), %d waiting", shown, len(pending) - len(picked))


async def _prune_job_heartbeats(session, registered: list[str]) -> int:
    """Drop heartbeat rows of jobs this worker no longer runs, so /holat
    never names a retired job (WP-16 deletes them once; an old worker may
    have re-written them during the migration)."""
    result = await session.execute(
        sa.text(
            "DELETE FROM heartbeats WHERE component LIKE 'job:%' "
            "AND component <> ALL(:registered)"
        ),
        {"registered": registered},
    )
    return result.rowcount or 0


# Profiles per sweep: one reasoning-model call each, so a first run over a
# large contact list spreads across a few hours instead of one burst; a
# person whose activity keeps changing is simply picked up again next sweep.
PROFILE_REFRESH_PER_RUN = 10


async def profile_refresh_job() -> None:
    """Rewrite stale person profiles (build step 4), a few per sweep.

    No quiet-hours guard: nothing is sent, the text only waits in
    ``people.notes`` for the next /kim or "Akmal kim?". ``refresh_stale``
    commits per person and never raises on a model failure, so a sweep
    that loses the API keeps every profile it already wrote.
    """
    async with session_scope() as session:
        written = await profiles.refresh_stale(session, limit=PROFILE_REFRESH_PER_RUN)
    if written:
        log.info("refreshed %d person profile(s)", written)


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

    A detailed receipt also carries the questions its window raised: a ✅ /
    ✖️ / ✏️ row per counterparty claim (build step 3), so "Akmal aytdi: sen
    unga qarzsan — to'g'rimi?" is answered from the receipt itself. A claim
    counts as asked only once the receipt really went out, and only as many
    rows as today's question budget allows (WP-18); the summary path asks
    nothing, and whatever is left unasked reaches him through question_job,
    the brief and /savollar.
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
                # Only claims nobody has asked yet ride on the receipt: the
                # question queue may have beaten it after quiet hours, and
                # the owner already holds that row.
                waiting = [
                    claim
                    for claim in await claims.pending_for(session, item.interaction.id)
                    if claim.asked_at is None and claim.duplicate_of is None
                ]
                # The receipt is not an interruption of its own, but each
                # claim row on it is a tap-request: the day's budget decides
                # how many ride along (WP-18); the rest wait in the queue.
                allowed = questions.plan(
                    [questions.pending_of_claim(c, now) for c in waiting],
                    spent=await questions.spent_today(session, now=now),
                    slot=questions.SLOT_DAY,
                    now=now,
                    last_push=None,
                    brief_sent=True,
                    interrupting=False,
                )[: keyboards.MAX_ROWS]
                keyboard = keyboards.claim_actions([p.subject.id for p in allowed])
                text = item.text
                if len(allowed) < len(waiting):
                    text += "\n" + replies.CLAIMS_QUEUED
                ok, msg = await deliver(bot, text, reply_markup=keyboard)
                if not ok:
                    # Unreachable: everything left stays queued for the next poll.
                    return
                # Marked only after a successful send, and committed at once, so
                # a crash mid-sweep repeats at most one receipt and loses none.
                # The claims whose buttons he now sees are logged with it.
                batch.mark_notified(item.interaction, now=now)
                await questions.record_shown(
                    session,
                    allowed,
                    via=questions.VIA_RECEIPT,
                    now=now,
                    tg_message_id=getattr(msg, "message_id", None),
                )
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


async def money_notice_job(bot: Bot, *, now: datetime | None = None) -> None:
    """A receipt for each payment the phone booked, within the minute (WP-15).

    Fewer than MONEY_RECEIPTS_FOLD_AT go out one by one with ✏️ Tuzat and
    🗑 O'chir; a burst is folded into one message; everything older than
    MONEY_RECEIPT_MAX_AGE_HOURS becomes one import summary. In quiet hours
    the receipts wait, or arrive silently when the owner chose that. A row
    is stamped only after its message went out, one commit per message.
    """
    now = now or datetime.now(settings.tz)
    silent = False
    if reminders.in_quiet_hours(now):
        if not settings.money_receipts_silent_at_night:
            return
        silent = True
    async with _notice_lock, session_scope() as session:
        queue = await money_notices.pending(session)
        await session.commit()  # stale notices stamped by pending()

        fresh = queue.fresh
        if fresh and len(fresh) < settings.money_receipts_fold_at:
            for interaction, txn in fresh:
                text = replies.money_receipt(txn, interaction, txn.counterparty)
                keyboard = keyboards.record_actions([("transaction", txn.id)])
                if not await notify(bot, text, reply_markup=keyboard, silent=silent):
                    return
                money_notices.mark_notified(interaction, now=now)
                await session.commit()
        elif fresh:
            text = replies.money_receipts_folded(fresh)
            keyboard = keyboards.record_actions(
                [("transaction", txn.id) for _, txn in fresh[:8]]
            )
            if not await notify(bot, text, reply_markup=keyboard, silent=silent):
                return
            for interaction, _ in fresh:
                money_notices.mark_notified(interaction, now=now)
            await session.commit()

        if queue.backfill:
            counts = {"book": 0, "review": 0, "ignore": 0}
            for _, verdict, _ in queue.backfill:
                if verdict in counts:
                    counts[verdict] += 1
            text = replies.money_import_summary(
                counts["book"], counts["review"], counts["ignore"]
            )
            if not await notify(bot, text, silent=silent):
                return
            for interaction, _, _ in queue.backfill:
                money_notices.mark_notified(interaction, now=now)
            await session.commit()


async def backup_job(bot: Bot) -> None:
    """Nightly encrypted pg_dump (spec §10), then a copy to Telegram.

    Silence means the dump worked. The file itself is the durable copy; the
    Telegram copy (BACKUP_TO_TELEGRAM, the owner's decision in build step 5)
    is what survives the VPS disk, and its outcome is recorded as the
    ``backup`` heartbeat so /holat and the health job can say whether the
    newest file ever left the server.
    """
    result = await backup.create_backup()
    if result.error == "no_recipient":
        # The health job tells the owner (backup_unconfigured); the log says
        # why tonight's run did nothing.
        log.warning("backup skipped: BACKUP_AGE_RECIPIENT is empty")
        return
    # Pieces left by an upload the container died in the middle of: nothing
    # prunes them otherwise, and they are only ever a copy of the file.
    stale_parts = sorted(backup.backup_dir().glob(f"*{backup.BACKUP_SUFFIX}.part[0-9]*"))
    if stale_parts:
        backup.remove_parts(stale_parts)
    if not result.ok or result.path is None:
        # A backup that stopped working is worth waking the owner for — it is
        # the only thing standing between a disk failure and losing everything.
        await notify(
            bot,
            "⚠️ <b>Zaxira nusxa muvaffaqiyatsiz</b>\n\n"
            f"<code>{escape(result.error or 'unknown')}</code>",
        )
        return
    if settings.backup_to_telegram:
        await send_backup_to_telegram(bot, result.path, result.size)


def _backup_caption(path: Path, size: int, index: int, total: int) -> str:
    stamp = backup.backup_stamp(path) or datetime.now(settings.tz)
    when = f"{short_date(stamp.date())} {clock(stamp)}"
    return escape(f"🗄 Zaxira nusxa {when} · {health.size_label(size)} · {index}/{total}")


async def _record_backup_delivery(
    path: Path, size: int, *, sent: bool, now: datetime, error: str | None = None
) -> None:
    """The ``backup`` heartbeat: did *this* file reach Telegram. Never raises —
    a database hiccup here must not turn a delivered backup into a warning."""
    detail: dict = {"sent": sent, "path": str(path), "size": size}
    if sent:
        detail["sent_at"] = now.isoformat()
    else:
        detail["error"] = (error or "unknown")[:200]
    try:
        async with session_scope() as session:
            await health.beat(session, health.BACKUP_COMPONENT, detail=detail, now=now)
    except Exception:
        log.exception("could not record the backup delivery heartbeat")


async def send_backup_to_telegram(bot: Bot, path: Path, size: int) -> bool:
    """Send one backup to the owner as documents, in pieces when it is big.

    Telegram caps a bot document at 50 MB, so ``backup.split_for_telegram``
    cuts anything larger into ``.partNN`` files next to the backup; every
    piece goes out in order with its ``N/M`` caption and the pieces are
    removed again whatever happens — the backup itself is never touched.
    A failure (Telegram down, a file past ``backup.MAX_PARTS`` pieces) is
    told to the owner as a warning that names the on-disk path: the backup is
    fine, only the off-site copy is missing. The warning is logged under the
    ``backup_failed`` alert key, so the health job does not repeat it at
    once, and it waits for quiet hours to end like every other warning — the
    ``backup`` heartbeat carries it to the health job in the morning.
    Returns whether every piece was delivered.
    """
    now = datetime.now(settings.tz)
    parts: list[Path] = []
    try:
        parts = await asyncio.to_thread(backup.split_for_telegram, path)
        total = len(parts)
        for index, part in enumerate(parts, 1):
            await bot.send_document(
                settings.owner_telegram_id,
                FSInputFile(part),
                caption=_backup_caption(path, size, index, total),
                disable_notification=True,
            )
    except Exception as exc:
        log.exception("backup %s could not be sent to Telegram", path.name)
        await _record_backup_delivery(path, size, sent=False, now=now, error=str(exc))
        if not reminders.in_quiet_hours():
            told = await notify(
                bot,
                "⚠️ <b>Zaxira nusxa Telegramga yuborilmadi</b>\n\n"
                f"Fayl diskda turibdi: <code>{escape(str(path))}</code>\n"
                f"Sabab: <code>{escape(str(exc)[:200] or type(exc).__name__)}</code>\n"
                "Keyingi kecha qayta uriniladi; hozir kerak bo'lsa "
                "<code>make backup</code>.",
            )
            if told:
                await _mark_alert_delivered("backup_failed", now=now)
        return False
    finally:
        backup.remove_parts(parts)

    await _record_backup_delivery(path, size, sent=True, now=now)
    log.info("backup %s sent to Telegram in %d piece(s)", path.name, len(parts))
    return True


async def _mark_alert_delivered(key: str, *, now: datetime) -> None:
    """Ledger entry for an alert this job sent itself, so the health job's
    dedupe covers it. Best effort: the message already went out."""
    try:
        async with session_scope() as session:
            health.mark_alerted(session, [key], now=now)
    except Exception:
        log.exception("could not record the %s alert", key)


# --- self-monitoring (build step 5) -----------------------------------------


async def record_job_event(job_id: str, *, ok: bool, error: str | None) -> None:
    """Upsert the ``job:<id>`` heartbeat for one finished scheduler run.

    Its own session, and it never raises: a heartbeat that cannot be written
    is a log line, not a second failure on top of the job's own.
    """
    try:
        async with session_scope() as session:
            await health.beat(
                session,
                health.JOB_PREFIX + job_id,
                detail={"ok": ok, "error": error[:200] if error else None},
            )
    except Exception:
        log.exception("could not record the outcome of job %s", job_id)


def _on_job_event(event, *, loop: asyncio.AbstractEventLoop):
    """APScheduler listener (EVENT_JOB_EXECUTED | EVENT_JOB_ERROR).

    Listeners are plain callbacks, so the upsert is handed to the loop as a
    task; the returned future is for tests, APScheduler ignores it.
    """
    exc = getattr(event, "exception", None)
    error = None if exc is None else f"{type(exc).__name__}: {exc}"
    return asyncio.run_coroutine_threadsafe(
        record_job_event(event.job_id, ok=exc is None, error=error), loop
    )


async def heartbeat_job(scheduler: AsyncIOScheduler | None = None) -> None:
    """The worker's own liveness row, once a minute."""
    jobs = len(scheduler.get_jobs()) if scheduler is not None else 0
    async with session_scope() as session:
        await health.beat(session, "worker", detail={"jobs": jobs})


# When the database is down the alert ledger is down with it; this memo is
# what keeps the worker from repeating "Baza javob bermayapti" every five
# minutes until it comes back. Per process, like the bot's.
_offline_alerted_at: dict[str, datetime] = {}


async def health_job(bot: Bot) -> None:
    """Judge the system and tell the owner what needs a hand (every 5 min).

    One status, the problems it shows, then the ledger decides: a problem is
    said once per ALERT_REPEAT_HOURS while it lasts, and once more as
    "tiklandi" when it clears. Critical problems (the database, the disk)
    ignore quiet hours; warnings and recoveries wait for morning — the ledger
    still shows them due then, nothing is lost. Only a delivered message is
    recorded, so an unreachable Telegram means the alert is simply tried
    again next tick.
    """
    now = datetime.now(settings.tz)
    status: health.Status | None = None
    try:
        async with session_scope() as session:
            status = await health.gather(session, now=now)
            if status.db_ok:
                await _alert_from_ledger(session, bot, health.problems(status), now=now)
                return
    except Exception:
        # Leaving the scope commits; with the database gone that can fail
        # too. A failure with the database *up* is a real one and stays loud.
        if status is None or status.db_ok:
            raise
        log.warning("health sweep: the database is unreachable", exc_info=True)
    await _alert_without_ledger(bot, health.problems(status), now=now)


async def _alert_from_ledger(
    session, bot: Bot, found: list[health.Problem], *, now: datetime
) -> None:
    quiet = reminders.in_quiet_hours(now)
    due, recovered = await health.alerts_due(session, found, now=now)
    for problem in due:
        if problem.severity != "critical" and quiet:
            continue
        if await notify(bot, problem.text):
            health.mark_alerted(session, [problem.key], now=now)
            # Committed one by one: a crash mid-sweep repeats at most one.
            await session.commit()
    if quiet:
        return
    for key in recovered:
        # Includes worker_silent, raised by the bot's watchdog: this job
        # running again is the proof, so the worker alone announces it.
        if await notify(bot, health.recovery_text(key)):
            health.mark_recovered(session, [key], now=now)
            await session.commit()


async def _alert_without_ledger(
    bot: Bot, found: list[health.Problem], *, now: datetime
) -> None:
    """The critical problems only, deduped in memory: with the database gone
    there is nothing else to judge and nowhere to write."""
    repeat = timedelta(hours=settings.alert_repeat_hours)
    for problem in found:
        if problem.severity != "critical":
            continue
        last = _offline_alerted_at.get(problem.key)
        if last is not None and now - last <= repeat:
            continue
        if await notify(bot, problem.text):
            _offline_alerted_at[problem.key] = now


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
            if await notify(bot, f"{reports.report_header(day)}\n\n{content}"):
                await _slot_questions(
                    bot,
                    slot=questions.SLOT_EVENING,
                    via=questions.VIA_EVENING,
                    header=replies.QUESTIONS_EVENING_HEADER,
                    now=datetime.now(settings.tz),
                )
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
    # The worker embeds through the api with this token.
    if problem := settings.api_token_problem():
        raise SystemExit(problem)
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
        question_job,
        IntervalTrigger(minutes=5),
        args=[bot],
        id="questions",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        money_notice_job,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="money_notices",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        profile_refresh_job,
        IntervalTrigger(minutes=30),
        id="profile_refresh",
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
    scheduler.add_job(
        heartbeat_job,
        IntervalTrigger(minutes=1),
        args=[scheduler],
        id="heartbeat",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        health_job,
        IntervalTrigger(minutes=5),
        args=[bot],
        id="health",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_listener(
        functools.partial(_on_job_event, loop=asyncio.get_running_loop()),
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )
    scheduler.start()
    log.info("worker started (tz=%s); jobs: %s", settings.timezone, scheduler.get_jobs())
    try:
        async with session_scope() as session:
            await _prune_job_heartbeats(
                session, [f"{health.JOB_PREFIX}{job.id}" for job in scheduler.get_jobs()]
            )
    except Exception:
        log.warning("could not prune stale job heartbeats", exc_info=True)
    # The first beat goes out before the catch-up, which can take minutes
    # (a report, a backup): the bot's watchdog must not read that as silence.
    try:
        await heartbeat_job(scheduler)
    except Exception:
        log.exception("startup heartbeat failed; the scheduler carries on")
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
