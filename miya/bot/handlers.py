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
from dataclasses import dataclass
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

from miya.bot import keyboards, recap_text, replies
from miya.bot.formatting import clip, escape, parse_ref, ref_of, short_date
from miya.bot.keyboards import FIELD_CODES, PAGE_SIZE, ChatsPage, chats_keyboard
from miya.config import settings
from miya.db.enums import (
    ChatType,
    DebtDirection,
    Direction,
    InteractionSource,
    TransactionType,
)
from miya.db.models import (
    ChatMonitor,
    ClientCode,
    Interaction,
    Person,
    QuestionLog,
    Receipt,
)
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
    loops,
    memories,
    money_events,
    nudges,
    people,
    persistence,
    planner,
    purge,
    queries,
    question_replies,
    questions,
    rag,
    recall,
    recaps,
    receipts,
    records,
    reminders,
    safety,
    vision,
)
from miya.services import codes as client_codes
from miya.services.embeddings import get_embedder
from miya.services.extraction import ExtractedTransaction
from miya.services.ingest import (
    apply_vision,
    create_interaction,
    describe_into,
    process_interaction,
    text_for_extraction,
    transcribe_into,
)
from miya.services.people import find_person
from miya.services.text import fold_apostrophes, normalise_for_search

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


async def _safe_answer(message: Message, text: str | None, *, reply_markup=None):
    """Reply after the data is durable; a failed send only costs the receipt.
    Returns the sent message, or None when nothing went out."""
    if not text:
        return None
    try:
        return await message.answer(text, reply_markup=reply_markup)
    except Exception:
        log.exception("could not send reply to owner (data is committed)")
    # Model-composed replies can contain broken HTML; a plain-text retry beats
    # the owner never seeing the answer at all.
    try:
        return await message.answer(text, parse_mode=None, reply_markup=reply_markup)
    except Exception:
        log.exception("plain-text retry failed too")
    return None


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
    keyboard = keyboards.with_money_splits(
        keyboard, [(iid, index) for _, iid, index in applied.matched_transactions]
    )
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


def _refusal(name: str | None) -> str:
    return replies.dangerous_file_refused(name, android=safety.is_android_package(name))


async def _refuse_dangerous(message: Message, media) -> bool:
    """JV-01: an installer or an archive is refused before `_download`, so it
    never reaches MEDIA_DIR. True when the owner was told so."""
    name = getattr(media, "file_name", None)
    verdict = safety.classify_attachment(name, getattr(media, "mime_type", None))
    if not verdict.dangerous:
        return False
    log.warning(
        "refused a dangerous file sent to the bot (kind=%s rule=%s)",
        verdict.kind,
        verdict.reason,
    )
    await _safe_answer(message, _refusal(name))
    return True


def _sniff_download(path: Path) -> safety.Verdict:
    """The bytes of a downloaded file, before any parser or transcriber sees
    them. An installer under a document's, a song's or a video's name is
    deleted on the spot."""
    verdict = safety.sniff_magic(path, path.suffix)
    if verdict.dangerous:
        path.unlink(missing_ok=True)
        log.warning(
            "deleted a disguised dangerous file (kind=%s rule=%s)",
            verdict.kind,
            verdict.reason,
        )
    return verdict


def _with_risk(media: dict, verdict: safety.Verdict | None, name: str | None) -> dict:
    if verdict is not None and verdict.kind == safety.MACRO:
        return {**media, "risk": verdict.as_risk(name)}
    return media


# --- images and receipts (JV-04) ----------------------------------------------


def _booked_money(applied) -> bool:
    """Did the owner's own words in this message already write money?"""
    return applied is not None and bool(
        applied.transactions
        or applied.matched_transactions
        or applied.settlements
        or applied.debts
    )


@dataclass(slots=True)
class _ReceiptCard:
    """The receipt card, its own message: the bank's verdict edits it in
    place later (JV-05), so it never shares a message with the reply."""

    receipt_id: int
    text: str
    keyboard: InlineKeyboardMarkup | None


async def _receipt_card(session, receipt: Receipt | None, applied) -> _ReceiptCard | None:
    """The card for an image the owner sent the bot, stamped as delivered:
    the owner gets it right here, never again from the worker. When the
    owner's own words in the message booked the payment, the card leaves
    out the "booked only from the bank" footer — on every later edit too."""
    if receipt is None:
        return None
    interaction = await session.get(Interaction, receipt.interaction_id)
    if interaction is not None:
        receipts.mark_card_alone(interaction)
        if _booked_money(applied):
            interaction.meta = {**(interaction.meta or {}), receipts.BOOKED_KEY: True}
    text = replies.receipt_card_for(
        receipt,
        await receipts.card_source(session, receipt, interaction),
        await receipts.verdict_detail(session, receipt),
    )
    keyboard = (
        keyboards.receipt_verdict(receipt.id)
        if receipts.owner_can_judge(receipt)
        else None
    )
    receipts.mark_notified(
        receipt, now=datetime.now(settings.tz), verdict=receipt.verdict
    )
    return _ReceiptCard(receipt.id, text, keyboard)


async def _read_image(
    session, interaction: Interaction, path: Path, *, readable: bool = True
) -> tuple[str, InlineKeyboardMarkup | None, _ReceiptCard | None]:
    """An image the owner sent the bot — a photo, or a .jpg/.png/.webp sent
    as a file — read once by vision; a receipt gets its card.

    ``readable`` is False when the file's bytes are not an image
    (safety.sniff_magic): the model never sees it, and the receipt the owner
    meant it to be waits in /tekshir. Returns (reply, keyboard, the card).
    """
    if readable:
        described = await describe_into(session, interaction, path, presume_receipt=True)
    else:
        described = await apply_vision(
            session,
            interaction,
            vision.VisionReading(
                description="", model=settings.extract_model, error="not an image"
            ),
            presume_receipt=True,
        )
    receipt = await receipts.for_interaction(session, interaction.id)
    keyboard = None
    applied = None
    if described is None and not interaction.raw_text:
        reply = "" if receipt is not None else replies.PHOTO_FAILED_HINT
    else:
        interaction.media = {**(interaction.media or {}), "processed": True}
        result = await process_interaction(session, interaction)
        reply, keyboard = _receipt(result)
        applied = result.applied if result.ok else None
        if result.ok and described is None:
            # The caption was extracted, but the image itself was not read —
            # the owner must not be told everything succeeded.
            reply = reply + "\n" + replies.VISION_PARTIAL_HINT
    card = await _receipt_card(session, receipt, applied)
    return reply, keyboard, card


async def _answer_image(
    message: Message,
    reply: str,
    keyboard: InlineKeyboardMarkup | None,
    card: _ReceiptCard | None,
) -> None:
    """The receipt card first, then the bot's reply with its own buttons."""
    if card is not None:
        sent = await _safe_answer(message, card.text, reply_markup=card.keyboard)
        await _stamp_receipt_card(card.receipt_id, sent)
    await _safe_answer(message, reply, reply_markup=keyboard)


async def _stamp_receipt_card(
    receipt_id: int | None, sent, *, verdict: str | None = None
) -> None:
    """Which message the card is, so the bank verdict can edit it in place
    later, and which verdict it now shows. Best-effort: the receipt itself is
    already committed."""
    message_id = getattr(sent, "message_id", None)
    if receipt_id is None or not isinstance(message_id, int):
        return
    try:
        async with session_scope() as session:
            await receipts.stamp_card(
                session,
                receipt_id,
                now=datetime.now(settings.tz),
                verdict=verdict,
                message_id=message_id,
            )
    except Exception as exc:
        log.error(
            "could not record the receipt card message for receipt %s: %s",
            receipt_id,
            type(exc).__name__,
        )


