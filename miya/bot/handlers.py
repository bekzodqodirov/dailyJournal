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
import re
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
from miya.bot.formatting import clip, escape, parse_ref, ref_of, short_date
from miya.bot.keyboards import FIELD_CODES, PAGE_SIZE, ChatsPage, chats_keyboard
from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource, TransactionType
from miya.db.models import ChatMonitor, ClientCode, Interaction, Person
from miya.db.session import session_scope
from miya.services import (
    approvals,
    audio,
    brief,
    chats,
    claims,
    client_import,
    documents,
    health,
    memories,
    money_events,
    nudges,
    planner,
    purge,
    queries,
    questions,
    rag,
    records,
    reminders,
    reports,
)
from miya.services import codes as client_codes
from miya.services.embeddings import EmbeddingError, get_embedder
from miya.services.ingest import (
    create_interaction,
    describe_into,
    process_interaction,
    text_for_extraction,
    transcribe_into,
)
from miya.services.people import find_person
from miya.services.text import fold_apostrophes

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
    """The confirmation text plus its ✅ / ✏️ / 🔄 rows, one per recorded row,
    and a Ha / Yo'q / Tuzat row per claim a counterparty made in it.

    The receipt is the ask: the claims it carries buttons for are marked as
    asked here, inside the session scope that produced it, so the commit
    that makes the rows durable records the question too. A send that then
    fails leaves the claim pending in /davolar and the brief — never lost.
    """
    if not result.ok:
        return replies.FAILED_EXTRACTION_HINT, None
    applied = result.applied
    keyboard = keyboards.applied_actions(
        replies.confirmation_refs(applied), replies.confirmation_claim_ids(applied)
    )
    _ask(applied.claims, keyboard)
    return replies.confirmation(applied), keyboard


def _ask(pending, keyboard: InlineKeyboardMarkup | None) -> None:
    """Mark as asked exactly the claims the keyboard carries a row for.

    Read off the built keyboard, not re-derived: a keyboard has a ceiling,
    and a claim whose row was capped away was not asked.
    """
    shown = set(keyboards.claim_ids_in(keyboard))
    for claim in pending:
        if claim.id in shown:
            claims.mark_asked(claim)


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
async def cmd_review(message: Message, command: CommandObject | None = None) -> None:
    """`/tekshir`: money texts with one-tap answers, then failed rows;
    `/tekshir hammasi` also lists what the reader ignored in the last week."""
    everything = (command.args or "").strip().lower() == "hammasi" if command else False
    now = datetime.now(settings.tz)
    async with session_scope() as session:
        money_rows, money_total = await queries.flagged_money(session)
        flagged, total = await queries.flagged_interactions(session)
        ignored, ignored_total = [], 0
        if everything:
            ignored, ignored_total = await queries.ignored_money(
                session, now - timedelta(days=keyboards.REVIEW_OLD_DAYS)
            )
        body = replies.review_report(
            flagged, total, money_rows, money_total, ignored, ignored_total
        )
        keyboard = keyboards.money_review(money_rows, flagged, ignored, now=now)
    await _safe_answer(message, body, reply_markup=keyboard)


def _unresolved_ignored(interaction: Interaction) -> bool:
    money = (interaction.media or {}).get("money") or {}
    return money.get("verdict") == "ignore" and "resolved" not in money


async def _answer_review(session, action: str, target: str | None) -> str:
    """One tap in /tekshir (WP-14)."""
    now = datetime.now(settings.tz)
    if action == keyboards.REVIEW_OLD:
        rows, _ = await queries.flagged_money(session, limit=1000)
        old = [
            r
            for r in rows
            if now - r.occurred_at > timedelta(days=keyboards.REVIEW_OLD_DAYS)
        ]
        for row in old:
            money_events.resolve_not_money(row, by=records.BY_BUTTON, now=now)
        return replies.REVIEW_NOT_MONEY_BULK.format(n=len(old))
    if target is None or not target.isdigit():
        return replies.REVIEW_GONE
    interaction = await session.get(Interaction, int(target), with_for_update=True)
    if interaction is None or not (
        interaction.needs_review or _unresolved_ignored(interaction)
    ):
        return replies.REVIEW_GONE
    if action == keyboards.REVIEW_SEEN:
        interaction.meta = {
            **(interaction.meta or {}),
            "reviewed": {"at": now.isoformat(), "by": records.BY_BUTTON},
        }
        interaction.needs_review = False
        return replies.REVIEW_SEEN
    if action == keyboards.REVIEW_NOT_MONEY:
        money_events.resolve_not_money(interaction, by=records.BY_BUTTON, now=now)
        return replies.REVIEW_NOT_MONEY
    txn_type = (
        TransactionType.income
        if action == keyboards.REVIEW_INCOME
        else TransactionType.expense
    )
    try:
        txn, merged = await money_events.book_from_review(
            session, interaction, txn_type, by=records.BY_BUTTON, now=now
        )
    except money_events.NotBookable:
        return replies.REVIEW_GONE
    if merged:
        return replies.REVIEW_MERGED.format(id=txn.id)
    return replies.REVIEW_BOOKED.format(line=replies.txn_short(txn), id=txn.id)


