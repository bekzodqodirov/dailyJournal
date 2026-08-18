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
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from miya.bot import replies
from miya.bot.formatting import clip
from miya.bot.keyboards import FIELD_CODES, PAGE_SIZE, ChatsPage, chats_keyboard
from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Person
from miya.db.session import session_scope
from miya.services import (
    audio,
    chats,
    documents,
    memories,
    planner,
    queries,
    rag,
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


async def _safe_answer(message: Message, text: str | None) -> None:
    """Reply after the data is durable; a failed send only costs the receipt."""
    if not text:
        return
    try:
        await message.answer(text)
        return
    except Exception:
        log.exception("could not send reply to owner (data is committed)")
    # Model-composed replies can contain broken HTML; a plain-text retry beats
    # the owner never seeing the answer at all.
    try:
        await message.answer(text, parse_mode=None)
    except Exception:
        log.exception("plain-text retry failed too")


async def _typing(message: Message) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        log.debug("send_chat_action failed", exc_info=True)


async def _download(bot: Bot, message: Message, media, path: Path) -> bool:
    """Fetch a file from Telegram; on failure tell the owner instead of dying."""
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


@router.message(Command("tekshir"))
async def cmd_review(message: Message) -> None:
    async with session_scope() as session:
        flagged, total = await queries.flagged_interactions(session)
        body = replies.review_report(flagged, total)
    await _safe_answer(message, body)


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
        reply = (
            replies.confirmation(result.applied)
            if result.ok
            else replies.FAILED_EXTRACTION_HINT
        )
    await _safe_answer(message, reply)


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
        if text is None:
            reply = replies.TRANSCRIPTION_FAILED_HINT
        else:
            interaction.media = {**(interaction.media or {}), "processed": True}
            result = await process_interaction(session, interaction)
            reply = (
                replies.confirmation(result.applied)
                if result.ok
                else replies.FAILED_EXTRACTION_HINT
            )
    await _safe_answer(message, reply)


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
        if described is None and not message.caption:
            reply = replies.PHOTO_FAILED_HINT
        else:
            interaction.media = {**(interaction.media or {}), "processed": True}
            result = await process_interaction(session, interaction)
            if not result.ok:
                reply = replies.FAILED_EXTRACTION_HINT
            elif described is None:
                # The caption was extracted, but the image itself was not read —
                # the owner must not be told everything succeeded.
                reply = (
                    replies.confirmation(result.applied)
                    + "\n"
                    + replies.VISION_PARTIAL_HINT
                )
            else:
                reply = replies.confirmation(result.applied)
    await _safe_answer(message, reply)


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
            reply = (
                replies.confirmation(result.applied)
                if result.ok
                else replies.FAILED_EXTRACTION_HINT
            )
    await _safe_answer(message, reply)


@router.message(F.video | F.video_note)
async def on_video(message: Message, bot: Bot) -> None:
    """Videos are stored, not transcribed — `/process` opts into the cost."""
    media = message.video or message.video_note
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


@router.message(Command("process"))
async def cmd_process(message: Message, bot: Bot) -> None:
    """On-demand processing of a replied-to media message (spec §6, §7A)."""
    target = message.reply_to_message
    if target is None:
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
        reply = await _process_on_demand(
            session,
            interaction,
            path,
            is_video=is_video,
            is_audio=is_audio,
            is_photo=is_photo,
        )
    await _safe_answer(message, reply)


async def _process_on_demand(
    session, interaction, path: Path, *, is_video: bool, is_audio: bool, is_photo: bool
) -> str:
    """Run the right pipeline for one explicitly requested media file."""
    if is_video:
        audio_path = path.with_suffix(".mp3")
        if not await audio.extract_audio(path, audio_path):
            interaction.needs_review = True
            return replies.TRANSCRIPTION_FAILED_HINT
        interaction.media = {**(interaction.media or {}), "audio_path": str(audio_path)}
        if await transcribe_into(session, interaction, audio_path) is None:
            return replies.TRANSCRIPTION_FAILED_HINT
    elif is_audio:
        if await transcribe_into(session, interaction, path) is None:
            return replies.TRANSCRIPTION_FAILED_HINT
    elif is_photo:
        described = await describe_into(session, interaction, path)
        if described is None and not interaction.raw_text:
            return replies.PHOTO_FAILED_HINT
    else:
        parsed = await documents.read_document_async(path)
        if parsed is None and not interaction.raw_text:
            interaction.needs_review = True
            return replies.DOCUMENT_FAILED_HINT
        if parsed is not None:
            interaction.transcript = parsed.text

    interaction.media = {**(interaction.media or {}), "processed": True}
    result = await process_interaction(session, interaction)
    return (
        replies.confirmation(result.applied)
        if result.ok
        else replies.FAILED_EXTRACTION_HINT
    )


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