@router.callback_query(F.data.startswith(f"{keyboards.RECEIPT_PREFIX}:"))
async def on_receipt_button(callback: CallbackQuery) -> None:
    """✅ Pul keldi / 🚩 Soxta under a receipt card (JV-05).

    Records the owner's verdict under a row lock and edits the card in
    place. Neither books money nor voids any; the same tap twice changes
    nothing. Not in ``_button_handlers``: a typed batch answer never
    judges a receipt.
    """
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or parts[1] not in (keyboards.RECEIPT_OK, keyboards.RECEIPT_FAKE)
        or not parts[2].isdigit()
    ):
        await callback.answer()
        return
    paid = parts[1] == keyboards.RECEIPT_OK
    receipt_id = int(parts[2])
    text = verdict = None
    async with session_scope() as session:
        receipt, _ = await receipts.set_owner_verdict(
            session, receipt_id, paid=paid, by=receipts.BY_BUTTON
        )
        if receipt is not None:
            verdict = receipt.verdict
            text = replies.receipt_card_for(
                receipt,
                await receipts.card_source(session, receipt),
                await receipts.verdict_detail(session, receipt),
            )
    if callback.message is not None:
        edited = False
        try:
            if text is not None:
                await callback.message.edit_text(text, reply_markup=None)
            else:
                await callback.message.edit_reply_markup(reply_markup=None)
            edited = True
        except Exception as exc:
            edited = "not modified" in str(exc).lower()
            if not edited:
                log.info(
                    "could not edit receipt card %s: %s",
                    receipt_id,
                    type(exc).__name__,
                    extra={"receipt_id": receipt_id},
                )
        if edited and text is not None:
            # The card shows the owner's verdict; until it does, the
            # worker's card refresh keeps trying.
            await _stamp_receipt_card(receipt_id, callback.message, verdict=verdict)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


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
        maybe = await queries.messages_maybe_to_me(session)
        body = replies.to_me_report(addressed, titles, maybe)
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
        await _safe_answer(message, replies.QIDIR_USAGE)
        return

    await _typing(message)
    now = datetime.now(settings.tz)
    async with session_scope() as session:
        # Codes and waybills are matched exactly, never by embedding (WP-40);
        # the block survives the embedder being down.
        found = client_codes.find_waybills(query) + client_codes.find_client_codes(query)
        lines = []
        for code in found:
            lines += await client_codes.mentions(
                session, code, limit=replies.QIDIR_EXACT_MAX
            )
        lines.sort(key=lambda line: line.when, reverse=True)
        exact = replies.exact_hits_block(lines) if lines else ""
        # Hybrid recall (WP-59): the words stand even when meaning search
        # is down.
        try:
            embedder = get_embedder()
        except Exception:
            log.warning("embedder unavailable for /qidir", exc_info=True)
            embedder = None
        result = await recall.search(session, embedder, query, now=now, k=10)
        degraded = embedder is None or result.degraded is not None
        if result.is_empty() and exact:
            body = ""
        else:
            body = replies.qidir_results(result, query)
            if degraded:
                body = f"{body}\n\n{rag.SEMANTIC_DEGRADED}"
    await _safe_answer(message, clip("\n\n".join(p for p in (exact, body) if p)))


@router.message(Command("manba"))
async def cmd_source(message: Message, command: CommandObject) -> None:
    """`/manba m12` — the original words behind a citation (WP-59)."""
    ref = (command.args or "").strip()
    if not ref:
        await _safe_answer(message, replies.MANBA_USAGE)
        return
    async with session_scope() as session:
        source = await recall.source_of(session, ref)
    if source is None:
        await _safe_answer(message, replies.MANBA_NOT_FOUND)
        return
    await _safe_answer(message, replies.manba(source))


@router.message(Command("hisobot"))
async def cmd_report(message: Message) -> None:
    await _typing(message)
    now = datetime.now(settings.tz)
    async with session_scope() as session:
        # A look, not the evening report: stored nowhere (WP-49), and it
        # says until when (WP-53).
        result = await recaps.build_evening(session, now.date(), now=now, store=False)
    for part in result.parts:
        await _safe_answer(message, part)


@router.message(Command("kecha"))
async def cmd_yesterday(message: Message) -> None:
    """`/kecha` — yesterday in one look and everything since the evening."""
    now = datetime.now(settings.tz)
    async with session_scope() as session:
        morning = await recaps.build_morning(session, now=now, store=False)
    if not morning.parts:
        await _safe_answer(message, recap_text.KECHA_NONE)
        return
    for part in morning.parts:
        await _safe_answer(message, part)


@router.message(Command("ertalab"))
async def cmd_brief(message: Message) -> None:
    """The morning on demand: "🌙 Kecha" (whose prose may ask the model
    once, cached) and then the brief — SQL — with the same buttons."""
    now = datetime.now(settings.tz)
    async with session_scope() as session:
        morning = await recaps.build_morning(session, now=now, store=False)
    for part in morning.parts:
        await _safe_answer(message, part)
    async with session_scope() as session:
        data = await brief.gather(session)
        data.queue = questions.summarise(
            await questions.collect(session, for_push=False), []
        )
        parts = replies.morning_brief_parts(data)
        due, _ = replies.morning_brief_refs(data, parts)
        keyboard = keyboards.brief_actions(due)
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        await _safe_answer(message, part, reply_markup=keyboard if last else None)


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
async def cmd_questions(message: Message, command: CommandObject | None = None) -> None:
    """`/savollar` — every question waiting for the owner, ranked, paged.
    Answering here spends none of the day's budget. `/savollar hal` lists
    what MIYA settled by itself in the last week, each with its undo."""
    args = ((command.args if command else None) or "").strip().lower()
    async with session_scope() as session:
        if args == "hal":
            since = datetime.now(settings.tz) - timedelta(days=7)
            resolved = await questions.auto_resolved_since(session, since)
            ids = {i.person_id for i in resolved.media if i.person_id}
            people = {
                p.id: p
                for p in await session.scalars(
                    sa.select(Person).where(Person.id.in_(ids))
                )
            }
            body = replies.auto_resolved_report(resolved, people)
            keyboard = keyboards.auto_resolved_keyboard(
                [c.id for c in resolved.claims[: replies.AUTO_RESOLVED_MAX]],
                [m.id for m in resolved.groups],
            )
        else:
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


JARVIS_PAUSE_WORDS = {"toxtat", "to'xtat", "stop"}
JARVIS_RESUME_WORDS = {"yoq", "yoqish", "start"}
JARVIS_HISTORY_WORDS = {"tarix"}


@router.message(Command("jarvis"))
async def cmd_jarvis(message: Message, command: CommandObject) -> None:
    """`/jarvis` status · `toxtat` pauses everything · `yoq [kanal]` resumes ·
    `tarix` lists the last 20 actions (JV-11).

    miya.jarvis is imported here, on the owner's command, never at startup
    (rule 20). A pause is checked at claim time, so it holds the moment this
    commits; when the database does not answer, the owner is told that
    nothing changed rather than shown a pause that did not happen.
    """
    from miya.jarvis import chatkey as jarvis_chatkey
    from miya.jarvis import policy as jarvis_policy
    from miya.jarvis import status as jarvis_status

    words = (command.args or "").split()
    sub = fold_apostrophes(words[0]).casefold() if words else ""
    now = datetime.now(settings.tz)
    scope = jarvis_policy.GLOBAL
    if sub in JARVIS_RESUME_WORDS and len(words) > 1:
        channel = jarvis_chatkey.channel_word(words[1])
        if channel is None:
            await _safe_answer(message, replies.JARVIS_UNKNOWN_CHANNEL)
            return
        scope = jarvis_policy.channel_scope(channel)
    if sub and sub not in JARVIS_PAUSE_WORDS | JARVIS_RESUME_WORDS | JARVIS_HISTORY_WORDS:
        await _safe_answer(message, replies.JARVIS_USAGE)
        return
    try:
        async with session_scope() as session:
            if sub in JARVIS_PAUSE_WORDS:
                await jarvis_policy.pause(session, by=records.BY_COMMAND, now=now)
                body = replies.JARVIS_PAUSED
            elif sub in JARVIS_RESUME_WORDS:
                await jarvis_policy.resume(
                    session, scope=scope, by=records.BY_COMMAND, now=now
                )
                state = await jarvis_status.gather(session, now)
                # «yoqildi» only when something can send now: with Jarvis
                # off, or the global pause still holding after a channel
                # resume, the owner sees the status, which says so.
                body = (
                    replies.jarvis_resumed(state.mode)
                    if state.mode != "off" and not state.paused
                    else replies.jarvis_status(state)
                )
            elif sub in JARVIS_HISTORY_WORDS:
                body = replies.jarvis_history(
                    await jarvis_status.recent_actions(session, limit=20)
                )
            else:
                body = replies.jarvis_status(await jarvis_status.gather(session, now))
    except Exception as exc:
        log.error(
            "/jarvis failed",
            extra={"command": sub or "status", "error": type(exc).__name__},
        )
        body = replies.JARVIS_FAILED
    await _safe_answer(message, body)


# --- personal chats and group replies (JV-12) ---------------------------------------
#
# miya.jarvis is imported inside each handler, never at startup (rule 20).
# Restricting is immediate (/shaxsiy, a 🔒 tap, switching a group off);
# loosening is a confirmed tap (unmarking, switching a group on). The jr:
# buttons are not in _button_handlers, so a typed batch answer never changes
# a rule.


# The most chats named in one «which one?» answer.
CHATS_TITLED_MAX = 6