@router.callback_query(F.data.startswith(f"{keyboards.REVIEW_PREFIX}:"))
async def on_review_button(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    target = parts[2] if len(parts) > 2 else None
    async with session_scope() as session:
        text = await _answer_review(session, action, target)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=keyboards.without_prefixed(
                    callback.message.reply_markup,
                    keyboards.REVIEW_PREFIX,
                    target if action != keyboards.REVIEW_OLD else action,
                )
            )
        except Exception:
            log.debug("could not trim the review keyboard", exc_info=True)
        await _safe_answer(callback.message, text)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


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
    day = datetime.now(settings.tz).date()
    async with session_scope() as session:
        content = await reports.generate_report(session, day)
    await _safe_answer(message, clip(f"{reports.report_header(day)}\n\n{content}"))


@router.message(Command("ertalab"))
async def cmd_brief(message: Message) -> None:
    """The morning brief on demand — the same message the worker sends at
    MORNING_BRIEF_TIME, with the same buttons. No model call: it is SQL."""
    async with session_scope() as session:
        data = await brief.gather(session)
        data.queue = questions.summarise(
            await questions.collect(session, for_push=False), []
        )
        body = replies.morning_brief(data)
        due, _ = replies.morning_brief_refs(data)
        keyboard = keyboards.brief_actions(due)
    await _safe_answer(message, body, reply_markup=keyboard)


# --- /savollar: everything waiting for a tap, paged (WP-19) -------------------


