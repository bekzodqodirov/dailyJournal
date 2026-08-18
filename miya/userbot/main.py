"""Telethon userbot — passive, read-only (spec §7B).

This process **never writes to Telegram**. There is no call to `send_message`,
`send_file`, `edit_message`, `delete_messages` or `send_read_acknowledge`
anywhere in this package, and a test asserts that stays true. It connects with
an existing session string, registers read-only handlers for new messages in
monitored chats, and stores each one as an interaction. Extraction happens
later, per conversation window, in the worker (spec §7B).

Safety properties, all deliberate:
  * ``USERBOT_ENABLED=false`` exits immediately — one flag disables it.
  * An unauthorised session exits with an error instead of prompting for a
    phone number on stdin, which in a container would hang forever.
  * No history is downloaded on start: only messages that arrive from now on.
    ``chat_monitors`` is synced from the dialog list, which reads titles only.
  * Nothing is ingested from a chat whose ``monitor_enabled`` is false.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import ChatMonitor, Interaction
from miya.db.session import engine, session_scope
from miya.services import audio, documents
from miya.services.chats import DialogInfo, ensure_monitor, sync_dialogs
from miya.services.ingest import create_interaction, transcribe_into
from miya.services.media_policy import MediaKind, MediaPlan, plan_for
from miya.services.people import resolve_person
from miya.services.vision import describe_image

log = logging.getLogger(__name__)


def userbot_media_dir() -> Path:
    path = settings.media_dir / "userbot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _media_path(suffix: str) -> Path:
    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    return userbot_media_dir() / f"{stamp}-{uuid.uuid4().hex[:8]}{suffix}"


# --- Telegram → domain -------------------------------------------------------


def chat_type_of(entity: object) -> ChatType:
    if isinstance(entity, User):
        return ChatType.private
    if isinstance(entity, Chat):
        return ChatType.group
    if isinstance(entity, Channel):
        # Telethon models supergroups and broadcast channels with one type.
        return ChatType.group if getattr(entity, "megagroup", False) else ChatType.channel
    return ChatType.group


def display_name_of(entity: object) -> str:
    """A human name for a Telegram user, falling back through what exists."""
    first = (getattr(entity, "first_name", None) or "").strip()
    last = (getattr(entity, "last_name", None) or "").strip()
    full = " ".join(p for p in (first, last) if p)
    if full:
        return full
    for attr in ("title", "username", "phone"):
        value = (getattr(entity, attr, None) or "").strip()
        if value:
            return value
    return f"tg:{getattr(entity, 'id', 'unknown')}"


def chat_title_of(entity: object) -> str | None:
    title = getattr(entity, "title", None)
    return title or (display_name_of(entity) if isinstance(entity, User) else None)


def kind_of(message: object) -> MediaKind:
    """Classify a Telethon message into the media-policy vocabulary."""
    if getattr(message, "sticker", None) or getattr(message, "gif", None):
        return MediaKind.sticker
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        return MediaKind.voice
    if getattr(message, "video_note", None):
        return MediaKind.video_note
    if getattr(message, "photo", None):
        return MediaKind.photo
    if getattr(message, "video", None):
        return MediaKind.video
    if getattr(message, "document", None):
        return MediaKind.document
    return MediaKind.text


def _file_info(message: object) -> tuple[str | None, int | None, str | None]:
    file = getattr(message, "file", None)
    if file is None:
        return None, None, None
    return (
        getattr(file, "name", None),
        getattr(file, "size", None),
        getattr(file, "mime_type", None),
    )


# --- ingestion ---------------------------------------------------------------


async def _apply_plan(
    session,
    client: TelegramClient,
    message: object,
    interaction: Interaction,
    plan: MediaPlan,
    suffix: str,
) -> None:
    """Download and process one message's media according to `plan`."""
    if not plan.download:
        if plan.skip_reason:
            interaction.media = {
                **(interaction.media or {}),
                "skipped": plan.skip_reason,
            }
        return

    path = _media_path(suffix)
    try:
        await client.download_media(message, file=str(path))
    except Exception:
        log.exception(
            "could not download media for message %s", getattr(message, "id", "?")
        )
        interaction.needs_review = True
        return
    if not path.is_file():
        interaction.needs_review = True
        return

    interaction.media = {**(interaction.media or {}), "path": str(path)}

    if plan.transcribe:
        audio_path = path
        if plan.extract_audio:
            audio_path = path.with_suffix(".mp3")
            if not await audio.extract_audio(path, audio_path):
                interaction.needs_review = True
                return
            interaction.media = {
                **(interaction.media or {}),
                "audio_path": str(audio_path),
            }
        await transcribe_into(session, interaction, audio_path)
        return

    if plan.vision:
        result = await describe_image(path)
        if result.error:
            log.warning("vision failed for message: %s", result.error)
            interaction.needs_review = True
            return
        interaction.meta = {**(interaction.meta or {}), "vision": result.text}
        return

    if plan.read_document:
        parsed = await documents.read_document_async(path)
        if parsed is None:
            interaction.media = {
                **(interaction.media or {}),
                "skipped": "unreadable",
            }
            return
        interaction.transcript = parsed.text
        interaction.meta = {
            **(interaction.meta or {}),
            "document": {"truncated": parsed.truncated, "detail": parsed.detail},
        }