async def _chats_titled(
    session, title: str, *, kinds: tuple[ChatType, ...], exact_only: bool = False
) -> list[ChatMonitor]:
    """Chats of ``kinds`` titled exactly ``title``, else those whose title
    contains it, newest first; a bare chat id finds that chat. Titles are
    compared folded like a person's name (case, apostrophes, Cyrillic), so
    «Do‘stlar» and «Оила» are found as typed «Do'stlar» and «Oila»."""
    wanted = title.strip()
    if not wanted:
        return []
    base = sa.select(ChatMonitor).where(ChatMonitor.chat_type.in_(kinds))
    if wanted.lstrip("-").isdigit():
        found = await session.scalar(base.where(ChatMonitor.tg_chat_id == int(wanted)))
        if found is not None:
            return [found]
    folded = normalise_for_search(wanted)
    if not folded:
        return []
    # The monitors table is small: one row per chat the account is in.
    titled = [
        (normalise_for_search(monitor.title), monitor)
        for monitor in await session.scalars(
            base.where(ChatMonitor.title.isnot(None)).order_by(ChatMonitor.id)
        )
    ]
    exact = [monitor for name, monitor in titled if name == folded]
    if exact or exact_only:
        return exact
    contains = [monitor for name, monitor in titled if folded in name]
    contains.sort(
        key=lambda monitor: (
            monitor.last_seen_at is None,
            -(monitor.last_seen_at.timestamp() if monitor.last_seen_at else 0.0),
            monitor.id,
        )
    )
    return contains[:CHATS_TITLED_MAX]


async def _personal_command(session, name: str, *, now: datetime):
    """`/shaxsiy` on one name: mark at once, or ask before unmarking."""
    from miya.jarvis import chatkey as jarvis_chatkey
    from miya.jarvis import policy as jarvis_policy

    # A family group named exactly so is the chat; otherwise a person by
    # name or code (_lookup), then any chat whose title holds the words.
    groups = await _chats_titled(session, name, kinds=(ChatType.group,), exact_only=True)
    monitor = groups[0] if len(groups) == 1 else None
    person = None
    if monitor is None:
        person, body = await _lookup(session, name, command="shaxsiy")
        if person is None:
            if body != replies.person_not_found(name):
                return body, None
            found = await _chats_titled(
                session, name, kinds=(ChatType.private, ChatType.group)
            )
            if not found:
                return body, None
            if len(found) > 1:
                return (
                    replies.group_ambiguous(
                        [(c.title, c.tg_chat_id) for c in found], command="shaxsiy"
                    ),
                    None,
                )
            monitor = found[0]
    if person is not None:
        addresses = await people.known_addresses(session, person)
        rule = await jarvis_policy.personal_rule(
            session,
            person_id=person.id,
            chat_keys=[a for a in addresses if a.startswith("tg:")],
        )
        if rule is not None:
            return (
                replies.personal_unmark_question(person.display_name),
                keyboards.personal_unmark_confirm(rule.id),
            )
        await jarvis_policy.mark_person(session, person, by=records.BY_COMMAND, now=now)
        return replies.personal_marked(person.display_name), None
    key = jarvis_chatkey.telegram(monitor.tg_chat_id)
    rule = await jarvis_policy.personal_rule(session, chat_keys=[key])
    if rule is not None:
        return (
            replies.personal_unmark_question(monitor.title),
            keyboards.personal_unmark_confirm(rule.id),
        )
    await jarvis_policy.mark_chat(session, monitor, by=records.BY_COMMAND, now=now)
    return replies.personal_marked(monitor.title), None


@router.message(Command("shaxsiy"))
async def cmd_personal(message: Message, command: CommandObject) -> None:
    """`/shaxsiy <ism|kod>` — MIYA never writes into this person's or chat's
    conversation on its own (JV-12). Marked at once, with every address the
    person is known at; on a chat already personal it asks before unmarking."""
    name = (command.args or "").strip()
    if not name:
        await _safe_answer(message, replies.PERSONAL_USAGE)
        return
    keyboard = None
    try:
        async with session_scope() as session:
            body, keyboard = await _personal_command(
                session, name, now=datetime.now(settings.tz)
            )
    except Exception as exc:
        log.error(
            "/shaxsiy failed",
            extra={"command": "shaxsiy", "error": type(exc).__name__},
        )
        body, keyboard = replies.JARVIS_FAILED, None
    await _safe_answer(message, body, reply_markup=keyboard)


async def _personal_page():
    """The `/shaxsiylar` text and its buttons. The suggestions run in a
    savepoint: when they fail, the list still comes out without them."""
    from miya.jarvis import policy as jarvis_policy

    now = datetime.now(settings.tz)
    async with session_scope() as session:
        marked = await jarvis_policy.personal_chats(session)
        try:
            async with session.begin_nested():
                suggestions = await jarvis_policy.suggest_personal(session, now=now)
        except Exception as exc:
            log.error(
                "personal suggestions failed",
                extra={"marked": len(marked), "error": type(exc).__name__},
            )
            suggestions = []
    body = replies.personal_list(marked, suggestions)
    keyboard = keyboards.personal_list(
        [(item.rule_id, item.name) for item in marked],
        [(item.person_id, item.name) for item in suggestions],
    )
    return body, keyboard


@router.message(Command("shaxsiylar"))
async def cmd_personal_list(message: Message) -> None:
    """`/shaxsiylar` — the personal chats, and likely family and friends MIYA
    found from the database, each one tap to mark (nothing is marked
    without the owner)."""
    try:
        body, keyboard = await _personal_page()
    except Exception as exc:
        log.error(
            "/shaxsiylar failed",
            extra={"command": "shaxsiylar", "error": type(exc).__name__},
        )
        body, keyboard = replies.JARVIS_FAILED, None
    await _safe_answer(message, body, reply_markup=keyboard)


@router.message(Command("guruhai"))
async def cmd_group_reply(message: Message, command: CommandObject) -> None:
    """`/guruhai <guruh>` — AI replies in one group: off at once, on after a
    confirm tap, and then only to messages addressed to the owner (JV-12)."""
    from miya.jarvis import chatkey as jarvis_chatkey
    from miya.jarvis import policy as jarvis_policy

    title = (command.args or "").strip()
    keyboard = None
    monitor_id = None
    try:
        async with session_scope() as session:
            if not title:
                body = replies.group_reply_usage(
                    await jarvis_policy.enabled_group_titles(session)
                )
            else:
                found = await _chats_titled(session, title, kinds=(ChatType.group,))
                if not found:
                    body = replies.group_not_found(title)
                elif len(found) > 1:
                    body = replies.group_ambiguous(
                        [(c.title, c.tg_chat_id) for c in found], command="guruhai"
                    )
                else:
                    monitor = found[0]
                    monitor_id = monitor.id
                    key = jarvis_chatkey.telegram(monitor.tg_chat_id)
                    state = await jarvis_policy.group_reply_state(session, key)
                    if state == "personal":
                        body = replies.group_reply_personal(monitor.title)
                    elif state == "on":
                        await jarvis_policy.set_group_reply(
                            session,
                            chat_key=key,
                            enabled=False,
                            by=records.BY_COMMAND,
                            now=datetime.now(settings.tz),
                        )
                        body = replies.group_reply_off(monitor.title)
                    else:
                        body = replies.group_reply_question(monitor.title)
                        keyboard = keyboards.group_reply_confirm(monitor.id)
    except Exception as exc:
        log.error(
            "/guruhai failed",
            extra={
                "command": "guruhai",
                "monitor_id": monitor_id,
                "error": type(exc).__name__,
            },
        )
        body, keyboard = replies.JARVIS_FAILED, None
    await _safe_answer(message, body, reply_markup=keyboard)


async def _rule_tap(action: str, ident: int, *, now: datetime):
    """One jr: tap: (text, keyboard, edit the tapped message in place)."""
    from miya.jarvis import chatkey as jarvis_chatkey
    from miya.jarvis import policy as jarvis_policy

    async with session_scope() as session:
        if action == keyboards.RULE_MARK:
            person = await session.get(Person, ident)
            if person is None:
                return replies.RULE_STALE, None, False
            await jarvis_policy.mark_person(
                session, person, by=records.BY_BUTTON, now=now
            )
            return replies.personal_marked(person.display_name), None, False
        if action == keyboards.RULE_ASK:
            rule = await jarvis_policy.personal_rule_by_id(session, ident)
            if rule is None:
                return replies.RULE_STALE, None, False
            [listed] = await jarvis_policy.describe(session, [rule])
            return (
                replies.personal_unmark_question(listed.name),
                keyboards.personal_unmark_confirm(rule.id),
                False,
            )
        if action == keyboards.RULE_YES:
            rule = await jarvis_policy.unmark_personal(
                session, ident, by=records.BY_BUTTON, now=now
            )
            if rule is None:
                return replies.RULE_STALE, None, True
            [listed] = await jarvis_policy.describe(session, [rule])
            return replies.personal_unmarked(listed.name), None, True
        # keyboards.RULE_GROUP_YES
        monitor = await session.get(ChatMonitor, ident)
        if monitor is None or monitor.chat_type != ChatType.group:
            return replies.RULE_STALE, None, True
        state = await jarvis_policy.set_group_reply(
            session,
            chat_key=jarvis_chatkey.telegram(monitor.tg_chat_id),
            enabled=True,
            by=records.BY_BUTTON,
            now=now,
        )
        if state == "personal":
            return replies.group_reply_personal(monitor.title), None, True
        return replies.group_reply_on(monitor.title), None, True


