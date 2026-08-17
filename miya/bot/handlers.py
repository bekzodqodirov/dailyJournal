"""Assistant bot handlers (spec §7A). Owner-only — every other user is ignored."""

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


async def _ingest_and_reply(message: Message, interaction, session) -> None:
    result = await process_interaction(session, interaction)
    if not result.ok:
        await message.answer(replies.FAILED_EXTRACTION_HINT)
        return
    await message.answer(replies.confirmation(result.applied))


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(replies.HELP)


@router.message(Command("yordam", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(replies.HELP)


@router.message(Command("qarz"))
async def cmd_debts(message: Message) -> None:
    async with session_scope() as session:
        balances = await queries.open_debts(session)
    await message.answer(replies.debts_report(balances))


@router.message(Command("vada", "va_da"))
async def cmd_promises(message: Message) -> None:
    async with session_scope() as session:
        items = await queries.open_promises(session)
    await message.answer(replies.promises_report(items))


@router.message(Command("bugun"))
async def cmd_today(message: Message) -> None:
    async with session_scope() as session:
        summary = await queries.day_summary(session)
    await message.answer(replies.day_report(summary))


@router.message(Command("kim"))
async def cmd_person(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer("Ism yozing: <code>/kim Akmal</code>")
        return

    async with session_scope() as session:
        people = list(await session.scalars(sa.select(Person)))
        person, score = best_match(name, people)
        if person is None or score < 70:
            await message.answer(f"❓ <b>{name}</b> topilmadi.")
            return
        summary = await queries.person_summary(session, person)
    await message.answer(replies.person_report(summary))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    async with session_scope() as session:
        interaction = await create_interaction(
            session,
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            text=message.text,
            occurred_at=message.date.astimezone(settings.tz),
        )
        await _ingest_and_reply(message, interaction, session)


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    media = message.voice or message.audio
    suffix = ".ogg" if message.voice else ".mp3"
    path = _media_path(suffix)
    await bot.download(media, destination=path)

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
            await message.answer(
                "⚠️ Ovozni matnga o'girib bo'lmadi. Fayl saqlandi, keyinroq urinaman."
            )
            return
        interaction.media = {**(interaction.media or {}), "processed": True}
        await _ingest_and_reply(message, interaction, session)


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    photo = message.photo[-1]  # highest resolution
    path = _media_path(".jpg")
    await bot.download(photo, destination=path)

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
            await message.answer("⚠️ Rasmni o'qib bo'lmadi. Saqlandi, keyinroq urinaman.")
            return
        interaction.media = {**(interaction.media or {}), "processed": True}
        await _ingest_and_reply(message, interaction, session)


@router.message(F.video_note | F.video | F.document | F.sticker)
async def on_unsupported(message: Message) -> None:
    await message.answer(
        "📎 Bu turdagi fayl hozircha qo'llab-quvvatlanmaydi — "
        "keyingi bosqichda qo'shiladi."
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