async def _counterparty(session, message, monitor: ChatMonitor):
    """Who this message is about: the peer in a DM, the speaker in a group.

    A DM stays attached to the other person in both directions so `/kim` sees
    one thread; in a group an incoming message belongs to whoever wrote it and
    an outgoing one belongs to nobody (the owner spoke).
    """
    if monitor.chat_type is ChatType.private:
        entity = await message.get_chat()
    elif message.out:
        return None
    else:
        entity = await message.get_sender()

    if entity is None or not isinstance(entity, User):
        return None
    return await resolve_person(
        session,
        display_name_of(entity),
        telegram_id=entity.id,
        telegram_username=getattr(entity, "username", None),
        phone=getattr(entity, "phone", None),
    )


_SUFFIX = {
    MediaKind.voice: ".ogg",
    MediaKind.video_note: ".mp4",
    MediaKind.photo: ".jpg",
    MediaKind.video: ".mp4",
}


async def already_stored(session, tg_chat_id: int, message) -> bool:
    """Guard against re-ingesting a message (reconnect replay, or backfill).

    Looked up by chat and timestamp so the existing
    ``ix_interactions_chat_occurred`` index does the work; the message id is
    then compared exactly.
    """
    occurred = message.date.astimezone(settings.tz)
    rows = await session.scalars(
        sa.select(Interaction.meta)
        .where(Interaction.source == InteractionSource.telegram_userbot)
        .where(Interaction.tg_chat_id == tg_chat_id)
        .where(Interaction.occurred_at == occurred)
    )
    return any((meta or {}).get("tg_message_id") == message.id for meta in rows)


async def ingest_message(client: TelegramClient, message) -> bool:
    """Store one Telegram message. Extraction is the window job's business.

    Takes a Telethon ``Message`` rather than an event so the live handler and
    the explicit backfill tool share exactly one ingestion path.
    """
    kind = kind_of(message)
    filename, size, mime = _file_info(message)

    async with session_scope() as session:
        chat = await message.get_chat()
        monitor = await ensure_monitor(
            session,
            DialogInfo(
                tg_chat_id=message.chat_id,
                chat_type=chat_type_of(chat),
                title=chat_title_of(chat),
            ),
        )
        if not monitor.monitor_enabled:
            return False

        plan = plan_for(
            kind,
            vision_enabled=monitor.vision_enabled,
            docs_enabled=monitor.docs_enabled,
            filename=filename,
            size=size,
        )
        if plan.ignore:
            return False

        text = (message.message or "").strip() or None
        if kind is MediaKind.text and not text:
            return False  # service messages (joins, pins) carry nothing

        if await already_stored(session, message.chat_id, message):
            return False

        person = await _counterparty(session, message, monitor)
        media = None
        if kind is not MediaKind.text:
            media = {
                "type": kind.value,
                "filename": filename,
                "size": size,
                "mime": mime,
                "caption": text,
                "processed": False,
            }

        interaction = await create_interaction(
            session,
            source=InteractionSource.telegram_userbot,
            direction=Direction.out if message.out else Direction.in_,
            person_id=person.id if person else None,
            tg_chat_id=message.chat_id,
            text=text,
            occurred_at=message.date.astimezone(settings.tz),
            media=media,
            meta={"tg_message_id": message.id},
        )
        if media is not None:
            await _apply_plan(
                session, client, message, interaction, plan, _SUFFIX.get(kind, ".bin")
            )
            interaction.media = {**(interaction.media or {}), "processed": True}
    return True


# --- dialog sync -------------------------------------------------------------


async def sync_from_client(client: TelegramClient) -> tuple[int, int]:
    """Register the dialog list. Titles only — no messages are fetched."""
    dialogs: list[DialogInfo] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        dialogs.append(
            DialogInfo(
                tg_chat_id=dialog.id,
                chat_type=chat_type_of(entity),
                title=chat_title_of(entity),
            )
        )
    async with session_scope() as session:
        created, renamed = await sync_dialogs(session, dialogs)
    log.info(
        "dialog sync: %d chats seen, %d new, %d renamed", len(dialogs), created, renamed
    )
    return created, renamed


# --- process -----------------------------------------------------------------


async def run() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if not settings.userbot_enabled:
        log.warning("USERBOT_ENABLED=false — the passive reader stays off")
        return
    if not (
        settings.telethon_api_id
        and settings.telethon_api_hash
        and settings.telethon_session
    ):
        raise SystemExit(
            "TELETHON_API_ID, TELETHON_API_HASH and TELETHON_SESSION are required "
            "when USERBOT_ENABLED=true"
        )

    client = TelegramClient(
        StringSession(settings.telethon_session),
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    # connect(), not start(): start() would prompt for a phone number on stdin
    # if the session were invalid, which in a container never returns.
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit(
                "TELETHON_SESSION is not authorised — regenerate it with "
                "`make userbot-login`"
            )

        me = await client.get_me()
        log.info("userbot connected as %s (read-only)", display_name_of(me))
        await sync_from_client(client)

        async def _on_message(event) -> None:
            try:
                await ingest_message(client, event.message)
            except Exception:
                # One bad message must never take the reader down; the next
                # message still lands.
                log.exception("failed to ingest message %s", getattr(event, "id", "?"))

        client.add_event_handler(_on_message, events.NewMessage(incoming=True))
        client.add_event_handler(_on_message, events.NewMessage(outgoing=True))
        log.info("listening for new messages in monitored chats")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()
        await engine.dispose()
        log.info("userbot stopped")


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