_RULE_ACTIONS = {
    keyboards.RULE_MARK,
    keyboards.RULE_ASK,
    keyboards.RULE_YES,
    keyboards.RULE_NO,
    keyboards.RULE_GROUP_YES,
}


@router.callback_query(F.data.startswith(f"{keyboards.RULE_PREFIX}:"))
async def on_rule_button(callback: CallbackQuery) -> None:
    """🔒 / 🔓 under `/shaxsiylar`, and the confirms of unmarking and of
    switching a group on (JV-12). Every change runs under a row lock and is
    re-checked; a second tap changes nothing. Not in ``_button_handlers``.
    """
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    ident = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
    if action not in _RULE_ACTIONS or ident is None:
        await _ack(callback)
        return
    if action == keyboards.RULE_NO:
        await _edit_callback(callback, replies.RULE_KEPT)
        return
    try:
        body, keyboard, in_place = await _rule_tap(
            action, ident, now=datetime.now(settings.tz)
        )
    except Exception as exc:
        log.error(
            "reply rule tap failed",
            extra={"rule_action": action, "ref_id": ident, "error": type(exc).__name__},
        )
        if callback.message is not None:
            await _safe_answer(callback.message, replies.JARVIS_FAILED)
        await _ack(callback)
        return
    if in_place:
        await _edit_callback(callback, body)
        return
    if callback.message is not None:
        await _safe_answer(callback.message, body, reply_markup=keyboard)
        if action == keyboards.RULE_MARK and body != replies.RULE_STALE:
            await _refresh_personal_page(callback)
    await _ack(callback)


async def _refresh_personal_page(callback: CallbackQuery) -> None:
    """The tapped list shows the person among the personal chats now."""
    try:
        body, keyboard = await _personal_page()
        await callback.message.edit_text(body, reply_markup=keyboard)
    except Exception as exc:
        # Cosmetic: the mark is committed; the next /shaxsiylar shows it.
        log.info(
            "could not refresh the personal list", extra={"error": type(exc).__name__}
        )