async def _savollar_page(session, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    now = datetime.now(settings.tz)
    pending = await questions.collect(session, now=now, for_push=False)
    groups = [p for p in pending if p.kind == questions.KIND_GROUPS]
    items = [p for p in pending if p.kind != questions.KIND_GROUPS]
    size = replies.SAVOLLAR_PAGE_SIZE
    pages = max(1, -(-len(items) // size))
    page = min(max(page, 1), pages)
    shown = items[(page - 1) * size : page * size]
    used = await questions.spent_today(session, now=now)
    people = {}
    ids = {
        p.subject.person_id
        for p in shown
        if p.kind == questions.KIND_MEDIA and p.subject.person_id
    }
    if ids:
        people = {
            person.id: person
            for person in await session.scalars(
                sa.select(Person).where(Person.id.in_(ids))
            )
        }
    total = len(items) + sum(len(g.subject) for g in groups)
    body = replies.savollar(
        shown,
        page=page,
        pages=pages,
        total=total,
        used=used,
        budget=settings.question_budget_per_day,
        people=people,
    )
    markup = keyboards.question_batch(shown)
    rows = list(markup.inline_keyboard) if markup is not None else []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sv:p:{page - 1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sv:p:{page + 1}"))
    if nav:
        rows.append(nav)
    if groups:
        rows.append(
            [
                InlineKeyboardButton(
                    text=replies.SAVOLLAR_GROUPS_BUTTON.format(k=len(groups[0].subject)),
                    callback_data="sv:g",
                )
            ]
        )
    # A pull: nothing goes into question_log, but a claim the owner now sees
    # counts as asked for today, as on /davolar.
    for item in shown:
        if item.kind == questions.KIND_CLAIM:
            claims.mark_asked(item.subject, now=now)
    return body, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("savollar"))
async def cmd_questions(message: Message) -> None:
    """`/savollar` — every question waiting for the owner, ranked, paged.
    Answering here spends none of the day's budget."""
    async with session_scope() as session:
        body, keyboard = await _savollar_page(session, 1)
    await _safe_answer(message, body, reply_markup=keyboard)


@router.callback_query(F.data.startswith("sv:"))
async def on_savollar_button(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    async with session_scope() as session:
        if parts[1:2] == ["g"]:
            monitors = await chats.awaiting_join_question(
                session, limit=settings.question_group_digest_size, for_push=False
            )
            body = replies.group_digest(monitors) if monitors else replies.SAVOLLAR_EMPTY
            keyboard = keyboards.group_digest(monitors) if monitors else None
        else:
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
            body, keyboard = await _savollar_page(session, page)
    if callback.message is not None:
        await _safe_answer(callback.message, body, reply_markup=keyboard)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


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


@router.message(Command("holat"))
async def cmd_status(message: Message) -> None:
    """`/holat` — is every part of MIYA alive, and what to type if not.

    The judgement (services/health.py) is the same one the worker's health
    job alerts on; asking here is only ever a read.
    """
    waiting: int | None = None
    used = 0
    try:
        async with session_scope() as session:
            status = await health.gather(session)
            now = datetime.now(settings.tz)
            used = await questions.spent_today(session, now=now)
            waiting = len(await questions.collect(session, now=now, for_push=False))
    except Exception:
        # The one report that must still come out when the database is
        # down: the db_down line and its remedy, from what the bot can see.
        log.exception("/holat: the database did not answer")
        status = health.Status.unreachable(datetime.now(settings.tz))
    await _safe_answer(
        message,
        replies.status_report(
            status,
            health.problems(status),
            questions_waiting=waiting,
            questions_used=used,
        ),
    )


@router.message(Command("unut"))
async def cmd_purge(message: Message, command: CommandObject) -> None:
    """`/unut` — destructive, so it only ever shows a plan and asks first."""
    argument = (command.args or "").strip()
    if not argument:
        await _safe_answer(message, replies.PURGE_USAGE)
        return

    async with session_scope() as session:
        plan, payload = await _build_purge_plan(session, argument)
        if isinstance(plan, str):
            # Two people answer to the name, or a code nobody holds: ask,
            # and offer nothing to press — a destructive command never guesses.
            await _safe_answer(message, plan)
            return
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

    match = await find_person(session, argument)
    if match.person is None:
        if match.unknown_code:
            return replies.code_unknown(match.unknown_code), ""
        return None, ""
    if match.ambiguous:
        codes = await client_codes.codes_of_many(
            session, [p.id for p in (match.person, match.runner_up) if p is not None]
        )
        return replies.person_ambiguous(match, command="unut", codes=codes), ""
    person = match.person
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
            await _finish_row(
                callback, _trimmed(callback, "md", raw_id), replies.MEDIA_GONE
            )
            return
        approvals.set_state(
            interaction,
            approvals.APPROVED if answer == "y" else approvals.DECLINED,
            answered_at=datetime.now(settings.tz).isoformat(),
        )
        body = replies.MEDIA_APPROVED if answer == "y" else replies.MEDIA_DECLINED

    await _finish_row(callback, _trimmed(callback, "md", raw_id), body)


@router.callback_query(F.data.startswith("ng:"))
async def on_new_group_button(callback: CallbackQuery) -> None:
    """Ha / Yo'q under "Yangi guruh: … — o'qiymi?".

    Only records the decision. "Ha" switches the chat on and queues a
    backfill of the last week; the userbot — the one process with a Telegram
    user session — reads it on its next sweep, exactly as with approved media.
    """
    parts = (callback.data or "").split(":")
    markup = callback.message.reply_markup if callback.message is not None else None
    if parts[1:2] == ["r"]:
        # "✖️ Qolganlari kerak emas": every group the digest still lists.
        ids = keyboards.group_ids_in(markup)
        async with session_scope() as session:
            declined = [
                m for m in [await chats.decline_join(session, i) for i in ids] if m
            ]
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                log.debug("could not clear the digest keyboard", exc_info=True)
            await _safe_answer(
                callback.message, replies.GROUP_REST_DONE.format(n=len(declined))
            )
        try:
            await callback.answer()
        except Exception:
            log.debug("could not acknowledge the callback", exc_info=True)
        return
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

    trimmed = _trimmed(callback, "ng", monitor_id)
    if trimmed is not None and not keyboards.group_ids_in(trimmed):
        trimmed = None  # only the rest row was left
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                log.debug("could not clear the digest keyboard", exc_info=True)
            await _safe_answer(callback.message, body)
            try:
                await callback.answer()
            except Exception:
                log.debug("could not acknowledge the callback", exc_info=True)
            return
    await _finish_row(callback, trimmed, body)


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


async def _finish_row(callback: CallbackQuery, trimmed, text: str) -> None:
    """Answer one tap. Inside a numbered batch (or when other rows are left)
    only this row goes and the outcome is its own message; a message about
    one thing is edited in place, as before."""
    markup = callback.message.reply_markup if callback.message is not None else None
    if callback.message is not None and (
        keyboards.is_numbered(markup) or trimmed is not None
    ):
        try:
            await callback.message.edit_reply_markup(reply_markup=trimmed)
        except Exception:
            log.debug("could not trim the question keyboard", exc_info=True)
        await _safe_answer(callback.message, text)
        try:
            await callback.answer()
        except Exception:
            log.debug("could not acknowledge the callback", exc_info=True)
        return
    await _edit_callback(callback, text)


def _trimmed(callback: CallbackQuery, prefix: str, ident):
    markup = callback.message.reply_markup if callback.message is not None else None
    return keyboards.without_prefixed(markup, prefix, ident)


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


@router.message(Command("ochir"))
async def cmd_void(message: Message, command: CommandObject) -> None:
    """`/ochir x12` — take one wrong money row out of every total; ↩️ undoes."""
    handle = (command.args or "").strip()
    if not handle:
        await _safe_answer(message, replies.OCHIR_USAGE)
        return
    parsed = parse_ref(handle)
    if parsed is not None and parsed[0] != "transaction":
        await _safe_answer(message, replies.OCHIR_ONLY_MONEY)
        return
    async with session_scope() as session:
        outcome = await _act(
            session, keyboards.ACTION_VOID, handle, by=records.BY_COMMAND
        )
    await _safe_answer(message, outcome.text, reply_markup=outcome.keyboard)


@router.message(Command("pul"))
async def cmd_money_day(message: Message, command: CommandObject) -> None:
    """`/pul`, `/pul kecha`, `/pul 2026-09-20` — one day's money rows by ref."""
    arg = (command.args or "").strip().lower()
    today = datetime.now(settings.tz).date()
    if not arg:
        day = today
    elif arg == "kecha":
        day = today - timedelta(days=1)
    else:
        try:
            day = date.fromisoformat(arg)
        except ValueError:
            await _safe_answer(message, replies.PUL_USAGE)
            return
    async with session_scope() as session:
        rows = await queries.transactions_on(session, day, include_voided=True)
    active = [("transaction", t.id) for t in rows if t.voided_at is None]
    await _safe_answer(
        message,
        replies.transactions_list(day, rows),
        reply_markup=keyboards.record_actions(active[: keyboards.MAX_ROWS]),
    )


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
        claim_id = claims.parse_ref(handle)
        if claim_id is not None:
            # "c12": a counterparty's claim, corrected before it is answered.
            body, keyboard = await _edit_claim(session, claim_id, edit)
            found = None
        else:
            found = await records.find(session, handle)
            if found is None:
                body = replies.RECORD_NOT_FOUND.format(ref=escape(handle))
        if found is not None:
            kind, record = found
            try:
                change = await records.set_field(
                    session, record, edit.field, edit.value, by=records.BY_COMMAND
                )
                body = replies.record_edited(change)
            except records.NotEditable:
                body = replies.FIELD_NOT_EDITABLE[kind]
            except records.NotOpen:
                # Only a voided money row is locked against edits.
                body = replies.TXN_VOIDED_LOCKED.format(ref=ref_of(record))
            except records.PaymentsExceed as exc:
                body = replies.debt_payments_exceed(ref_of(record), exc)
            except records.PaymentsExist:
                body = replies.DEBT_CURRENCY_LOCKED.format(ref=ref_of(record))
            except records.UnknownCode as exc:
                body = replies.code_unknown(exc.code)
            except records.IdentityConflict as exc:
                body = replies.identity_conflict(exc.code, exc.holder, exc.named)
            except records.UnknownPerson as exc:
                index = records.ask_new_person(record, exc.name, by=records.BY_COMMAND)
                body = replies.new_person_question(exc.name)
                keyboard = keyboards.new_person_question(ref_of(record), index)
    await _safe_answer(message, body, reply_markup=keyboard)


async def _edit_claim(
    session, claim_id: int, edit: records.Edit
) -> tuple[str, InlineKeyboardMarkup | None]:
    """`/tuzat c12 summa 4 mln`: the claim's payload changes, the claim stays
    pending, and the corrected question comes back with its buttons so the
    owner answers it right there."""
    try:
        claim = await claims.edit(session, claim_id, edit, by=claims.BY_COMMAND)
    except claims.AlreadyAnswered:
        return replies.CLAIM_ALREADY, None
    except claims.UnknownCode as exc:
        return replies.code_unknown(exc.code), None
    except ValueError:
        claim = await claims.get(session, claim_id)
        if claim is None:
            return replies.CLAIM_GONE, None
        view = claims.view(claim)
        if edit.field not in claims.EDITABLE.get(claim.kind, ()):
            body = replies.claim_field_refused(view, edit.field)
        else:
            body = replies.claim_value_refused(view)
        return body, keyboards.claim_actions([claim_id])
    if claim is None:
        return replies.CLAIM_GONE, None
    return replies.claim_edited(claims.view(claim)), keyboards.claim_actions([claim_id])


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
    except records.NotOpen:
        return replies.TXN_VOIDED_LOCKED.format(ref=ref_of(record))
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
        if kind == "transaction":
            return await _act_on_transaction(session, action, record, by=by)
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


async def _act_on_transaction(session, action: str, txn, *, by: str) -> _Outcome:
    """A money row (WP-13): voided or restored, never "done"."""
    handle = ref_of(txn)
    if action == keyboards.ACTION_DONE:
        return _Outcome(replies.TXN_NOT_DOABLE.format(ref=handle), False)
    if action in (keyboards.ACTION_VOID, keyboards.ACTION_CLOSE):
        change = await records.void(session, txn, by=by)
        return _Outcome(
            replies.record_voided(change),
            True,
            keyboards.reopen_actions([("transaction", txn.id)]),
        )
    if action == keyboards.ACTION_REOPEN:
        change = await records.reopen(session, txn, by=by)
        return _Outcome(replies.record_unvoided(change), True)
    return _Outcome(replies.tuzat_hint("transaction", handle), False)


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


async def _answer_missed(session, action: str, handle: str) -> str:
    """✅ Bog'landim / ⏰ Ertalab eslat on one missed-call loop (build step 6).

    The marks live on the loop's own interaction, exactly as they do for a
    nudged question: "Bog'landim" is final for every ring at or before it
    (loops.missed_calls reads the mark's timestamp as the floor for that
    number), "Ertalab" only snoozes the nudge — the loop itself stays open
    and listed until a real contact or the ✅ closes it.
    """
    interaction_id = keyboards.parse_missed_ref(handle)
    interaction = (
        await session.get(Interaction, interaction_id)
        if interaction_id is not None
        else None
    )
    if interaction is None:
        return replies.MISSED_GONE
    if action == keyboards.ACTION_MISSED_ANSWERED:
        nudges.mark_answered(interaction, by=records.BY_BUTTON)
        return replies.MISSED_ANSWERED
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
        markup = callback.message.reply_markup if callback.message is not None else None
        await _finish_row(callback, keyboards.without(markup, handle), text)
        return
    if action in keyboards.MISSED_ANSWERS:
        # A missed-call loop, not a record: "m<interaction id>". Unlike a
        # question nudge, its row also rides the morning brief, so the
        # outcome goes out as its own message and only the tapped row leaves
        # the keyboard — the brief's other buttons still have work to do.
        async with session_scope() as session:
            text = await _answer_missed(session, action, handle)
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=keyboards.without(callback.message.reply_markup, handle)
                )
            except Exception:
                log.debug("could not trim the missed-call keyboard", exc_info=True)
            await _safe_answer(callback.message, text)
        try:
            await callback.answer()
        except Exception:
            log.debug("could not acknowledge the callback", exc_info=True)
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


# --- a counterparty's claim: ask first (build step 3) -------------------------


@router.message(Command("davolar"))
async def cmd_claims(message: Message) -> None:
    """`/davolar` — every claim still waiting for the owner's word, oldest
    first, each with its Ha / Yo'q / Tuzat row. Listing is asking: what is
    shown with buttons is marked as asked."""
    async with session_scope() as session:
        pending = await claims.pending(session)
        shown = pending[: keyboards.MAX_ROWS]
        body = replies.claims_list(
            [claims.view(c) for c in shown], hidden=len(pending) - len(shown)
        )
        keyboard = keyboards.claim_actions([c.id for c in shown])
        _ask(shown, keyboard)
    await _safe_answer(message, body, reply_markup=keyboard)


async def _answer_claim(session, action: str, claim_id: int) -> _Outcome | None:
    """Ha / Yo'q / Tuzat on one claim. None for an action that is not one."""
    try:
        if action == keyboards.ACTION_CLAIM_YES:
            accepted = await claims.accept(session, claim_id, by=claims.BY_BUTTON)
            if accepted is None:
                return _Outcome(replies.CLAIM_GONE, True)
            # The row it became gets the receipt's own buttons — and nothing
            # in it can be a claim again, so there is no second question.
            keyboard = keyboards.applied_actions(
                replies.confirmation_refs(accepted.applied), []
            )
            # Nothing written → the claim is still pending and its row stays.
            return _Outcome(replies.claim_accepted(accepted), accepted.written, keyboard)
        if action == keyboards.ACTION_CLAIM_NO:
            claim = await claims.decline(session, claim_id, by=claims.BY_BUTTON)
            if claim is None:
                return _Outcome(replies.CLAIM_GONE, True)
            return _Outcome(replies.CLAIM_DECLINED, True)
        if action == keyboards.ACTION_CLAIM_EDIT:
            claim = await claims.get(session, claim_id)
            if claim is None:
                return _Outcome(replies.CLAIM_GONE, True)
            if claim.state != claims.PENDING:
                return _Outcome(replies.CLAIM_ALREADY, True)
            return _Outcome(replies.claim_tuzat_hint(claims.view(claim)), False)
    except claims.AlreadyAnswered:
        return _Outcome(replies.CLAIM_ALREADY, True)
    return None


@router.callback_query(F.data.startswith("cl:"))
async def on_claim_button(callback: CallbackQuery) -> None:
    """✅ Ha / ✖️ Yo'q / ✏️ Tuzat under a counterparty's claim.

    "Ha" writes the item exactly as the extraction would have, had the owner
    said it himself; "Yo'q" writes nothing. The outcome goes out as its own
    message and the answered claim's row leaves the keyboard — the receipt,
    brief or list it sat on stays readable, with its other rows intact. A
    second tap finds the claim answered (the row lock in claims.accept) and
    says so instead of writing twice.
    """
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer()
        return
    action, claim_id = parts[1], int(parts[2])

    async with session_scope() as session:
        outcome = await _answer_claim(session, action, claim_id)
    if outcome is None:
        await callback.answer()
        return

    if outcome.finished and callback.message is not None:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=keyboards.without_claim(
                    callback.message.reply_markup, claim_id
                )
            )
        except Exception:
            log.debug("could not trim the claim keyboard", exc_info=True)
    if callback.message is not None:
        await _safe_answer(callback.message, outcome.text, reply_markup=outcome.keyboard)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


async def _lookup(
    session, name: str, *, command: str
) -> tuple[Person | None, str | None]:
    """The person a question names, or the reply that says why there is none.

    A question tolerates a looser match than a write (QUESTION_THRESHOLD),
    but two people scoring alike are asked back, never guessed: a wrong
    guess here answers about the wrong person.
    """
    match = await find_person(session, name)
    if match.person is None:
        if match.unknown_code:
            return None, replies.code_unknown(match.unknown_code)
        return None, replies.person_not_found(name)
    if match.ambiguous:
        codes = await client_codes.codes_of_many(
            session, [p.id for p in (match.person, match.runner_up) if p is not None]
        )
        return None, replies.person_ambiguous(match, command=command, codes=codes)
    return match.person, None


@router.message(Command("kim"))
async def cmd_person(message: Message, command: CommandObject) -> None:
    """Everything held about one person: profile, figures, facts, history."""
    name = (command.args or "").strip()
    if not name:
        await _safe_answer(message, replies.KIM_USAGE)
        return

    async with session_scope() as session:
        person, body = await _lookup(session, name, command="kim")
        if person is not None:
            body = replies.person_report(await queries.person_summary(session, person))
    await _safe_answer(message, body)


_TARIX_COUNT = re.compile(r"^(?P<name>.+?)\s+(?P<count>\d{1,4})$")


def _parse_history_args(args: str) -> tuple[str, int]:
    """'Akmal 50' → ('Akmal', 50); 'Akmal' → ('Akmal', TARIX_DEFAULT).

    The count is clamped to [1, TARIX_MAX]: a request for a thousand lines
    is a request for as many as one message can carry.
    """
    args = client_codes.canonicalise_codes(args.strip())
    found = _TARIX_COUNT.match(args)
    if found is None:
        return args, replies.TARIX_DEFAULT
    count = min(max(int(found.group("count")), 1), replies.TARIX_MAX)
    return found.group("name").strip(), count


@router.message(Command("tarix"))
async def cmd_history(message: Message, command: CommandObject) -> None:
    """A person's contact history, oldest at the top, newest at the bottom."""
    name, count = _parse_history_args(command.args or "")
    if not name:
        await _safe_answer(message, replies.TARIX_USAGE)
        return

    async with session_scope() as session:
        person, body = await _lookup(session, name, command="tarix")
        if person is not None:
            entries = await queries.timeline(session, person.id, limit=count)
            body = replies.history_report(person, entries, requested=count)
    await _safe_answer(message, body)


# "/eslab Akmal: matn" or "/eslab Akmal — matn": the first colon or dash
# splits the name from the words.
_ESLAB_SPLIT = re.compile(r"\s*(?::|—|–)\s*")


def _parse_remember_args(args: str) -> tuple[str, str] | None:
    parts = _ESLAB_SPLIT.split(args.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    name, text = parts[0].strip(), parts[1].strip()
    if not name or not text:
        return None
    return name, text


@router.message(Command("eslab"))
async def cmd_remember(message: Message, command: CommandObject) -> None:
    """The owner tells MIYA something about a person, in his own words.

    Stored as a memory against the person (tag ``manual``); the worker
    embeds it on its next tick. No extraction: what he typed is the fact.
    """
    parsed = _parse_remember_args(command.args or "")
    if parsed is None:
        await _safe_answer(message, replies.ESLAB_USAGE)
        return
    name, text = parsed

    async with session_scope() as session:
        person, body = await _lookup(session, name, command="eslab")
        if person is not None:
            await memories.remember(
                session,
                text,
                person_id=person.id,
                occurred_at=datetime.now(settings.tz),
                tags=["manual"],
            )
            body = replies.remembered(person, text)
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


# --- client codes by hand (WP-33) ---------------------------------------------

_DETACH_WORDS = {"o'chir", "ochir", "olib tashla", "-"}


def _code_args(args: str) -> tuple[list[str], str]:
    """(the codes in the arguments, what is left once they are taken out)."""
    found = client_codes.find_client_codes(args)
    rest = client_codes.client_code_re().sub(" ", args)
    return found, " ".join(rest.split())


@router.message(Command("kod"))
async def cmd_code(message: Message, command: CommandObject) -> None:
    """`/kod Akmal GS367` — say in one line which client a code is."""
    args = fold_apostrophes((command.args or "").strip())
    if not args:
        await _safe_answer(message, replies.KOD_USAGE)
        return
    found, rest = _code_args(args)
    if len(found) != 1:
        body = replies.code_bad(args) if not found else replies.KOD_USAGE
        await _safe_answer(message, body)
        return
    code = found[0]
    keyboard = None
    async with session_scope() as session:
        holder = await client_codes.holder(session, code)
        if not rest:
            if holder is None:
                body = replies.code_unknown(code)
            else:
                summary = await queries.person_summary(session, holder)
                body = replies.person_report(summary)
        elif rest.lower() in _DETACH_WORDS:
            gone = await client_codes.detach(session, code, by=records.BY_COMMAND)
            body = (
                replies.code_detached(code, gone.display_name)
                if gone is not None
                else replies.code_unknown(code)
            )
        elif rest.lower().startswith("yangi "):
            if holder is not None:
                body = replies.code_already(code, holder.display_name)
            else:
                person = Person(display_name=rest[6:].strip(), aliases=[])
                session.add(person)
                await session.flush()
                await client_codes.attach(
                    session, person, code, source="command", by=records.BY_COMMAND
                )
                body = replies.code_attached(code, person.display_name, [code])
        else:
            body, keyboard = await _code_to_person(session, code, rest, holder)
    await _safe_answer(message, body, reply_markup=keyboard)


async def _code_to_person(session, code: str, name: str, holder: Person | None):
    match = await find_person(session, name)
    if match.person is None:
        return replies.code_person_not_found(name, code), None
    if match.ambiguous:
        held = await client_codes.codes_of_many(
            session, [p.id for p in (match.person, match.runner_up) if p is not None]
        )
        return replies.person_ambiguous(match, command="kod", codes=held), None
    person = match.person
    if holder is not None and holder.id == person.id:
        return replies.code_already(code, person.display_name), None
    if holder is not None:
        row = await session.scalar(
            sa.select(ClientCode).where(
                ClientCode.code == code, ClientCode.status == "active"
            )
        )
        return (
            replies.code_taken(code, holder.display_name, person.display_name),
            keyboards.code_move(row_id=row.id, person_id=person.id),
        )
    await client_codes.attach(
        session, person, code, source="command", by=records.BY_COMMAND
    )
    held = await client_codes.codes_of(session, person.id)
    return replies.code_attached(code, person.display_name, held), None


@router.message(Command("kodlar"), F.document)
async def cmd_code_import(message: Message, bot: Bot) -> None:
    """The owner's client list sent as a file captioned /kodlar (WP-34).

    Read by code, never by the model: the file is stored as a processed
    interaction and only a preview comes back, with one Ha to write it.
    """
    document = message.document
    suffix = Path(document.file_name or "").suffix.lower() or ".bin"
    path = _media_path(suffix)
    if not await _download(bot, message, document, path):
        return
    async with session_scope() as session:
        interaction = Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=message.date.astimezone(settings.tz),
            raw_text=message.caption,
            media={
                "type": "document",
                "path": str(path),
                "filename": document.file_name,
            },
            meta={"kind": "client_import"},
            processed=True,
        )
        session.add(interaction)
        await session.flush()
        try:
            rows = client_import.read_rows(path)
        except client_import.BadFile:
            body, keyboard = replies.IMPORT_BAD_FILE, None
        else:
            plan = await client_import.plan_import(session, rows)
            body = replies.import_preview(document.file_name or path.name, plan.counts())
            keyboard = keyboards.client_import(interaction.id)
    await _safe_answer(message, body, reply_markup=keyboard)


async def _apply_client_import(session, interaction_id: int) -> str:
    """Re-read and re-plan at press time, the way /unut re-derives its plan:
    what is written is what the file and the database say now."""
    interaction = await session.get(Interaction, interaction_id)
    if interaction is None or (interaction.meta or {}).get("kind") != "client_import":
        return replies.CODE_STALE
    try:
        rows = client_import.read_rows((interaction.media or {}).get("path", ""))
    except (client_import.BadFile, OSError):
        return replies.IMPORT_BAD_FILE
    plan = await client_import.plan_import(session, rows)
    written = await client_import.apply_import(session, plan, by=records.BY_BUTTON)
    conflicts = [(p.code, p.row.name, p.against) for p in plan.conflict]
    return replies.import_done(written, conflicts)


@router.message(Command("kodlar"))
async def cmd_code_suggestions(message: Message) -> None:
    """Code suggestions learned from chats — pulled here, never pushed."""
    async with session_scope() as session:
        rows = await client_codes.pending_suggestions(session, limit=10)
        lines = []
        for row in rows:
            source = (
                await session.get(Interaction, row.source_interaction_id)
                if row.source_interaction_id
                else None
            )
            excerpt = text_for_extraction(source)[:80] if source is not None else ""
            day = short_date(row.created_at.astimezone(settings.tz).date())
            lines.append(
                replies.code_suggestion_line(
                    row.code, row.person.display_name, day, excerpt
                )
            )
    if not rows:
        await _safe_answer(message, replies.KODLAR_EMPTY)
        return
    body = "\n".join([replies.KODLAR_HEADER, *lines])
    await _safe_answer(message, body, reply_markup=keyboards.code_suggestions(rows))


@router.callback_query(F.data.startswith("kod:"))
async def on_code_button(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "no":
        await _edit_callback(callback, replies.CODE_MOVE_DECLINED)
        return
    if action == "impno":
        await _edit_callback(callback, replies.IMPORT_CANCELLED)
        return
    if action == "imp" and len(parts) == 3 and parts[2].isdigit():
        async with session_scope() as session:
            body = await _apply_client_import(session, int(parts[2]))
        await _edit_callback(callback, body)
        return
    if action == "mv" and len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        async with session_scope() as session:
            body = await _move_code(session, int(parts[2]), int(parts[3]))
        await _edit_callback(callback, body)
        return
    if action in ("sy", "sn") and len(parts) == 3 and parts[2].isdigit():
        row_id = int(parts[2])
        async with session_scope() as session:
            body = await _answer_suggestion(session, row_id, accept=action == "sy")
        await _finish_row(callback, _trimmed(callback, "kod", row_id), body)
        return
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


async def _move_code(session, row_id: int, person_id: int) -> str:
    """Re-validated at press time: a row that is no longer the active one,
    or a person since forgotten, makes the question stale."""
    row = await session.get(ClientCode, row_id)
    target = await session.get(Person, person_id)
    if row is None or row.status != "active" or target is None:
        return replies.CODE_STALE
    if row.person_id == target.id:
        return replies.code_already(row.code, target.display_name)
    holder = await session.get(Person, row.person_id)
    await client_codes.move(session, row.code, target, by=records.BY_BUTTON)
    return replies.code_moved(row.code, target.display_name, holder.display_name)


async def _answer_suggestion(session, row_id: int, *, accept: bool) -> str:
    try:
        if accept:
            row = await client_codes.accept_suggestion(
                session, row_id, by=records.BY_BUTTON
            )
        else:
            row = await client_codes.reject_suggestion(
                session, row_id, by=records.BY_BUTTON
            )
    except client_codes.CodeTaken as exc:
        row = await session.get(ClientCode, row_id)
        wanted = await session.get(Person, row.person_id)
        return replies.code_taken(row.code, exc.holder.display_name, wanted.display_name)
    if row is None:
        return replies.CODE_STALE
    person = await session.get(Person, row.person_id)
    if accept:
        return replies.code_suggestion_accepted(row.code, person.display_name)
    return replies.code_suggestion_rejected(row.code, person.display_name)


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
