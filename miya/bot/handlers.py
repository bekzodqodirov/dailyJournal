"""Assistant bot handlers (spec §7A). Owner-only — every other user is ignored.

Ordering rule for every content handler: **commit first, reply second.** The
reply goes out only after `session_scope` has committed, and it is best-effort
— a Telegram failure (network, flood limit) must never roll back ingested data
or, worse, lose the owner's message entirely. Pre-ingest Telegram calls
(`send_chat_action`, downloads) are equally best-effort or guarded so an API
hiccup cannot kill the handler before anything reached the database.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import sqlalchemy as sa
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from miya.bot import keyboards, replies
from miya.bot.formatting import clip, escape, ref_of
from miya.bot.keyboards import FIELD_CODES, PAGE_SIZE, ChatsPage, chats_keyboard
from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import ChatMonitor, Interaction, Person
from miya.db.session import session_scope
from miya.services import (
    approvals,
    audio,
    brief,
    chats,
    documents,
    memories,
    nudges,
    planner,
    purge,
    queries,
    rag,
    records,
    reminders,
    reports,
)
from miya.services.embeddings import EmbeddingError, get_embedder
from miya.services.ingest import (
    create_interaction,
    describe_into,
    process_interaction,
    transcribe_into,
)
from miya.services.people import best_match

log = logging.getLogger(__name__)


def is_owner(user_id: int | None) -> bool:
    """Fail closed: with OWNER_TELEGRAM_ID unset, nobody is the owner."""
    owner = settings.owner_telegram_id
    return owner is not None and user_id == owner


router = Router(name="assistant")
router.message.filter(F.from_user.id.func(is_owner))
# Buttons are as privileged as commands — `/chats` toggles what gets read.
router.callback_query.filter(F.from_user.id.func(is_owner))


def _media_path(suffix: str) -> Path:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    return settings.media_dir / f"{stamp}-{uuid.uuid4().hex[:8]}{suffix}"


async def _safe_answer(message: Message, text: str | None, *, reply_markup=None) -> None:
    """Reply after the data is durable; a failed send only costs the receipt."""
    if not text:
        return
    try:
        await message.answer(text, reply_markup=reply_markup)
        return
    except Exception:
        log.exception("could not send reply to owner (data is committed)")
    # Model-composed replies can contain broken HTML; a plain-text retry beats
    # the owner never seeing the answer at all.
    try:
        await message.answer(text, parse_mode=None, reply_markup=reply_markup)
    except Exception:
        log.exception("plain-text retry failed too")


def _receipt(result) -> tuple[str, InlineKeyboardMarkup | None]:
    """The confirmation text plus its ✅ / ✏️ / 🔄 rows, one per recorded row."""
    if not result.ok:
        return replies.FAILED_EXTRACTION_HINT, None
    return (
        replies.confirmation(result.applied),
        keyboards.record_actions(replies.confirmation_refs(result.applied)),
    )


async def _typing(message: Message) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        log.debug("send_chat_action failed", exc_info=True)


# Telegram's Bot API refuses to serve files above 20 MB — retrying can never
# succeed, so the owner deserves an honest message instead of "try again".
TG_BOT_MAX_DOWNLOAD = 20 * 1024 * 1024


async def _download(bot: Bot, message: Message, media, path: Path) -> bool:
    """Fetch a file from Telegram; on failure tell the owner instead of dying."""
    size = getattr(media, "file_size", None)
    if size and size > TG_BOT_MAX_DOWNLOAD:
        await _safe_answer(message, replies.FILE_TOO_BIG_HINT)
        return False
    try:
        await bot.download(media, destination=path)
        return True
    except Exception:
        log.exception("could not download media from Telegram")
        await _safe_answer(
            message, "⚠️ Faylni yuklab bo'lmadi — qaytadan yuborib ko'ring."
        )
        return False


# --- commands ---------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await _safe_answer(message, replies.HELP)


@router.message(Command("yordam", "help"))
async def cmd_help(message: Message) -> None:
    await _safe_answer(message, replies.HELP)


@router.message(Command("qarz"))
async def cmd_debts(message: Message) -> None:
    async with session_scope() as session:
        balances = await queries.open_debts(session)
    await _safe_answer(message, replies.debts_report(balances))


@router.message(Command("vada", "va_da"))
async def cmd_promises(message: Message) -> None:
    async with session_scope() as session:
        items = await queries.open_promises(session)
    await _safe_answer(message, replies.promises_report(items))


@router.message(Command("bugun"))
async def cmd_today(message: Message) -> None:
    async with session_scope() as session:
        summary = await queries.day_summary(session)
    await _safe_answer(message, replies.day_report(summary))


@router.message(Command("menga"))
async def cmd_to_me(message: Message) -> None:
    """Group traffic aimed at the owner, separated from the room's noise."""
    async with session_scope() as session:
        addressed = await queries.messages_to_me(session)
        titles = dict(
            (
                await session.execute(
                    sa.select(ChatMonitor.tg_chat_id, ChatMonitor.title)
                )
            ).all()
        )
        body = replies.to_me_report(addressed, titles)
    await _safe_answer(message, body)