async def _ack(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


# --- the owner's style (JV-14) --------------------------------------------------------
#
# miya.jarvis is imported inside each handler, never at startup (rule 20).
# The js: buttons are not in _button_handlers, so a typed batch answer never
# changes the style the AI writes in.

STYLE_IMPORT_WORDS = {"yukla", "yuklash", "import"}


async def _style_page(session, *, now: datetime, force: bool = False):
    """The `/uslub` card and its buttons: the newest draft (or the active
    profile); with none yet, or with ``force``, one is built now."""
    from miya.jarvis import style as jarvis_style

    profile = None if force else await jarvis_style.current_profile(session)
    if profile is None:
        result = await jarvis_style.build_profile(
            session, now=now, by=records.BY_COMMAND, force=force
        )
        if result.status == "too_few" or result.profile is None:
            return (
                replies.style_too_few(result.sample_count, jarvis_style.min_samples()),
                None,
            )
        profile = result.profile
    card = await jarvis_style.card(session, profile)
    return (
        replies.style_card(card),
        keyboards.style_card(profile.version, active=profile.state == "active"),
    )


@router.message(Command("uslub"))
async def cmd_style(message: Message, command: CommandObject) -> None:
    """`/uslub` — the owner's texting style as MIYA learned it, with
    ✅ Faollashtirish / 🔄 Qayta tuzish · `/uslub yukla` — read the older
    private chats (read-only) to learn from more of the owner's messages."""
    words = (command.args or "").split()
    sub = fold_apostrophes(words[0]).casefold() if words else ""
    if sub and sub not in STYLE_IMPORT_WORDS:
        await _safe_answer(message, replies.STYLE_USAGE)
        return
    now = datetime.now(settings.tz)
    keyboard = None
    try:
        async with session_scope() as session:
            if sub:
                asked = await chats.request_style_import(session, now=now)
                if not asked:
                    body = replies.STYLE_IMPORT_NO_CHATS
                elif settings.jarvis_enabled:
                    # style_refresh says «✅ Uslub tayyor» once it is read.
                    body = replies.STYLE_IMPORT_STARTED
                else:
                    body = replies.STYLE_IMPORT_STARTED_MANUAL
            else:
                body, keyboard = await _style_page(session, now=now)
    except Exception as exc:
        log.error(
            "/uslub failed",
            extra={"command": sub or "show", "error": type(exc).__name__},
        )
        body, keyboard = replies.JARVIS_FAILED, None
    await _safe_answer(message, body, reply_markup=keyboard)


async def _style_tap(action: str, version: int, *, now: datetime):
    """One js: tap: (text, keyboard)."""
    from miya.jarvis import style as jarvis_style

    async with session_scope() as session:
        if action == keyboards.STYLE_ACTIVATE:
            profile = await jarvis_style.activate(
                session, version, by=records.BY_BUTTON, now=now
            )
            if profile is None:
                return replies.STYLE_STALE, None
            return replies.style_activated(profile.version), None
        return await _style_page(session, now=now, force=True)


@router.callback_query(F.data.startswith(f"{keyboards.STYLE_PREFIX}:"))
async def on_style_button(callback: CallbackQuery) -> None:
    """✅ Faollashtirish / 🔄 Qayta tuzish under `/uslub` (JV-14). The
    activation runs under a row lock; a tap on a retired version changes
    nothing. Not in ``_button_handlers``."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    version = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
    if (
        action not in (keyboards.STYLE_ACTIVATE, keyboards.STYLE_REBUILD)
        or version is None
    ):
        await _ack(callback)
        return
    try:
        body, keyboard = await _style_tap(action, version, now=datetime.now(settings.tz))
    except Exception as exc:
        log.error(
            "style tap failed",
            extra={
                "style_action": action,
                "version": version,
                "error": type(exc).__name__,
            },
        )
        body, keyboard = replies.JARVIS_FAILED, None
    if callback.message is not None:
        await _safe_answer(callback.message, body, reply_markup=keyboard)
    await _ack(callback)


# --- the knowledge base (JV-15) -------------------------------------------------------
#
# miya.jarvis is imported inside each handler, never at startup (rule 20).
# The kb: buttons are not in _button_handlers, so a typed batch answer never
# adds a text the AI may send on its own.

KB_ADD_WORDS = {"qosh", "qoshish", "qoshing", "add"}
KB_EDIT_WORDS = {"tuzat", "tuzatish", "edit"}
KB_RETIRE_WORDS = {"ochir", "ochirish", "uchir", "delete"}
KB_SUGGEST_WORDS = {"taklif", "takliflar"}
# The most entries `/bilim` lists, and suggestion cards sent at once.
KB_LIST_MAX = 50
KB_SUGGESTIONS_SHOWN = 5


def _kb_word(word: str) -> str:
    return fold_apostrophes(word).casefold().replace("'", "")


def _first_word(text: str) -> tuple[str, str]:
    """(the first word, the rest), split on any whitespace: the owner may
    put the rest on the next line."""
    parts = text.split(maxsplit=1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _kb_outcome(result) -> str:
    """The text for a kb.WriteResult: added, or why not."""
    if result.refusal is not None:
        refusal = result.refusal
        return replies.kb_refused(refusal.reason, refusal.labels, refusal.limit)
    return replies.kb_added(result.entry)


async def _kb_command(session, sub: str, rest: str, *, now: datetime):
    """One `/bilim` subcommand: [(text, keyboard)], one message each."""
    from miya.jarvis import kb

    if not sub:
        active = await kb.count(session, kb.ACTIVE)
        entries = await kb.listing(session, kb.ACTIVE, limit=KB_LIST_MAX)
        return [(replies.kb_list(entries, active), None)]
    if sub in KB_ADD_WORDS:
        question, answer = kb.split_entry(rest)
        if not answer:
            return [(replies.KB_USAGE, None)]
        result = await kb.add(
            session, question=question, answer=answer, by=records.BY_COMMAND, now=now
        )
        return [(_kb_outcome(result), None)]
    if sub in KB_EDIT_WORDS:
        handle, body = _first_word(rest)
        entry_id = kb.parse_ref(handle)
        question, answer = kb.split_entry(body)
        if entry_id is None or not answer:
            return [(replies.KB_USAGE, None)]
        edited = await kb.edit(
            session,
            entry_id,
            question=question,
            answer=answer,
            by=records.BY_COMMAND,
            now=now,
        )
        if edited is None:
            return [(replies.kb_not_found(entry_id), None)]
        if edited.refusal is not None:
            return [(_kb_outcome(edited), None)]
        return [(replies.kb_edited(kb.view(edited.entry)), None)]
    if sub in KB_RETIRE_WORDS:
        entry_id = kb.parse_ref(rest)
        if entry_id is None:
            return [(replies.KB_USAGE, None)]
        retired = await kb.retire(session, entry_id, by=records.BY_COMMAND, now=now)
        if retired is None:
            return [(replies.kb_not_found(entry_id), None)]
        return [(replies.kb_retired(entry_id), None)]
    if sub in KB_SUGGEST_WORDS:
        waiting = await kb.listing(session, kb.SUGGESTED, limit=KB_SUGGESTIONS_SHOWN)
        if not waiting:
            return [(replies.KB_NO_SUGGESTIONS, None)]
        cards = [
            (replies.kb_suggestion(entry), keyboards.kb_suggestion(entry.id))
            for entry in waiting
        ]
        more = await kb.count(session, kb.SUGGESTED) - len(waiting)
        if more > 0:
            cards.append((replies.KB_MORE.format(n=more), None))
        return cards
    return [(replies.KB_USAGE, None)]


@router.message(Command("bilim"))
async def cmd_knowledge(message: Message, command: CommandObject) -> None:
    """`/bilim` — the business knowledge base: the facts and answers the
    owner approved, the only statements Jarvis may make on its own.
    `/bilim qo'sh Savol | Javob` adds one (a price, discount, payment term,
    deadline, promise or a client's personal data is refused), `/bilim tuzat
    b12 Savol | Javob` corrects one, `/bilim o'chir b12` retires one, and
    `/bilim taklif` shows what MIYA suggests from the owner's own answers."""
    first, rest = _first_word(command.args or "")
    sub = _kb_word(first) if first else ""
    now = datetime.now(settings.tz)
    try:
        async with session_scope() as session:
            outgoing = await _kb_command(session, sub, rest.strip(), now=now)
    except Exception as exc:
        log.error(
            "/bilim failed",
            extra={"command": sub or "list", "error": type(exc).__name__},
        )
        outgoing = [(replies.JARVIS_FAILED, None)]
    for body, keyboard in outgoing:
        await _safe_answer(message, body, reply_markup=keyboard)


async def _kb_tap(action: str, entry_id: int, *, now: datetime) -> str:
    """One kb: tap: the card's new text."""
    from miya.jarvis import kb

    async with session_scope() as session:
        if action == keyboards.KB_ADD:
            result = await kb.approve_suggestion(
                session, entry_id, by=records.BY_BUTTON, now=now
            )
            if result is None:
                return replies.KB_STALE
            return _kb_outcome(result)
        dismissed = await kb.dismiss_suggestion(
            session, entry_id, by=records.BY_BUTTON, now=now
        )
        if dismissed is None:
            return replies.KB_STALE
        return replies.kb_dismissed(entry_id)


@router.callback_query(F.data.startswith(f"{keyboards.KB_PREFIX}:"))
async def on_knowledge_button(callback: CallbackQuery) -> None:
    """✅ Qo'shish / ✖️ Kerak emas under a knowledge-base suggestion
    (JV-15): the owner's one tap is the only way a mined entry becomes
    active. Under a row lock and re-checked; a second tap changes nothing.
    Not in ``_button_handlers``."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    entry_id = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
    if action not in (keyboards.KB_ADD, keyboards.KB_DISMISS) or entry_id is None:
        await _ack(callback)
        return
    try:
        body = await _kb_tap(action, entry_id, now=datetime.now(settings.tz))
    except Exception as exc:
        log.error(
            "knowledge tap failed",
            extra={
                "kb_action": action,
                "kb_entry_id": entry_id,
                "error": type(exc).__name__,
            },
        )
        await _ack(callback)
        if callback.message is not None:
            await _safe_answer(callback.message, replies.JARVIS_FAILED)
        return
    if callback.message is not None:
        await _edit_callback(callback, body)
    else:
        await _ack(callback)


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
        repeats = await claims.duplicate_counts(session, [c.id for c in shown])
        await claims.load_evidence(session, shown)
        views = [claims.view(c) for c in shown]
        for view in views:
            view.repeats = repeats.get(view.id, 0)
        body = replies.claims_list(views, hidden=len(pending) - len(shown))
        keyboard = keyboards.claim_actions([c.id for c in shown])
        _ask(shown, keyboard)
    await _safe_answer(message, body, reply_markup=keyboard)


async def _answer_claim(
    session, action: str, claim_id: int, extra: int | None = None
) -> _Outcome | None:
    """Ha / Yo'q / Tuzat on one claim. None for an action that is not one."""
    try:
        if action == keyboards.ACTION_CLAIM_YES:
            claim = await claims.get(session, claim_id)
            view = claims.view(claim) if claim is not None else None
            if (
                view is not None
                and view.state == claims.PENDING
                and (view.origin == "ambiguous")
            ):
                # MIYA's own question needs its own answer, not a plain Ha.
                if view.kind == claims.KIND_SETTLEMENT:
                    keyboard = keyboards.claim_direction(claim_id)
                elif view.candidates:
                    keyboard = keyboards.claim_candidates(claim_id, view.candidates)
                else:
                    keyboard = None
                if keyboard is not None:
                    return _Outcome(replies.claim_question(view), False, keyboard)
        if action in (keyboards.ACTION_CLAIM_THEY_PAID, keyboards.ACTION_CLAIM_I_PAID):
            direction = (
                DebtDirection.they_owe_me
                if action == keyboards.ACTION_CLAIM_THEY_PAID
                else DebtDirection.i_owe_them
            )
            accepted = await claims.accept_with_direction(
                session, claim_id, direction, by=claims.BY_BUTTON
            )
            if accepted is None:
                return _Outcome(replies.CLAIM_GONE, True)
            keyboard = keyboards.applied_actions(
                replies.confirmation_refs(accepted.applied), []
            )
            return _Outcome(replies.claim_accepted(accepted), accepted.written, keyboard)
        if action == keyboards.ACTION_CLAIM_PROMISE and extra is not None:
            try:
                chosen = await claims.choose_promise(
                    session, claim_id, extra, by=claims.BY_BUTTON
                )
            except claims.NotACandidate:
                return _Outcome(replies.CLAIM_GONE, True)
            if chosen is None:
                return _Outcome(replies.CLAIM_GONE, True)
            _, promise = chosen
            return _Outcome(replies.claim_promise_kept(promise), True)
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
        if action == keyboards.ACTION_CLAIM_UNDO:
            claim = await claims.reopen_auto(session, claim_id, by=claims.BY_BUTTON)
            if claim is None:
                return _Outcome(replies.CLAIM_GONE, True)
            return _Outcome(
                replies.claim_reopened(claims.view(claim)),
                True,
                keyboards.claim_actions([claim.id]),
            )
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
    if len(parts) not in (3, 4) or not all(p.isdigit() for p in parts[2:]):
        await callback.answer()
        return
    action, claim_id = parts[1], int(parts[2])
    extra = int(parts[3]) if len(parts) == 4 else None

    async with session_scope() as session:
        outcome = await _answer_claim(session, action, claim_id, extra)
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
            held = await client_codes.codes_of(session, person.id)
            body = replies.history_report(person, entries, requested=count, codes=held)
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
    if await _refuse_dangerous(message, media):
        return
    if target.document and vision.is_heic(
        getattr(media, "file_name", None), getattr(media, "mime_type", None)
    ):
        await _safe_answer(message, replies.RECEIPT_HEIC_HINT)
        return

    await _typing(message)
    is_video = bool(target.video or target.video_note)
    is_audio = bool(target.voice or target.audio)
    # An image sent as a file goes to vision like a photo (JV-04).
    is_photo = bool(target.photo) or bool(
        target.document and vision.is_image_document(getattr(media, "file_name", None))
    )
    suffix = Path(getattr(media, "file_name", "") or "").suffix
    if not suffix:
        suffix = (
            ".mp4" if is_video else ".jpg" if is_photo else ".ogg" if is_audio else ".bin"
        )

    path = _media_path(suffix)
    if not await _download(bot, message, media, path):
        return
    name = getattr(media, "file_name", None)
    sniffed: safety.Verdict | None = _sniff_download(path)
    if sniffed.dangerous:
        await _safe_answer(message, _refusal(name))
        return
    image_readable = sniffed.readable
    if is_video or is_audio or is_photo:
        sniffed = None  # only a document's bytes decide whether it is parsed

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=target.caption,
            occurred_at=target.date.astimezone(settings.tz),
            media=_with_risk(
                {
                    "type": "video" if is_video else "photo" if is_photo else "file",
                    "path": str(path),
                    "filename": name,
                    "caption": target.caption,
                    "processed": False,
                },
                sniffed,
                name,
            ),
            meta={"tg_message_id": target.message_id, "on_demand": True},
        )
        reply, keyboard, card = await _process_on_demand(
            session,
            interaction,
            path,
            is_video=is_video,
            is_audio=is_audio,
            is_photo=is_photo,
            sniffed=sniffed,
            image_readable=image_readable,
        )
    await _answer_image(message, reply, keyboard, card)


async def _process_on_demand(
    session,
    interaction,
    path: Path,
    *,
    is_video: bool,
    is_audio: bool,
    is_photo: bool,
    sniffed: safety.Verdict | None = None,
    image_readable: bool = True,
) -> tuple[str, InlineKeyboardMarkup | None, _ReceiptCard | None]:
    """Run the right pipeline for one explicitly requested media file.

    A document is parsed only when its bytes matched its name (`sniffed`);
    a macro document is read as text and the owner told so (JV-01). An
    image, photo or file, is read by vision and a receipt gets its card
    (JV-04). Returns (reply, keyboard, the receipt card or None)."""
    macro_read = False
    if is_video:
        audio_path = path.with_suffix(".mp3")
        if not await audio.extract_audio(path, audio_path):
            interaction.needs_review = True
            return replies.TRANSCRIPTION_FAILED_HINT, None, None
        interaction.media = {**(interaction.media or {}), "audio_path": str(audio_path)}
        if await transcribe_into(session, interaction, audio_path) is None:
            return replies.TRANSCRIPTION_FAILED_HINT, None, None
    elif is_audio:
        if await transcribe_into(session, interaction, path) is None:
            return replies.TRANSCRIPTION_FAILED_HINT, None, None
    elif is_photo:
        return await _read_image(session, interaction, path, readable=image_readable)
    else:
        readable = sniffed is None or sniffed.readable
        parsed = await documents.read_document_async(path) if readable else None
        if parsed is None and not interaction.raw_text:
            interaction.needs_review = True
            return replies.DOCUMENT_FAILED_HINT, None, None
        if parsed is not None:
            interaction.transcript = parsed.text
            macro_read = sniffed is not None and sniffed.kind == safety.MACRO

    interaction.media = {**(interaction.media or {}), "processed": True}
    result = await process_interaction(session, interaction)
    reply, keyboard = _receipt(result)
    if macro_read:
        name = (interaction.media or {}).get("filename")
        reply = reply + "\n" + replies.macro_document_note(name)
    return reply, keyboard, None


# --- client codes by hand (WP-33) ---------------------------------------------

_DETACH_WORDS = {"o'chir", "ochir", "olib tashla", "-"}


def _code_args(args: str) -> tuple[list[str], str]:
    """(the codes in the arguments, what is left once they are taken out)."""
    found = client_codes.find_client_codes(args)
    rest = client_codes.client_code_re().sub(" ", args)
    return found, " ".join(rest.split())


_MERGE_SPLIT = re.compile(r"\s*(?:>|→|->)\s*")


async def _merge_side(session, name: str):
    """(person, None) or (None, reply) for one side of /birlashtir."""
    match = await find_person(session, name)
    if match.person is None:
        if match.unknown_code:
            return None, replies.code_unknown(match.unknown_code)
        return None, replies.person_not_found(name)
    if match.ambiguous:
        held = await client_codes.codes_of_many(
            session, [p.id for p in (match.person, match.runner_up) if p is not None]
        )
        return None, replies.person_ambiguous(match, command="birlashtir", codes=held)
    return match.person, None


@router.message(Command("birlashtir"))
async def cmd_merge(message: Message, command: CommandObject) -> None:
    """`/birlashtir Акмал > Akmal` — the first joins the second (WP-77)."""
    sides = _MERGE_SPLIT.split((command.args or "").strip(), maxsplit=1)
    if len(sides) != 2 or not all(side.strip() for side in sides):
        await _safe_answer(message, replies.MERGE_USAGE)
        return
    keyboard = None
    async with session_scope() as session:
        source, body = await _merge_side(session, sides[0].strip())
        if source is not None:
            target, body = await _merge_side(session, sides[1].strip())
            if target is not None:
                if source.id == target.id:
                    body = replies.MERGE_SAME
                elif (
                    source.telegram_id
                    and target.telegram_id
                    and source.telegram_id != target.telegram_id
                ):
                    body = replies.MERGE_REFUSED
                else:
                    counts = await people.merge_plan(session, source, target)
                    body = replies.merge_preview(source, target, counts)
                    keyboard = keyboards.merge_confirm(source.id, target.id)
    await _safe_answer(message, body, reply_markup=keyboard)


@router.callback_query(F.data.startswith("birl:"))
async def on_merge_button(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await _edit_callback(callback, replies.MERGE_CANCELLED)
        return
    async with session_scope() as session:
        source = await session.get(Person, int(parts[1]))
        target = await session.get(Person, int(parts[2]))
        if source is None or target is None or source.id == target.id:
            body = replies.MERGE_STALE
        else:
            try:
                merged = await people.merge_into(
                    session, source, target, by=records.BY_BUTTON
                )
                body = replies.merge_done(merged)
            except people.MergeRefused:
                body = replies.MERGE_REFUSED
    await _edit_callback(callback, body)


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
    if holder is not None and people.is_placeholder(holder, code):
        # The owner names the placeholder's client (WP-77): folded in, or
        # renamed when the name is new. The owner's assertion; nothing asked.
        match = await find_person(session, name)
        if match.person is not None and match.person.id != holder.id:
            if match.ambiguous:
                held = await client_codes.codes_of_many(
                    session,
                    [p.id for p in (match.person, match.runner_up) if p is not None],
                )
                return replies.person_ambiguous(match, command="kod", codes=held), None
            try:
                target = await people.merge_into(
                    session, holder, match.person, by=records.BY_COMMAND
                )
            except people.MergeRefused:
                return replies.MERGE_REFUSED, None
            return replies.placeholder_merged(code, target.display_name), None
        holder.display_name = name.strip()
        return replies.placeholder_merged(code, holder.display_name), None
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


_SPOKEN_ASK = re.compile(r"(?<![a-z0-9])(?:nima|kim)(?![a-z0-9])")


def _spoken_mode(text: str) -> str:
    """A transcript has no '?': the "-mi" particle marks the question too,
    and so does "nima"/"kim" with a recalling verb ("nima bo'lgandi"). The
    two questions SQL answers alone (JV-06) are questions however spoken."""
    if rag.owner_question(text, datetime.now(settings.tz)) is not None:
        return "question"
    mode = rag.classify(text)
    if mode != "note":
        return mode
    if loops.ends_in_question_particle(fold_apostrophes(text)):
        return "question"
    if _SPOKEN_ASK.search(normalise_for_search(text)) and rag.wants_evidence(text):
        return "question"
    return mode


def _turn(row: Interaction) -> rag.HistoryTurn:
    meta = row.meta or {}
    return rag.HistoryTurn(
        question=row.raw_text or row.transcript or "",
        answer=str(meta.get("answer") or ""),
        refs=[str(r) for r in meta.get("refs") or []],
    )


async def _followup_history(session, message) -> tuple[tuple, bool]:
    """(history, replied): the answer the owner replied to, or the latest
    questions within RAG_FOLLOWUP_MINUTES (WP-75)."""
    answered = sa.and_(
        Interaction.meta["kind"].astext == "question",
        Interaction.meta.has_key("answer"),
    )
    reply = getattr(message, "reply_to_message", None)
    reply_id = getattr(reply, "message_id", None) if reply is not None else None
    if isinstance(reply_id, int):
        row = await session.scalar(
            sa.select(Interaction)
            .where(
                answered,
                Interaction.meta["answer_message_id"].astext == str(reply_id),
            )
            .order_by(Interaction.id.desc())
            .limit(1)
        )
        if row is not None:
            return (_turn(row),), True
    minutes = settings.rag_followup_minutes
    pairs = settings.rag_followup_pairs
    if minutes <= 0 or pairs <= 0:
        return (), False
    at = getattr(message, "date", None)
    now = at.astimezone(settings.tz) if at is not None else datetime.now(settings.tz)
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(
                answered,
                Interaction.occurred_at >= now - timedelta(minutes=minutes),
                Interaction.occurred_at <= now,
            )
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
            .limit(pairs)
        )
    )
    return tuple(_turn(r) for r in reversed(rows)), False


