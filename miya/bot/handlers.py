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
from aiogram.types import Message

from miya.bot import replies
from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Person
from miya.db.session import session_scope
from miya.services import queries
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
    except Exception:
        log.exception("could not send reply to owner (data is committed)")


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


@router.message(F.video_note | F.video | F.document | F.sticker)
async def on_unsupported(message: Message) -> None:
    await _safe_answer(
        message,
        "📎 Bu turdagi fayl hozircha qo'llab-quvvatlanmaydi — "
        "keyingi bosqichda qo'shiladi.",
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