@router.message(Command("guruhlar"))
async def cmd_chat_digests(message: Message) -> None:
    """What each monitored chat was actually about today."""
    async with session_scope() as session:
        body = replies.chat_digest_report(await queries.chat_digests(session))
    await _safe_answer(message, body)


@router.message(Command("tekshir"))
async def cmd_review(message: Message) -> None:
    async with session_scope() as session:
        flagged, total = await queries.flagged_interactions(session)
        body = replies.review_report(flagged, total)
    await _safe_answer(message, body)


@router.message(Command("qayta"))
async def cmd_retry(message: Message) -> None:
    """Re-extract everything `/tekshir` is holding (spec §14).

    Extraction fails for reasons that later stop being true — an outage, a
    rate limit, a bug in the pipeline. Without this the raw text is kept
    faithfully and then never becomes a debt or a promise, which is only half
    of not losing it.

    Each interaction gets its own transaction: one that fails again must not
    roll back the ones already rescued alongside it.
    """
    await _typing(message)
    async with session_scope() as session:
        pending = await queries.retryable_interactions(session)
        ids = [row.id for row in pending]

    if not ids:
        await _safe_answer(message, replies.RETRY_NOTHING_TO_DO)
        return

    rescued = failed = 0
    for interaction_id in ids:
        try:
            async with session_scope() as session:
                interaction = await session.get(Interaction, interaction_id)
                if interaction is None:
                    continue
                result = await process_interaction(session, interaction)
                if result.ok:
                    interaction.needs_review = False
                    rescued += 1
                else:
                    failed += 1
        except Exception:
            log.exception("retrying interaction %s failed", interaction_id)
            failed += 1

    await _safe_answer(message, replies.retry_report(rescued, failed))


@router.message(Command("qidir"))
async def cmd_search(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await _safe_answer(message, "Nima qidiray? <code>/qidir bojxona</code>")
        return

    await _typing(message)
    try:
        async with session_scope() as session:
            hits = await memories.search(session, get_embedder(), query, k=8)
        body = replies.search_results(hits, query)
    except EmbeddingError:
        log.warning("semantic search unavailable", exc_info=True)
        body = replies.SEARCH_UNAVAILABLE
    await _safe_answer(message, body)


@router.message(Command("hisobot"))
async def cmd_report(message: Message) -> None:
    await _typing(message)
    async with session_scope() as session:
        content = await reports.generate_report(session)
    await _safe_answer(message, clip(f"📊 <b>Kunlik hisobot</b>\n\n{content}"))


@router.message(Command("ertalab"))
async def cmd_brief(message: Message) -> None:
    """The morning brief on demand — the same message the worker sends at
    MORNING_BRIEF_TIME, with the same buttons. No model call: it is SQL."""
    async with session_scope() as session:
        data = await brief.gather(session)
        body = replies.morning_brief(data)
        due, stale = replies.morning_brief_refs(data)
    await _safe_answer(message, body, reply_markup=keyboards.brief_actions(due, stale))


@router.message(Command("reja"))
async def cmd_plan(message: Message) -> None:
    await _typing(message)
    async with session_scope() as session:
        content = await planner.plan_tomorrow(session)
    await _safe_answer(message, clip(f"📅 <b>Ertangi reja</b>\n\n{content}"))


@router.message(Command("chats"))
async def cmd_chats(message: Message) -> None:
    async with session_scope() as session:
        monitors, total = await chats.list_monitors(session, offset=0, limit=PAGE_SIZE)
    if not monitors:
        await _safe_answer(message, replies.CHATS_EMPTY)
        return
    page = ChatsPage(monitors=monitors, page=0, total=total)
    try:
        await message.answer(replies.CHATS_HEADER, reply_markup=chats_keyboard(page))
    except Exception:
        log.exception("could not send the chat list")


@router.callback_query(F.data.startswith("ch:"))
async def on_chats_button(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    page_index = 0
    notice = None

    if action == "t" and len(parts) == 4:
        field = FIELD_CODES.get(parts[3])
        if field is None:
            await callback.answer()
            return
        async with session_scope() as session:
            monitor = await chats.toggle(session, int(parts[2]), field)
            enabled = bool(monitor and getattr(monitor, field))
            notice = "✅ yoqildi" if enabled else "❌ o'chirildi"
        page_index = _page_of(callback)
    elif action == "p" and len(parts) == 3:
        page_index = int(parts[2])
    else:  # "noop" — the page counter in the middle of the nav row
        await callback.answer()
        return

    async with session_scope() as session:
        monitors, total = await chats.list_monitors(
            session, offset=page_index * PAGE_SIZE, limit=PAGE_SIZE
        )
    page = ChatsPage(monitors=monitors, page=page_index, total=total)
    try:
        await callback.message.edit_text(
            replies.CHATS_HEADER, reply_markup=chats_keyboard(page)
        )
    except Exception:
        # Telegram rejects an edit that changes nothing; the toggle still
        # landed, so this is a cosmetic failure only.
        log.debug("chat list edit failed", exc_info=True)
    await callback.answer(notice or "")


def _page_of(callback: CallbackQuery) -> int:
    """Which page the pressed keyboard was showing, so a toggle stays put."""
    markup = callback.message.reply_markup if callback.message else None
    for row in markup.inline_keyboard if markup else []:
        for button in row:
            data = button.callback_data or ""
            if data == "ch:noop":
                current, _, _ = button.text.partition("/")
                return max(0, int(current) - 1) if current.isdigit() else 0
    return 0


@router.message(Command("xarajat"))
async def cmd_usage(message: Message, command: CommandObject) -> None:
    """`/xarajat` — this month by default, or `/xarajat 2026-07`."""
    today = purge.today()
    period = (command.args or "").strip()
    if period:
        try:
            year, month = (int(p) for p in period.split("-")[:2])
            first = date(year, month, 1)
        except (ValueError, TypeError):
            await _safe_answer(message, "Sana formati: <code>/xarajat 2026-07</code>")
            return
    else:
        first = today.replace(day=1)

    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    async with session_scope() as session:
        summary = await queries.usage_summary(session, first, min(last, today))
    await _safe_answer(message, replies.usage_report(summary))


@router.message(Command("unut"))
async def cmd_purge(message: Message, command: CommandObject) -> None:
    """`/unut` — destructive, so it only ever shows a plan and asks first."""
    argument = (command.args or "").strip()
    if not argument:
        await _safe_answer(message, replies.PURGE_USAGE)
        return

    async with session_scope() as session:
        plan, payload = await _build_purge_plan(session, argument)
        body = replies.purge_preview(plan) if plan and not plan.is_empty() else None

    if body is None:
        await _safe_answer(message, replies.PURGE_NOTHING)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Ha, o'chir", callback_data=payload),
                InlineKeyboardButton(text="Bekor", callback_data="unut:no"),
            ]
        ]
    )
    try:
        await message.answer(body, reply_markup=keyboard)
    except Exception:
        log.exception("could not send the purge confirmation")