async def _sql_answer(
    session, text: str, now: datetime, *, interaction_id: int | None = None
) -> tuple[str, str] | None:
    """(kind, reply) for «Bugun kimga nima va'da bergandim?» and «Kecha kimlar
    yozdi?», rendered from SQL with no model call (JV-06). None for any other
    text, and when the query fails: the model path answers it then."""
    asked = rag.owner_question(text, now)
    if asked is None:
        return None
    window = (asked.start, asked.end)
    try:
        async with session.begin_nested():
            if asked.kind == rag.OWNER_PROMISES:
                made, total = await queries.promises_made(session, window)
                reply = replies.promises_made_report(
                    made,
                    day=asked.day,
                    days_ago=asked.days_ago,
                    today=now.astimezone(settings.tz).date(),
                    total=total,
                )
            else:
                wrote, total = await queries.who_wrote(session, window)
                reply = replies.who_wrote_report(
                    wrote, day=asked.day, days_ago=asked.days_ago, total=total
                )
    except Exception as exc:
        log.warning(
            "sql answer failed: %s",
            type(exc).__name__,
            extra={
                "error": type(exc).__name__,
                "kind": asked.kind,
                "interaction_id": interaction_id,
            },
        )
        return None
    return asked.kind, reply


async def _answer_into(
    session,
    interaction: Interaction,
    text: str,
    mode: str,
    *,
    header: str | None = None,
    history: tuple = (),
) -> str:
    """Answer ``text`` and keep the answer and what it cited on the row."""
    interaction.processed = True
    fast = await _sql_answer(
        session, text, interaction.occurred_at, interaction_id=interaction.id
    )
    if fast is not None:
        kind, body = fast
        reply = clip(f"{header}\n\n{body}" if header else body)
        interaction.meta = {
            **(interaction.meta or {}),
            "mode": "question",
            "sql": kind,
            "answer": reply[:4000],
            "refs": [],
        }
        return reply
    result = await rag.answer_full(session, text, mode=mode, history=history)
    reply = clip(f"{header}\n\n{result.text}" if header else result.text)
    interaction.meta = {
        **(interaction.meta or {}),
        "mode": result.mode,
        "answer": reply[:4000],
        "refs": result.refs,
    }
    return reply


async def _send_answer(message: Message, reply: str, interaction_id: int) -> None:
    sent = await _safe_answer(
        message, reply, reply_markup=keyboards.save_as_note(interaction_id)
    )
    if sent is not None and getattr(sent, "message_id", None) is not None:
        async with session_scope() as session:
            row = await session.get(Interaction, interaction_id)
            if row is not None:
                row.meta = {**(row.meta or {}), "answer_message_id": sent.message_id}


async def _answer_question(
    message: Message, text: str, mode: str, *, history: tuple | None = None
) -> None:
    """A question or an opinion: answered, not extracted — but it still lands
    in interactions ("every input lands here"), marked so /tekshir and the
    extractor both leave it alone, with the answer and what it cited."""
    async with session_scope() as session:
        if history is None:
            history, _ = await _followup_history(session, message)
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=text,
            occurred_at=message.date.astimezone(settings.tz),
            meta={"kind": "question"},
        )
        reply = await _answer_into(session, interaction, text, mode, history=history)
        interaction_id = interaction.id
    await _send_answer(message, reply, interaction_id)


@router.message(Command("savol"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    """`/savol …` — answered as a question, never written down as a note."""
    text = (command.args or "").strip()
    if not text:
        await _safe_answer(message, replies.SAVOL_USAGE)
        return
    await _answer_question(message, text, "question")


@router.message(Command("fikr"))
async def cmd_opinion(message: Message, command: CommandObject) -> None:
    """`/fikr …` — what the records say about it, then MIYA's own view."""
    text = (command.args or "").strip()
    if not text:
        await _safe_answer(message, replies.FIKR_USAGE)
        return
    await _answer_question(message, text, "opinion")


@router.callback_query(F.data.startswith("vq:n:"))
async def on_save_as_note(callback: CallbackQuery) -> None:
    """📝 Eslatma sifatida yozib qo'y: the owner meant it as a note after all.
    The question row itself becomes the note, once (WP-60)."""
    raw = (callback.data or "").split(":")[-1]
    body = replies.QUESTION_GONE
    keyboard = None
    if raw.isdigit():
        async with session_scope() as session:
            row = await session.get(Interaction, int(raw))
            meta = (row.meta or {}) if row is not None else {}
            if row is None or meta.get("kind") not in ("question", "note_from_question"):
                body = replies.QUESTION_GONE
            elif meta.get("converted_at"):
                body = replies.ALREADY_SAVED
            else:
                row.meta = {
                    **meta,
                    "kind": "note_from_question",
                    "converted_at": datetime.now(settings.tz).isoformat(),
                }
                row.processed = False
                # A question was never indexed; the note must be.
                row.search_indexed_at = None
                row.codes_indexed_at = None
                result = await process_interaction(session, row)
                receipt, keyboard = _receipt(result)
                body = f"{replies.SAVED_AS_NOTE}\n{receipt}"
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.debug("could not drop the save-as-note button", exc_info=True)
        await _safe_answer(callback.message, body, reply_markup=keyboard)
    try:
        await callback.answer()
    except Exception:
        log.debug("could not acknowledge the callback", exc_info=True)


@router.message(Command("yuk"))
async def cmd_waybill(message: Message, command: CommandObject) -> None:
    """`/yuk YW26-004715` — every message, call and note that mentions it."""
    lookup = client_codes.canonical_lookup(command.args or "")
    if lookup is None:
        await _safe_answer(message, replies.YUK_USAGE)
        return
    async with session_scope() as session:
        lines = await client_codes.mentions(session, lookup[1], limit=30)
    await _safe_answer(message, replies.yuk_report(lookup[1], lines))


async def _code_lookup_reply(session, kind: str, code: str) -> str:
    if kind == "waybill":
        return replies.yuk_report(
            code, await client_codes.mentions(session, code, limit=30)
        )
    holder = await client_codes.holder(session, code)
    if holder is None:
        return replies.code_unknown(code)
    return replies.person_report(await queries.person_summary(session, holder))


@router.callback_query(F.data.startswith("mx:split:"))
async def on_money_split(callback: CallbackQuery) -> None:
    """➕ Bu boshqa to'lov: the typed payment was a second payment after all."""
    parts = (callback.data or "").split(":")
    body = replies.MONEY_SPLIT_GONE
    if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        async with session_scope() as session:
            txn = await _split_typed_payment(session, int(parts[2]), int(parts[3]))
            if txn is not None:
                body = replies.money_split_done(txn)
    markup = callback.message.reply_markup if callback.message is not None else None
    rows = [
        row
        for row in (markup.inline_keyboard if markup is not None else [])
        if not any(b.callback_data == callback.data for b in row)
    ]
    trimmed = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await _finish_row(callback, trimmed, body)


async def _split_typed_payment(session, interaction_id: int, index: int):
    interaction = await session.get(Interaction, interaction_id, with_for_update=True)
    if interaction is None:
        return None
    meta = interaction.meta or {}
    skipped = meta.get("money_skipped") or []
    done = set(meta.get("money_split") or [])
    if index >= len(skipped) or index in done:
        return None
    item = ExtractedTransaction.model_validate(skipped[index])
    txn = await persistence.write_transaction(
        session,
        interaction,
        item,
        persistence.Applied(),
        now=datetime.now(settings.tz),
        skip_bank_match=True,
    )
    if txn is None:
        return None
    await session.flush()
    interaction.meta = {**meta, "money_split": sorted(done | {index})}
    return txn


@router.message(Command("kodlar"), F.document)
async def cmd_code_import(message: Message, bot: Bot) -> None:
    """The owner's client list sent as a file captioned /kodlar (WP-34).

    Read by code, never by the model: the file is stored as a processed
    interaction and only a preview comes back, with one Ha to write it.
    """
    document = message.document
    if await _refuse_dangerous(message, document):
        return
    suffix = Path(document.file_name or "").suffix.lower() or ".bin"
    path = _media_path(suffix)
    if not await _download(bot, message, document, path):
        return
    sniffed = _sniff_download(path)
    if sniffed.dangerous:
        await _safe_answer(message, _refusal(document.file_name))
        return
    async with session_scope() as session:
        # Stored even when unreadable, like a file read_rows rejects: the row
        # is what lets retention delete the download later.
        interaction = Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=message.date.astimezone(settings.tz),
            raw_text=message.caption,
            media=_with_risk(
                {
                    "type": "document",
                    "path": str(path),
                    "filename": document.file_name,
                },
                sniffed,
                document.file_name,
            ),
            meta={"kind": "client_import"},
            processed=True,
        )
        session.add(interaction)
        await session.flush()
        rows = None
        if sniffed.readable:  # a file that is not what its name says is never parsed
            try:
                rows = client_import.read_rows(path)
            except client_import.BadFile:
                rows = None
        if rows is None:
            body, keyboard = replies.IMPORT_BAD_FILE, None
        else:
            plan = await client_import.plan_import(session, rows)
            body = replies.import_preview(document.file_name or path.name, plan.counts())
            keyboard = keyboards.client_import(interaction.id)
            if sniffed.kind == safety.MACRO:
                body += "\n" + replies.macro_document_note(document.file_name)
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


class _Collector:
    """Stands in for the message a button sat on: keeps what the button's
    handler would have said, so one typed reply gets one answer (WP-84)."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.reply_markup = None

    async def answer(self, text, **kwargs):
        self.texts.append(text)

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)

    async def edit_reply_markup(self, **kwargs):
        return None


class _TypedTap:
    """A button press, typed."""

    def __init__(self, data: str, message: _Collector) -> None:
        self.data = data
        self.message = message
        self.from_user = None

    async def answer(self, *args, **kwargs):
        return None


def _button_handlers():
    return {
        "cl": on_claim_button,
        "rec": on_record_button,
        "md": on_media_button,
        keyboards.REVIEW_PREFIX: on_review_button,
    }


async def _batch_reply(message: Message) -> bool:
    """A reply to a question batch — "1 ha 2 yo'q" — answered exactly as the
    taps would have been. False when the text is not one."""
    reply = getattr(message, "reply_to_message", None)
    reply_id = getattr(reply, "message_id", None) if reply is not None else None
    if not isinstance(reply_id, int):
        return False
    async with session_scope() as session:
        logged = list(
            await session.scalars(
                sa.select(QuestionLog)
                .where(QuestionLog.tg_message_id == reply_id)
                .order_by(QuestionLog.id)
            )
        )
        if not logged:
            return False
        answers = question_replies.parse(message.text or "", len(logged))
        if answers is None:
            return False
        waiting = {
            (p.kind, p.ref): p for p in await questions.collect(session, for_push=False)
        }
    done: list[str] = []
    missed: list[int] = []
    outcomes: list[str] = []
    for answer in answers:
        row = logged[answer.number - 1]
        item = waiting.get((row.kind, row.ref))
        if item is None:
            missed.append(answer.number)
            continue
        buttons = keyboards.question_item_row(item, answer.number)
        button = question_replies.button_for(buttons, answer.verb)
        if button is None:
            missed.append(answer.number)
            continue
        if answer.amount_text:
            edit = records.parse_edit(answer.amount_text)
            is_amount = edit is not None and edit.field == "amount"
            if item.kind != questions.KIND_CLAIM or not is_amount:
                missed.append(answer.number)
                continue
            async with session_scope() as session:
                try:
                    await claims.edit(
                        session, item.subject.id, edit, by=claims.BY_COMMAND
                    )
                except (ValueError, claims.AlreadyAnswered):
                    missed.append(answer.number)
                    continue
        prefix = (button.callback_data or "").split(":")[0]
        handler = _button_handlers().get(prefix)
        if handler is None:
            missed.append(answer.number)
            continue
        sink = _Collector()
        await handler(_TypedTap(button.callback_data, sink))
        outcomes.extend(sink.texts)
        done.append(f"{answer.number} {answer.verb}")
    lines = []
    if done:
        lines.append(replies.QUESTION_REPLY_DONE.format(list=", ".join(done)))
    lines += [replies.QUESTION_REPLY_PARTIAL.format(n=n) for n in missed]
    body = "\n".join(lines)
    if outcomes:
        body += "\n\n" + "\n\n".join(outcomes)
    await _safe_answer(message, clip(body))
    return True


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    await _typing(message)
    lookup = client_codes.canonical_lookup(
        (message.text or "").strip().rstrip("?").strip()
    )
    if lookup is not None:
        # A bare code or waybill is a lookup, not a note: SQL answers it and
        # no model is called (WP-40).
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
            reply = await _code_lookup_reply(session, *lookup)
        await _safe_answer(message, reply)
        return
    if await _batch_reply(message):
        return
    async with session_scope() as session:
        history, replied = await _followup_history(session, message)
    mode = rag.classify(message.text or "")
    if replied and mode == "note":
        # A reply to MIYA's answer asks on, "?" or not (WP-75).
        mode = "question"
    if mode != "question" and rag.owner_question(
        message.text or "", message.date.astimezone(settings.tz)
    ):
        # «Kecha kimlar yozdi» needs no '?': SQL answers it alone (JV-06).
        mode = "question"
    if mode != "note":
        await _answer_question(message, message.text, mode, history=history)
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


async def _spoken_question(
    session, interaction: Interaction, text: str, message=None
) -> str | None:
    """A voice or video note that asks something is answered, not logged
    (WP-60); None when it is a note."""
    history, replied = (
        await _followup_history(session, message) if message is not None else ((), False)
    )
    mode = _spoken_mode(text)
    if mode == "note" and replied:
        mode = "question"
    if mode == "note":
        return None
    interaction.meta = {
        **(interaction.meta or {}),
        "kind": "question",
        "mode": mode,
        "via": "voice",
    }
    return await _answer_into(
        session,
        interaction,
        text,
        mode,
        header=replies.voice_question_header(text),
        history=history,
    )


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    media = message.voice or message.audio
    if await _refuse_dangerous(message, media):
        return
    await _typing(message)
    suffix = ".ogg" if message.voice else ".mp3"
    path = _media_path(suffix)
    if not await _download(bot, message, media, path):
        return
    if _sniff_download(path).dangerous:
        await _safe_answer(message, _refusal(getattr(media, "file_name", None)))
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
        asked = None
        if text is None:
            reply = replies.TRANSCRIPTION_FAILED_HINT
        else:
            interaction.media = {**(interaction.media or {}), "processed": True}
            asked = await _spoken_question(session, interaction, text, message)
            if asked is not None:
                reply = asked
            else:
                result = await process_interaction(session, interaction)
                reply, keyboard = _receipt(result)
        interaction_id = interaction.id
    if asked is not None:
        await _send_answer(message, reply, interaction_id)
        return
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    await _typing(message)
    photo = message.photo[-1]  # highest resolution
    path = _media_path(".jpg")
    if not await _download(bot, message, photo, path):
        return
    # Telegram re-encodes a photo as JPEG; its bytes are still checked
    # before the vision model sees them (JV-01, JV-04).
    sniffed = _sniff_download(path)
    if sniffed.dangerous:
        await _safe_answer(message, _refusal(None))
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
        reply, keyboard, card = await _read_image(
            session, interaction, path, readable=sniffed.readable
        )
    await _answer_image(message, reply, keyboard, card)


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    """A file sent to the bot. Installers and archives are refused before any
    download (JV-01); everything else is checked by its bytes before it is
    parsed, and a macro document is read as text only."""
    document = message.document
    if await _refuse_dangerous(message, document):
        return
    if vision.is_heic(document.file_name, document.mime_type):
        # Vision cannot read the iPhone's own format; nothing is fetched.
        await _safe_answer(message, replies.RECEIPT_HEIC_HINT)
        return
    await _typing(message)
    suffix = Path(document.file_name or "").suffix or ".bin"
    path = _media_path(suffix)
    if not await _download(bot, message, document, path):
        return
    sniffed = _sniff_download(path)
    if sniffed.dangerous:
        await _safe_answer(message, _refusal(document.file_name))
        return
    if vision.is_image_document(document.file_name):
        await _on_image_document(message, document, path, sniffed)
        return

    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=message.caption,
            occurred_at=message.date.astimezone(settings.tz),
            media=_with_risk(
                {
                    "type": "document",
                    "path": str(path),
                    "filename": document.file_name,
                    "mime": document.mime_type,
                    "size": document.file_size,
                    "caption": message.caption,
                    "processed": False,
                },
                sniffed,
                document.file_name,
            ),
        )
        parsed = await documents.read_document_async(path) if sniffed.readable else None
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
            if parsed is not None and sniffed.kind == safety.MACRO:
                reply = reply + "\n" + replies.macro_document_note(document.file_name)
    await _safe_answer(message, reply, reply_markup=keyboard)


async def _on_image_document(
    message: Message, document, path: Path, sniffed: safety.Verdict
) -> None:
    """A .jpg/.png/.webp sent as a file, uncompressed, is a photo to vision
    (JV-04) once its bytes passed the shield."""
    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.receipt_photo,
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
        reply, keyboard, card = await _read_image(
            session, interaction, path, readable=sniffed.readable
        )
    await _answer_image(message, reply, keyboard, card)


@router.message(F.video_note)
async def on_video_note(message: Message, bot: Bot) -> None:
    """A video note is a voice note with a face — transcribed always (spec §6)."""
    await _typing(message)
    path = _media_path(".mp4")
    if not await _download(bot, message, message.video_note, path):
        return

    audio_path = path.with_suffix(".mp3")
    keyboard = None
    asked = None
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
                asked = await _spoken_question(session, interaction, text, message)
                if asked is not None:
                    reply = asked
                else:
                    result = await process_interaction(session, interaction)
                    reply, keyboard = _receipt(result)
        interaction_id = interaction.id
    # Replied only after the commit, like every other handler here.
    if asked is not None:
        await _send_answer(message, reply, interaction_id)
        return
    await _safe_answer(message, reply, reply_markup=keyboard)


@router.message(F.video)
async def on_video(message: Message, bot: Bot) -> None:
    """Videos are stored, not transcribed — `/process` opts into the cost."""
    media = message.video
    if await _refuse_dangerous(message, media):
        return
    path = _media_path(".mp4")
    if not await _download(bot, message, media, path):
        return
    if _sniff_download(path).dangerous:
        await _safe_answer(message, _refusal(getattr(media, "file_name", None)))
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