async def _build_purge_plan(session, argument: str):
    """Parse `/unut` arguments into a plan plus its confirm-callback payload.

    The payload re-derives the plan on confirmation instead of caching it, so a
    bot restart between the preview and the button press can never execute a
    stale, wider purge than the one the owner saw.
    """
    lowered = argument.lower()

    if lowered.startswith("chat "):
        title = argument[5:].strip()
        monitor = await session.scalar(
            sa.select(ChatMonitor).where(ChatMonitor.title.ilike(f"%{title}%")).limit(1)
        )
        if monitor is None and title.lstrip("-").isdigit():
            monitor = await session.scalar(
                sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == int(title))
            )
        if monitor is None:
            return None, ""
        return (
            await purge.plan_chat(session, monitor.tg_chat_id),
            f"unut:c:{monitor.tg_chat_id}",
        )

    span = purge.parse_range(argument)
    if span is not None:
        start, end = span
        return (
            await purge.plan_range(session, start, end),
            f"unut:d:{start.isoformat()}:{end.isoformat()}",
        )

    people = list(await session.scalars(sa.select(Person)))
    person, score = best_match(argument, people)
    if person is None or score < 70:
        return None, ""
    return await purge.plan_person(session, person), f"unut:p:{person.id}"


@router.callback_query(F.data.startswith("md:"))
async def on_media_button(callback: CallbackQuery) -> None:
    """The owner's answer to "shall I read this?".

    Only records the decision. The file lives in a private Telegram chat that
    the assistant bot cannot see — the userbot fetches it on its next sweep.
    """
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    answer, raw_id = parts[1], parts[2]

    async with session_scope() as session:
        interaction = await session.get(Interaction, int(raw_id))
        if interaction is None or approvals.state_of(interaction) in (
            None,
            approvals.EXPIRED,
        ):
            await _edit_callback(callback, replies.MEDIA_GONE)
            return
        approvals.set_state(
            interaction,
            approvals.APPROVED if answer == "y" else approvals.DECLINED,
            answered_at=datetime.now(settings.tz).isoformat(),
        )
        body = replies.MEDIA_APPROVED if answer == "y" else replies.MEDIA_DECLINED

    await _edit_callback(callback, body)


@router.callback_query(F.data.startswith("ng:"))
async def on_new_group_button(callback: CallbackQuery) -> None:
    """Ha / Yo'q under "Yangi guruh: … — o'qiymi?".

    Only records the decision. "Ha" switches the chat on and queues a
    backfill of the last week; the userbot — the one process with a Telegram
    user session — reads it on its next sweep, exactly as with approved media.
    """
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
        await callback.answer()
        return
    answer, monitor_id = parts[1], int(parts[2])

    async with session_scope() as session:
        if answer == "y":
            monitor = await chats.accept_join(session, monitor_id)
            body = (
                replies.new_group_accepted(
                    monitor.title, monitor.tg_chat_id, chats.BACKFILL_DAYS
                )
                if monitor is not None
                else replies.NEW_GROUP_GONE
            )
        else:
            monitor = await chats.decline_join(session, monitor_id)
            body = (
                replies.new_group_declined(monitor.title, monitor.tg_chat_id)
                if monitor is not None
                else replies.NEW_GROUP_GONE
            )

    await _edit_callback(callback, body)


@router.callback_query(F.data.startswith("unut:"))
async def on_purge_button(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "no"

    if action == "no":
        await _edit_callback(callback, replies.PURGE_CANCELLED)
        return

    async with session_scope() as session:
        plan = None
        if action == "p" and len(parts) == 3:
            person = await session.get(Person, int(parts[2]))
            if person is not None:
                plan = await purge.plan_person(session, person)
        elif action == "c" and len(parts) == 3:
            plan = await purge.plan_chat(session, int(parts[2]))
        elif action == "d" and len(parts) == 4:
            plan = await purge.plan_range(
                session, date.fromisoformat(parts[2]), date.fromisoformat(parts[3])
            )

        if plan is None:
            body = replies.PURGE_EXPIRED
        else:
            result = await purge.execute(session, plan)
            body = replies.purge_done(plan, result)

    await _edit_callback(callback, body)


async def _edit_callback(callback: CallbackQuery, text: str) -> None:
    """Replace a confirmation prompt with its outcome; the buttons go away."""
    try:
        await callback.message.edit_text(text, reply_markup=None)
    except Exception:
        log.debug("could not edit the callback message", exc_info=True)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


# --- closing and correcting one record (build step 1) -----------------------


@router.message(Command("bajarildi"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    """`/bajarildi d12` — kept: promise/task done, debt settled in full."""
    handle = (command.args or "").strip()
    if not handle:
        await _safe_answer(message, replies.REF_USAGE)
        return
    async with session_scope() as session:
        outcome = await _act(
            session, keyboards.ACTION_DONE, handle, by=records.BY_COMMAND
        )
    await _safe_answer(message, outcome.text, reply_markup=outcome.keyboard)


@router.message(Command("yop"))
async def cmd_close(message: Message, command: CommandObject) -> None:
    """`/yop p7` — closed without counting as kept."""
    handle = (command.args or "").strip()
    if not handle:
        await _safe_answer(message, replies.REF_USAGE)
        return
    async with session_scope() as session:
        outcome = await _act(
            session, keyboards.ACTION_CLOSE, handle, by=records.BY_COMMAND
        )
    await _safe_answer(message, outcome.text, reply_markup=outcome.keyboard)


@router.message(Command("qaytar"))
async def cmd_reopen(message: Message, command: CommandObject) -> None:
    """`/qaytar p7` — undo a close; the same thing the ↩️ button does."""
    handle = (command.args or "").strip()
    if not handle:
        await _safe_answer(message, replies.REF_USAGE)
        return
    async with session_scope() as session:
        outcome = await _act(
            session, keyboards.ACTION_REOPEN, handle, by=records.BY_COMMAND
        )
    await _safe_answer(message, outcome.text)


@router.message(Command("tuzat"))
async def cmd_edit(message: Message, command: CommandObject) -> None:
    """`/tuzat d12 6 mln` — one field of one row; the old value is kept.

    A refusal explains itself: an amount below the recorded repayments, a
    currency change with payments on the books, a field the kind does not
    have. A name MIYA does not know is not created on the spot — the owner
    decided anything uncertain is asked first — so it comes back as a
    question with Ha / Yo'q.
    """
    # Any whitespace after the ref: a newline or a tab is as good as a space.
    parts = (command.args or "").split(None, 1)
    handle = parts[0] if parts else ""
    edit = records.parse_edit(parts[1] if len(parts) > 1 else "")
    if not handle or edit is None:
        await _safe_answer(message, replies.TUZAT_USAGE)
        return
    keyboard = None
    async with session_scope() as session:
        found = await records.find(session, handle)
        if found is None:
            body = replies.RECORD_NOT_FOUND.format(ref=escape(handle))
        else:
            kind, record = found
            try:
                change = await records.set_field(
                    session, record, edit.field, edit.value, by=records.BY_COMMAND
                )
                body = replies.record_edited(change)
            except records.NotEditable:
                body = replies.FIELD_NOT_EDITABLE[kind]
            except records.PaymentsExceed as exc:
                body = replies.debt_payments_exceed(ref_of(record), exc)
            except records.PaymentsExist:
                body = replies.DEBT_CURRENCY_LOCKED.format(ref=ref_of(record))
            except records.UnknownPerson as exc:
                index = records.ask_new_person(record, exc.name, by=records.BY_COMMAND)
                body = replies.new_person_question(exc.name)
                keyboard = keyboards.new_person_question(ref_of(record), index)
    await _safe_answer(message, body, reply_markup=keyboard)


async def _answer_new_person(session, action: str, handle: str, index: int) -> str:
    """Ha / Yo'q on "Yangi odam 'Sardor' yaratilsinmi?".

    Only "Ha" creates the person and attaches the row to it; "Yo'q" changes
    nothing. The name comes from the row's history, where the question was
    recorded, so a button pressed after a restart still knows it.
    """
    found = await records.find(session, handle)
    if found is None:
        return replies.RECORD_NOT_FOUND.format(ref=escape(handle))
    kind, record = found
    name = records.pending_person(record, index)
    if name is None:
        return replies.NEW_PERSON_EXPIRED
    if action == keyboards.ACTION_PERSON_NO:
        return replies.NEW_PERSON_DECLINED.format(name=escape(name))
    try:
        change = await records.set_field(
            session, record, "person", name, by=records.BY_BUTTON, create_person=True
        )
    except records.NotEditable:
        return replies.FIELD_NOT_EDITABLE[kind]
    return replies.record_edited(change)


class _Outcome(NamedTuple):
    """What one action produced: the reply, whether the row's buttons should
    go, and the keyboard the reply carries (the ↩️ Qaytar after a close).

    "Finished" means the row was closed or reopened, or the owner said it is
    still open and will be asked again in a week.
    """

    text: str
    finished: bool
    keyboard: InlineKeyboardMarkup | None = None


async def _act(session, action: str, handle: str, *, by: str) -> _Outcome:
    """Run one button/command action on one ref."""
    found = await records.find(session, handle)
    if found is None:
        return _Outcome(replies.RECORD_NOT_FOUND.format(ref=escape(handle)), True)
    kind, record = found
    person = records.person_of(record)
    undo = keyboards.reopen_actions([(kind, record.id)])
    try:
        if action == keyboards.ACTION_DONE or (
            action == keyboards.ACTION_SETTLE_BALANCE and kind != "debt"
        ):
            change = await records.mark_done(session, record, by=by)
            return _Outcome(replies.record_done(change), True, undo)
        if action == keyboards.ACTION_SETTLE_BALANCE:
            # A "Hali ochiqmi?" line is a balance; its ✅ settles every row.
            changes = await records.settle_balance(session, record, by=by)
            return _Outcome(
                replies.balance_settled(changes),
                True,
                keyboards.reopen_actions([(c.kind, c.record.id) for c in changes]),
            )
        if action == keyboards.ACTION_CLOSE:
            change = await records.close(session, record, by=by)
            return _Outcome(replies.record_closed(change), True, undo)
        if action == keyboards.ACTION_REOPEN:
            change = await records.reopen(session, record, by=by)
            return _Outcome(replies.record_reopened(change), True)
        if action == keyboards.ACTION_FLIP:
            if kind != "debt":
                return _Outcome(replies.FIELD_NOT_EDITABLE[kind], False)
            change = await records.flip(session, record, by=by)
            return _Outcome(replies.record_edited(change), False)
        if action == keyboards.ACTION_OPEN:
            # A debt question is about a balance and its button is keyed by
            # the first row: "open" means any row of the balance is open.
            if kind == "debt":
                rows = await records.open_in_balance(session, record)
            else:
                rows = [record] if records.is_open(record) else []
            if not rows:
                # A stale "Ha" on a row closed since the question went out:
                # nothing to keep open, and no ack to log.
                return _Outcome(replies.RECORD_ALREADY_CLOSED, True)
            await reminders.acknowledge(session, kind, record)
            return _Outcome(replies.record_still_open(kind, rows, person), True)
        return _Outcome(replies.tuzat_hint(kind, ref_of(record)), False)
    except records.NotOpen:
        return _Outcome(replies.RECORD_ALREADY_CLOSED, True)
    except records.AlreadyOpen:
        return _Outcome(replies.RECORD_ALREADY_OPEN, True)
    except records.NotReopenable:
        return _Outcome(replies.DEBT_NOT_REOPENABLE.format(ref=ref_of(record)), True)
    except records.NotClosable:
        return _Outcome(replies.DEBT_NOT_CLOSABLE.format(ref=ref_of(record)), False)


async def _answer_nudge(session, action: str, handle: str) -> str:
    """✅ Javob berdim / ⏰ Ertaga on one nudged question.

    "Answered" is written on the interaction's own metadata and is final: the
    question leaves the brief, the report and the sweep at once. "Ertaga"
    only snoozes the nudge until the next morning brief; the question itself
    stays open and listed.
    """
    interaction_id = keyboards.parse_question_ref(handle)
    interaction = (
        await session.get(Interaction, interaction_id)
        if interaction_id is not None
        else None
    )
    if interaction is None:
        return replies.NUDGE_GONE
    if action == keyboards.ACTION_QUESTION_ANSWERED:
        nudges.mark_answered(interaction, by=records.BY_BUTTON)
        # And every follow-up, not only the newest: the open-loops engine
        # walks only rows older than LOOP_QUESTION_HOURS, so a mark on a
        # follow-up sent an hour ago is invisible to it for hours, and an
        # older, unmarked "qachon?" would surface as a fresh nudge the moment
        # this one was closed.
        for row in await _follow_ups(session, interaction):
            nudges.mark_answered(row, by=records.BY_BUTTON)
        return replies.NUDGE_ANSWERED
    until = nudges.next_morning()
    nudges.snooze(interaction, until=until)
    return replies.nudge_snoozed(until)


async def _follow_ups(session, question: Interaction) -> list[Interaction]:
    """Every incoming userbot message in the question's chat since it, oldest
    first.

    In a group only messages aimed at the owner count, the same rows the
    open-loops engine reads there; other people talking afterwards is not
    a follow-up. Empty when the question is the newest message itself.
    """
    if question.tg_chat_id is None:
        return []
    chat_type = await session.scalar(
        sa.select(ChatMonitor.chat_type).where(
            ChatMonitor.tg_chat_id == question.tg_chat_id
        )
    )
    stmt = (
        sa.select(Interaction)
        .where(Interaction.source == InteractionSource.telegram_userbot)
        .where(Interaction.direction == Direction.in_)
        .where(Interaction.tg_chat_id == question.tg_chat_id)
        .where(Interaction.occurred_at >= question.occurred_at)
        .where(Interaction.id != question.id)
        .order_by(Interaction.occurred_at, Interaction.id)
    )
    if chat_type is not ChatType.private:
        stmt = stmt.where(queries._addressed_to_owner())
    return list(await session.scalars(stmt))


@router.callback_query(F.data.startswith("rec:"))
async def on_record_button(callback: CallbackQuery) -> None:
    """✅ / ✏️ / 🔄 / Ha / Yop / ↩️ Qaytar on a confirmation, reminder or outcome.

    The outcome goes out as its own message — the confirmation or reminder
    stays readable — and the finished row's buttons are removed, leaving the
    others in place.
    """
    parts = (callback.data or "").split(":")
    if len(parts) == 4 and parts[1] in keyboards.PERSON_ANSWERS and parts[3].isdigit():
        async with session_scope() as session:
            text = await _answer_new_person(session, parts[1], parts[2], int(parts[3]))
        await _edit_callback(callback, text)
        return
    if len(parts) != 3:
        await callback.answer()
        return
    action, handle = parts[1], parts[2]
    if action in keyboards.QUESTION_ANSWERS:
        # A nudged question, not a record: "q<interaction id>".
        async with session_scope() as session:
            text = await _answer_nudge(session, action, handle)
        await _edit_callback(callback, text)
        return

    async with session_scope() as session:
        outcome = await _act(session, action, handle, by=records.BY_BUTTON)

    if outcome.finished and callback.message is not None:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=keyboards.without(callback.message.reply_markup, handle)
            )
        except Exception:
            log.debug("could not trim the record keyboard", exc_info=True)
    if callback.message is not None:
        await _safe_answer(callback.message, outcome.text, reply_markup=outcome.keyboard)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


@router.message(Command("kim"))
async def cmd_person(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await _safe_answer(message, "Ism yozing: <code>/kim Akmal</code>")
        return

    async with session_scope() as session:
        people = list(await session.scalars(sa.select(Person)))
        person, score = best_match(name, people)
        if person is None or score < 70:
            body = replies.person_not_found(name)
        else:
            body = replies.person_report(await queries.person_summary(session, person))
    await _safe_answer(message, body)


@router.message(Command("process"))
async def cmd_process(message: Message, bot: Bot) -> None:
    """On-demand processing of a media message (spec §6, §7A).

    Works both ways the owner would reach for it: replying `/process` to a
    forwarded video, or sending the video with `/process` as its caption.
    """
    target = message.reply_to_message or message
    if target is message and not (
        message.video
        or message.video_note
        or message.voice
        or message.audio
        or message.document
        or message.photo
    ):
        await _safe_answer(message, replies.PROCESS_NO_TARGET)
        return

    media = (
        target.video
        or target.video_note
        or target.voice
        or target.audio
        or target.document
        or (target.photo[-1] if target.photo else None)
    )
    if media is None:
        await _safe_answer(message, replies.PROCESS_NO_MEDIA)
        return

    await _typing(message)
    is_video = bool(target.video or target.video_note)
    is_audio = bool(target.voice or target.audio)
    is_photo = bool(target.photo)
    suffix = Path(getattr(media, "file_name", "") or "").suffix
    if not suffix:
        suffix = (
            ".mp4" if is_video else ".jpg" if is_photo else ".ogg" if is_audio else ".bin"
        )

    path = _media_path(suffix)
    if not await _download(bot, message, media, path):
        return

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=target.caption,
            occurred_at=target.date.astimezone(settings.tz),
            media={
                "type": "video" if is_video else "photo" if is_photo else "file",
                "path": str(path),
                "filename": getattr(media, "file_name", None),
                "caption": target.caption,
                "processed": False,
            },
            meta={"tg_message_id": target.message_id, "on_demand": True},
        )
        reply, keyboard = await _process_on_demand(
            session,
            interaction,
            path,
            is_video=is_video,
            is_audio=is_audio,
            is_photo=is_photo,
        )
    await _safe_answer(message, reply, reply_markup=keyboard)


async def _process_on_demand(
    session, interaction, path: Path, *, is_video: bool, is_audio: bool, is_photo: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Run the right pipeline for one explicitly requested media file."""
    if is_video:
        audio_path = path.with_suffix(".mp3")
        if not await audio.extract_audio(path, audio_path):
            interaction.needs_review = True
            return replies.TRANSCRIPTION_FAILED_HINT, None
        interaction.media = {**(interaction.media or {}), "audio_path": str(audio_path)}
        if await transcribe_into(session, interaction, audio_path) is None:
            return replies.TRANSCRIPTION_FAILED_HINT, None
    elif is_audio:
        if await transcribe_into(session, interaction, path) is None:
            return replies.TRANSCRIPTION_FAILED_HINT, None
    elif is_photo:
        described = await describe_into(session, interaction, path)
        if described is None and not interaction.raw_text:
            return replies.PHOTO_FAILED_HINT, None
    else:
        parsed = await documents.read_document_async(path)
        if parsed is None and not interaction.raw_text:
            interaction.needs_review = True
            return replies.DOCUMENT_FAILED_HINT, None
        if parsed is not None:
            interaction.transcript = parsed.text

    interaction.media = {**(interaction.media or {}), "processed": True}
    result = await process_interaction(session, interaction)
    return _receipt(result)


# --- content ----------------------------------------------------------------


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    await _typing(message)
    if rag.looks_like_question(message.text):
        # Questions are answered, not extracted — but they still land in
        # interactions ("every input lands here"), marked so `/tekshir` and
        # the extractor both leave them alone.
        async with session_scope() as session:
            interaction = await create_interaction(
                session,
                source=InteractionSource.assistant_bot,
                direction=Direction.in_,
                text=message.text,
                occurred_at=message.date.astimezone(settings.tz),
                meta={"kind": "question"},
            )
            interaction.processed = True
            reply = clip(await rag.answer(session, message.text))
        await _safe_answer(message, reply)
        return

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=message.text,
            occurred_at=message.date.astimezone(settings.tz),
        )
        result = await process_interaction(session, interaction)
        reply, keyboard = _receipt(result)
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    await _typing(message)
    media = message.voice or message.audio
    suffix = ".ogg" if message.voice else ".mp3"
    path = _media_path(suffix)
    if not await _download(bot, message, media, path):
        return

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=message.caption,
            occurred_at=message.date.astimezone(settings.tz),
            media={
                "type": "voice",
                "path": str(path),
                "mime": media.mime_type,
                "size": media.file_size,
                "duration": getattr(media, "duration", None),
                "caption": message.caption,
                "processed": False,
            },
        )
        text = await transcribe_into(session, interaction, path)
        keyboard = None
        if text is None:
            reply = replies.TRANSCRIPTION_FAILED_HINT
        else:
            interaction.media = {**(interaction.media or {}), "processed": True}
            result = await process_interaction(session, interaction)
            reply, keyboard = _receipt(result)
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    await _typing(message)
    photo = message.photo[-1]  # highest resolution
    path = _media_path(".jpg")
    if not await _download(bot, message, photo, path):
        return

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.receipt_photo,
            direction=Direction.in_,
            text=message.caption,
            occurred_at=message.date.astimezone(settings.tz),
            media={
                "type": "photo",
                "path": str(path),
                "size": photo.file_size,
                "caption": message.caption,
                "processed": False,
            },
        )
        # Photos sent straight to the bot are always vision-processed (spec §6).
        described = await describe_into(session, interaction, path)
        keyboard = None
        if described is None and not message.caption:
            reply = replies.PHOTO_FAILED_HINT
        else:
            interaction.media = {**(interaction.media or {}), "processed": True}
            result = await process_interaction(session, interaction)
            reply, keyboard = _receipt(result)
            if result.ok and described is None:
                # The caption was extracted, but the image itself was not read —
                # the owner must not be told everything succeeded.
                reply = reply + "\n" + replies.VISION_PARTIAL_HINT
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    await _typing(message)
    document = message.document
    suffix = Path(document.file_name or "").suffix or ".bin"
    path = _media_path(suffix)
    if not await _download(bot, message, document, path):
        return

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=message.caption,
            occurred_at=message.date.astimezone(settings.tz),
            media={
                "type": "document",
                "path": str(path),
                "filename": document.file_name,
                "mime": document.mime_type,
                "size": document.file_size,
                "caption": message.caption,
                "processed": False,
            },
        )
        parsed = await documents.read_document_async(path)
        keyboard = None
        if parsed is None and not message.caption:
            interaction.needs_review = True
            reply = replies.DOCUMENT_FAILED_HINT
        else:
            if parsed is not None:
                interaction.transcript = parsed.text
                interaction.meta = {
                    **(interaction.meta or {}),
                    "document": {
                        "truncated": parsed.truncated,
                        "detail": parsed.detail,
                    },
                }
            interaction.media = {**(interaction.media or {}), "processed": True}
            result = await process_interaction(session, interaction)
            reply, keyboard = _receipt(result)
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.video_note)
async def on_video_note(message: Message, bot: Bot) -> None:
    """A video note is a voice note with a face — transcribed always (spec §6)."""
    await _typing(message)
    path = _media_path(".mp4")
    if not await _download(bot, message, message.video_note, path):
        return

    audio_path = path.with_suffix(".mp3")
    keyboard = None
    async with session_scope() as session:
        # The row is created *before* ffmpeg runs. A failed audio extraction
        # must still leave a needs_review interaction the owner can find in
        # /tekshir — returning early here would drop the message entirely
        # while telling him it had been saved.
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=message.date.astimezone(settings.tz),
            media={
                "type": "video_note",
                "path": str(path),
                "size": message.video_note.file_size,
                "duration": getattr(message.video_note, "duration", None),
                "processed": False,
            },
        )
        if not await audio.extract_audio(path, audio_path):
            interaction.needs_review = True
            reply = replies.TRANSCRIPTION_FAILED_HINT
        else:
            interaction.media = {
                **(interaction.media or {}),
                "audio_path": str(audio_path),
            }
            text = await transcribe_into(session, interaction, audio_path)
            if text is None:
                reply = replies.TRANSCRIPTION_FAILED_HINT
            else:
                interaction.media = {**(interaction.media or {}), "processed": True}
                result = await process_interaction(session, interaction)
                reply, keyboard = _receipt(result)
    # Replied only after the commit, like every other handler here.
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.video)
async def on_video(message: Message, bot: Bot) -> None:
    """Videos are stored, not transcribed — `/process` opts into the cost."""
    media = message.video
    path = _media_path(".mp4")
    if not await _download(bot, message, media, path):
        return

    async with session_scope() as session:
        await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=message.caption,
            occurred_at=message.date.astimezone(settings.tz),
            media={
                "type": "video",
                "path": str(path),
                "size": media.file_size,
                "duration": getattr(media, "duration", None),
                "caption": message.caption,
                "processed": False,
            },
            meta={"tg_message_id": message.message_id},
        )
    await _safe_answer(message, replies.VIDEO_STORED_HINT)


@router.message(F.sticker)
async def on_sticker(message: Message) -> None:
    """Stickers carry nothing to extract (spec §6) — silently ignored."""


@router.message()
async def on_unsupported(message: Message) -> None:
    """The owner sent something no handler takes (a contact, a location, a
    poll, a mistyped command). Silence would look like MIYA recorded it — say
    plainly it did not.
    """
    text = (message.text or "").strip()
    if text.startswith("/"):
        # A typo'd command must not be reported as an unsupported *media type*.
        await _safe_answer(message, replies.UNKNOWN_COMMAND_HINT)
        return
    await _safe_answer(message, replies.UNSUPPORTED_HINT)


def build_reject_router() -> Router:
    """Log and drop everything from anyone who is not the owner."""
    reject = Router(name="reject")

    @reject.message()
    async def _reject(message: Message) -> None:
        log.warning(
            "ignored message from non-owner user_id=%s chat_id=%s",
            message.from_user.id if message.from_user else None,
            message.chat.id,
        )

    return reject
