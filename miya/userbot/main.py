"""Telethon userbot — passive, read-only (spec §7B).

This process **never writes to Telegram**. There is no call to `send_message`,
`send_file`, `edit_message`, `delete_messages` or `send_read_acknowledge`
anywhere in this package, and a test asserts that stays true. It connects with
an existing session string, registers read-only handlers for new messages in
monitored chats, and stores each one as an interaction. Extraction happens
later, per conversation window, in the worker (spec §7B).

Safety properties, all deliberate:
  * ``USERBOT_ENABLED=false`` idles without connecting — one flag disables it.
  * An unauthorised session exits with an error instead of prompting for a
    phone number on stdin, which in a container would hang forever.
  * No history is downloaded on start: only messages that arrive from now on.
    ``chat_monitors`` is synced from the dialog list, which reads titles only.
  * Nothing is ingested from a chat whose ``monitor_enabled`` is false.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, MessageEntityTextUrl, User

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource, WindowStatus
from miya.db.models import ChatMonitor, ConversationWindow, Interaction, Person
from miya.db.session import engine, session_scope
from miya.logs import setup_logging
from miya.services import (
    approvals,
    audio,
    authorship,
    chats,
    documents,
    health,
    media_policy,
    owner_address,
    safety,
    windows,
)
from miya.services import usage as usage_service
from miya.services.chats import DialogInfo, ensure_monitor, sync_dialogs
from miya.services.ingest import apply_vision, create_interaction
from miya.services.media_policy import (
    MediaKind,
    MediaPlan,
    forced_plan,
    plan_for,
)
from miya.services.people import resolve_person
from miya.services.text import (  # noqa: F401 - transliterate is re-exported
    alias_pattern,
    fold_apostrophes,
    transliterate,
)
from miya.services.transcription import TranscriptionError, get_transcriber
from miya.services.vision import read_image

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
    if getattr(message, "voice", None):
        return MediaKind.voice
    if getattr(message, "audio", None):
        return MediaKind.audio
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


@dataclass(slots=True)
class MediaOutcome:
    """Result of the slow half of ingestion, computed with no database held."""

    path: Path | None = None
    audio_path: Path | None = None
    transcript: object | None = None  # Transcript
    vision: object | None = None  # VisionReading
    document: object | None = None  # DocumentText
    skip_reason: str | None = None
    failed: bool = False  # download or conversion failed; needs the owner's eye
    # What the bytes turned out to be when they were not plain safe (JV-01).
    risk: safety.Verdict | None = None


async def fetch_media(
    client: TelegramClient, message: object, plan: MediaPlan, suffix: str
) -> MediaOutcome:
    """Download and process media **outside any transaction**.

    Scribe can take minutes on a long voice message. Doing that inside the
    ingest transaction would pin a pooled connection and — worse — hold the
    person-resolution advisory lock the whole time, stalling every other
    writer in the system. So the slow work happens here, with nothing held,
    and only the results are persisted afterwards.
    """
    if not plan.download:
        return MediaOutcome(skip_reason=plan.skip_reason, risk=plan.risk)

    path = _media_path(suffix)
    try:
        await client.download_media(message, file=str(path))
    except Exception:
        log.exception(
            "could not download media for message %s", getattr(message, "id", "?")
        )
        return MediaOutcome(failed=True)
    if not path.is_file():
        return MediaOutcome(failed=True)

    sniffed = safety.sniff_magic(path, path.suffix)
    if sniffed.dangerous:
        # An installer under a document's, a song's or a video's name: gone
        # before anything reads or transcribes it, and the owner is warned
        # like for any other installer.
        path.unlink(missing_ok=True)
        return MediaOutcome(skip_reason=media_policy.SKIP_DANGEROUS, risk=sniffed)

    outcome = MediaOutcome(path=path)

    if plan.transcribe:
        audio_path = path
        if plan.extract_audio:
            audio_path = path.with_suffix(".mp3")
            if not await audio.extract_audio(path, audio_path):
                outcome.failed = True
                return outcome
            outcome.audio_path = audio_path
        try:
            outcome.transcript = await get_transcriber().transcribe(audio_path)
        except TranscriptionError as exc:
            log.error("transcription failed for a Telegram message: %s", exc)
            outcome.failed = True
        return outcome

    if plan.vision:
        if not sniffed.readable:
            # Not an image whatever its name says (JV-04): never sent to vision.
            outcome.skip_reason = media_policy.SKIP_MISMATCH
            outcome.failed = True
            return outcome
        sent_at = getattr(message, "date", None)
        outcome.vision = await read_image(
            path, now=sent_at.astimezone(settings.tz) if sent_at else None
        )
        return outcome

    if plan.read_document:
        if not sniffed.readable:
            # Not what its name says; never parsed, kept for the owner's eye.
            outcome.skip_reason = media_policy.SKIP_MISMATCH
            outcome.failed = True
            return outcome
        if sniffed.kind == safety.MACRO:
            outcome.risk = sniffed
        elif plan.risk is not None and plan.risk.kind == safety.MACRO:
            outcome.risk = plan.risk
        outcome.document = await documents.read_document_async(path)
        if outcome.document is None:
            # The policy only plans a read for a format we support, so failing
            # here means a real document was lost — the owner should see it in
            # /tekshir rather than never hearing about it.
            outcome.skip_reason = "unreadable"
            outcome.failed = True
    return outcome


def _media_pushable(message, monitor: ChatMonitor) -> bool:
    if getattr(message, "out", False) and not settings.media_ask_outgoing:
        return False
    return monitor.chat_type is ChatType.private or settings.media_ask_in_groups


async def _recheck_to_me(session, interaction: Interaction) -> None:
    """A group voice note is only words once transcribed: look for the
    owner's names again, before the window closes over it (WP-37)."""
    meta = interaction.meta or {}
    if (
        meta.get("to_me")
        or interaction.window_id is not None
        or interaction.direction is not Direction.in_
        or interaction.tg_chat_id is None
        or not interaction.transcript
    ):
        return
    chat_type = await session.scalar(
        sa.select(ChatMonitor.chat_type).where(
            ChatMonitor.tg_chat_id == interaction.tg_chat_id
        )
    )
    if chat_type is not ChatType.group:
        return
    if mentions_owner(interaction.transcript):
        sender = (
            await session.get(Person, interaction.person_id)
            if interaction.person_id
            else None
        )
        interaction.meta = await owner_address.demote_if_namesake(
            session,
            {**meta, "to_me": True, "to_me_via": "transcript"},
            interaction.transcript,
            mentioned=False,
            chat_id=interaction.tg_chat_id,
            sender=sender,
            aliases=_owner_aliases(),
        )


def _warn_owner(*, incoming: bool, archive: bool, occurred_at: datetime) -> bool:
    """Only what others sent, live: never an archive import or old history."""
    if not incoming or archive:
        return False
    return occurred_at >= datetime.now(settings.tz) - safety.NOTICE_MAX_AGE


def _flag_risk(interaction: Interaction, verdict: safety.Verdict) -> None:
    """media.risk, meta.safety and the owner's warning for a file found risky
    only once its bytes were read (JV-01)."""
    meta = interaction.meta or {}
    if verdict.kind == safety.MACRO and interaction.transcript is None:
        notify = False  # the warning says it was read as text; it was not
    else:
        notify = _warn_owner(
            incoming=interaction.direction is Direction.in_,
            archive=bool(meta.get("archive")),
            occurred_at=interaction.occurred_at,
        )
    interaction.meta = safety.with_file_verdict(meta, verdict, notify=notify)


async def persist_media(session, interaction: Interaction, outcome: MediaOutcome) -> None:
    """Write what `fetch_media` produced. Short, and inside one transaction."""
    media = dict(interaction.media or {})
    if outcome.skip_reason:
        media["skipped"] = outcome.skip_reason
    if outcome.risk is not None:
        media["risk"] = outcome.risk.as_risk(media.get("filename"))
    if outcome.path:
        media["path"] = str(outcome.path)
    if outcome.audio_path:
        media["audio_path"] = str(outcome.audio_path)

    if outcome.failed:
        interaction.needs_review = True

    if outcome.transcript is not None:
        interaction.transcript = outcome.transcript.text
        await usage_service.record_transcription_usage(
            session,
            provider=settings.transcriber,
            model=getattr(get_transcriber(), "model", None),
            seconds=outcome.transcript.duration,
            source_interaction_id=interaction.id,
        )
        if outcome.transcript.language:
            interaction.meta = {
                **(interaction.meta or {}),
                "asr_language": outcome.transcript.language,
            }
        await _recheck_to_me(session, interaction)

    if outcome.vision is not None:
        # The description for the extractor and, for a payment receipt, its
        # receipts row; the card goes out from the worker (JV-04).
        await apply_vision(session, interaction, outcome.vision, presume_receipt=False)

    if outcome.document is not None:
        interaction.transcript = outcome.document.text
        interaction.meta = {
            **(interaction.meta or {}),
            "document": {
                "truncated": outcome.document.truncated,
                "detail": outcome.document.detail,
            },
        }

    if outcome.risk is not None:
        _flag_risk(interaction, outcome.risk)

    media["processed"] = True
    interaction.media = media


async def _counterparty_entity(message, monitor: ChatMonitor):
    """The account this message is about: the peer in a DM, the speaker in a
    group; None for the owner's own group messages. Its Telegram flags
    (``scam``, ``fake``, ``contact``) feed the message shield (JV-09)."""
    if monitor.chat_type is ChatType.private:
        return await message.get_chat()
    if message.out:
        return None
    return await message.get_sender()


async def _counterparty(session, message, monitor: ChatMonitor, entity=None):
    """Who this message is about: the peer in a DM, the speaker in a group.

    A DM stays attached to the other person in both directions so `/kim` sees
    one thread; in a group an incoming message belongs to whoever wrote it and
    an outgoing one belongs to nobody (the owner spoke). ``entity`` is the
    account when the caller already looked it up.
    """
    if entity is None:
        entity = await _counterparty_entity(message, monitor)
    if entity is None or not isinstance(entity, User):
        return None
    # A contact the owner saved carries the owner's own spelling, codes and
    # all (WP-35); anyone else's profile name only suggests a code.
    saved = bool(getattr(entity, "contact", False))
    return await resolve_person(
        session,
        display_name_of(entity),
        telegram_id=entity.id,
        telegram_username=getattr(entity, "username", None),
        phone=getattr(entity, "phone", None),
        code_policy="attach" if saved else "suggest",
        source="contact" if saved else "tg_name",
    )


# --- the message shield (JV-09) -------------------------------------------------

# How many of a sender's earlier messages say how they usually write.
SHIELD_HISTORY = 50


def _text_links(message) -> list[tuple[str, str]]:
    """``(shown text, url)`` for each Telegram text link. Entity offsets count
    UTF-16 code units, as Telegram does."""
    raw = getattr(message, "message", None) or ""
    encoded = raw.encode("utf-16-le")
    links = []
    for entity in getattr(message, "entities", None) or ():
        if not isinstance(entity, MessageEntityTextUrl):
            continue
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        links.append((encoded[start:end].decode("utf-16-le", "ignore"), entity.url))
    return links


def _button_urls(message) -> list[str]:
    """The URLs on a message's inline buttons; read from the markup itself, so
    nothing is fetched."""
    urls = []
    for row in getattr(getattr(message, "reply_markup", None), "rows", None) or ():
        for button in getattr(row, "buttons", None) or ():
            url = getattr(button, "url", None)
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


@dataclasses.dataclass(slots=True)
class _History:
    prior_messages: int = 0
    owner_wrote_before: bool = False
    texts: tuple[str, ...] = ()
    risky_files: bool = False
    short_links: bool = False
    loans: bool = False


async def _sender_history(
    session, *, person_id: int | None, chat_id: int, private: bool
) -> _History:
    """What the sender's earlier messages look like: their newest
    SHIELD_HISTORY incoming messages (by person, else by private chat), and
    whether the owner ever wrote in this private chat.

    A group sender MIYA cannot name (an anonymous admin, a channel posting
    into the group) has no history of their own: the group's is everyone
    else's, so they count as unknown and nothing is compared."""
    if person_id is None and not private:
        return _History()
    who = (
        Interaction.person_id == person_id
        if person_id is not None
        else Interaction.tg_chat_id == chat_id
    )
    rows = (
        await session.execute(
            sa.select(Interaction.raw_text, Interaction.media)
            .where(Interaction.source == InteractionSource.telegram_userbot)
            .where(Interaction.direction == Direction.in_, who)
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
            .limit(SHIELD_HISTORY)
        )
    ).all()
    wrote = False
    if private:
        wrote = bool(
            await session.scalar(
                sa.select(
                    sa.exists().where(
                        Interaction.source == InteractionSource.telegram_userbot,
                        Interaction.tg_chat_id == chat_id,
                        # MIYA's own replies are not the owner writing (JV-10).
                        authorship.owner_words_clause(),
                    )
                )
            )
        )
    texts = tuple(text for text, _ in rows if text)
    risky = False
    for _, media in rows:
        risk = (media or {}).get("risk") if isinstance(media, dict) else None
        if isinstance(risk, dict) and risk.get("kind") in (safety.HTML, safety.MACRO):
            risky = True
    return _History(
        prior_messages=len(rows),
        owner_wrote_before=wrote,
        texts=texts,
        risky_files=risky,
        short_links=any(
            safety.is_short_link(u) for t in texts for u in safety.extract_urls(t)
        ),
        loans=any(safety.is_loan_request(t) for t in texts),
    )


async def _message_shield(
    session,
    message,
    monitor: ChatMonitor,
    *,
    entity,
    person,
    kind: MediaKind,
    plan: MediaPlan,
) -> safety.MessageVerdict | None:
    """The JV-09 verdict on an incoming message, before any download or model
    call. A failure here is logged with the message's ids and costs the
    message nothing: it is stored without a verdict."""
    try:
        async with session.begin_nested():
            history = await _sender_history(
                session,
                person_id=person.id if person is not None else None,
                chat_id=message.chat_id,
                private=monitor.chat_type is ChatType.private,
            )
        text = getattr(message, "message", None) or ""
        features = safety.MessageFeatures(
            text=text,
            urls=tuple(
                safety.extract_urls(
                    text,
                    text_links=_text_links(message),
                    buttons=_button_urls(message),
                )
            ),
            attachment=plan.risk,
            has_file=kind is MediaKind.document,
            sender_scam=bool(getattr(entity, "scam", False)),
            sender_fake=bool(getattr(entity, "fake", False)),
            forwarded=getattr(message, "fwd_from", None) is not None,
            via_bot=bool(getattr(message, "via_bot_id", None)),
            saved_contact=bool(
                getattr(entity, "contact", False)
                or getattr(entity, "mutual_contact", False)
            ),
            prior_messages=history.prior_messages,
            owner_wrote_before=history.owner_wrote_before,
            history_script=safety.dominant_script(history.texts),
            history_risky_files=history.risky_files,
            history_short_links=history.short_links,
            history_loans=history.loans,
        )
        return safety.scam_verdict(
            features, extra_safe=settings.safety_extra_safe_domains_list
        )
    except Exception as exc:
        log.error(
            "message shield failed for message %s in %s: %s",
            getattr(message, "id", None),
            message.chat_id,
            type(exc).__name__,
            extra={
                "tg_chat_id": message.chat_id,
                "tg_message_id": getattr(message, "id", None),
                "error": type(exc).__name__,
            },
        )
        return None


def _quarantined(plan: MediaPlan) -> MediaPlan:
    """A scam message's media is never fetched, read, transcribed or asked
    about; a file the JV-01 shield refused keeps that reason."""
    return dataclasses.replace(
        plan,
        download=False,
        transcribe=False,
        extract_audio=False,
        vision=False,
        read_document=False,
        ask=False,
        ask_reason=None,
        skip_reason=plan.skip_reason or safety.SKIP_QUARANTINED,
    )


_SUFFIX = {
    MediaKind.voice: ".ogg",
    MediaKind.audio: ".mp3",
    MediaKind.video_note: ".mp4",
    MediaKind.photo: ".jpg",
    MediaKind.video: ".mp4",
}


def _suffix_for(kind: MediaKind, filename: str | None) -> str:
    """Where a download lands. Documents and audio keep their real extension.

    documents.read_document() dispatches on the suffix, so a `.bin` path made
    every invoice and packing list in every monitored chat unreadable — the
    text was dropped with nothing flagged. Audio files are the same story one
    layer down: a shared `.m4a` saved as `.mp3` is handed to Scribe with a
    content type that does not match its bytes.
    """
    if kind in (MediaKind.document, MediaKind.audio) and filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix
    return _SUFFIX.get(kind, ".bin")


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


TG_MESSAGE_INDEX = "ux_interactions_tg_message"
# Media an archive import leaves untranscribed unless asked (WP-76).
_SPOKEN_KINDS = (MediaKind.voice, MediaKind.audio, MediaKind.video_note, MediaKind.video)
# How many earlier versions of an edited message are kept (WP-74).
MAX_EDITS = 10


async def record_edit(client: TelegramClient, message) -> bool:
    """A message was edited: keep the old words as a revision and store the
    new ones (WP-74). Reactions and pins also arrive as edits; the text is
    compared first, so they change nothing. True when something changed."""
    async with session_scope() as session:
        monitor = await session.scalar(
            sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == message.chat_id)
        )
        row = await session.scalar(
            sa.select(Interaction).where(
                Interaction.source == InteractionSource.telegram_userbot,
                Interaction.tg_chat_id == message.chat_id,
                Interaction.meta.has_key("tg_message_id"),
                Interaction.meta["tg_message_id"].astext.cast(sa.BigInteger)
                == message.id,
            )
        )
        if row is None:
            unknown = True
        else:
            unknown = False
            if monitor is None or not monitor.monitor_enabled:
                return False
            new = (message.message or "").strip() or None
            if new == row.raw_text:
                return False
            edited_at = getattr(message, "edit_date", None) or datetime.now(settings.tz)
            meta = dict(row.meta or {})
            meta["edits"] = [
                *(meta.get("edits") or []),
                {
                    "at": edited_at.astimezone(settings.tz).isoformat(),
                    "old": row.raw_text,
                },
            ][-MAX_EDITS:]
            if isinstance(row.media, dict) and "caption" in row.media:
                row.media = {**row.media, "caption": new}
            row.raw_text = new  # the WP-39 listener re-indexes it
            if isinstance(getattr(message, "id", None), int):
                monitor.last_seen_message_id = max(
                    monitor.last_seen_message_id or 0, message.id
                )
            if row.window_id is not None:
                window = await session.get(ConversationWindow, row.window_id)
                if window is not None and window.status is WindowStatus.pending:
                    row.meta = meta
                    await session.flush()
                    await windows.rerender(session, window)
                else:
                    # Already extracted: flagged, never re-extracted.
                    meta["edited_after_extraction"] = True
            row.meta = meta
    if unknown:
        return await ingest_message(client, message)
    return True


async def ingest_message(
    client: TelegramClient,
    message,
    *,
    archive: bool = False,
    transcribe_media: bool = True,
    read_media: bool = True,
) -> bool:
    """Store one Telegram message. Extraction is the window job's business.

    Takes a Telethon ``Message`` rather than an event so the live handler and
    the explicit backfill tool share exactly one ingestion path.

    ``archive`` (WP-76): an old message imported for search only — stored
    processed, never windowed or extracted, never an open question. Without
    ``transcribe_media`` its voice and video are kept as metadata only;
    without ``read_media`` (the style import, JV-14) all of its media is.
    """
    kind = kind_of(message)
    filename, size, mime = _file_info(message)
    plan: MediaPlan | None = None
    interaction_id: int | None = None

    # Transaction 1: decide, dedupe, resolve the person and store the message.
    # Short by design — see fetch_media().
    async with session_scope() as session:
        chat = await message.get_chat()
        monitor = await ensure_monitor(
            session,
            DialogInfo(
                tg_chat_id=message.chat_id,
                chat_type=chat_type_of(chat),
                title=chat_title_of(chat),
                is_bot=bool(getattr(chat, "bot", False)),
            ),
        )
        if not monitor.monitor_enabled:
            if monitor.chat_type is not ChatType.private and monitor.decided_by is None:
                # Activity only — counters and timestamps rank the group in
                # the owner's next digest (WP-20); nothing it says is kept.
                await chats.note_activity(
                    session,
                    monitor,
                    out=bool(getattr(message, "out", False)),
                    addressed=addressed_to_owner(message, monitor.chat_type),
                    now=datetime.now(settings.tz),
                )
            return False

        # The catch-up cursor (WP-21): every message seen in an allowed chat
        # advances it, stickers and service messages included.
        if isinstance(getattr(message, "id", None), int):
            monitor.last_seen_message_id = max(
                monitor.last_seen_message_id or 0, message.id
            )

        plan = plan_for(
            kind,
            vision_enabled=monitor.vision_enabled,
            docs_enabled=monitor.docs_enabled,
            filename=filename,
            size=size,
            mime=mime,
        )
        if plan.ignore:
            return False

        text = (message.message or "").strip() or None
        if kind is MediaKind.text and not text:
            return False  # service messages (joins, pins) carry nothing

        if await already_stored(session, message.chat_id, message):
            return False

        entity = await _counterparty_entity(message, monitor)
        person = await _counterparty(session, message, monitor, entity)
        # JV-09: the spam and scam verdict, before anything is downloaded or
        # read by a model. A scam is quarantined: nothing fetched, nothing
        # asked, never windowed for extraction. An archive import is search
        # history only and needs none.
        shield = (
            None
            if message.out or archive
            else await _message_shield(
                session,
                message,
                monitor,
                entity=entity,
                person=person,
                kind=kind,
                plan=plan,
            )
        )
        quarantined = shield is not None and shield.quarantined
        if quarantined and kind is not MediaKind.text:
            plan = _quarantined(plan)
        elif shield is not None and shield.verdict == safety.VERDICT_SPAM and plan.ask:
            # A spammer's big file is never worth a tap: no question is opened.
            plan = dataclasses.replace(
                plan,
                ask=False,
                ask_reason=None,
                skip_reason=safety.SKIP_SPAM,
            )
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
            if plan.risk is not None:
                # JV-01: what the shield saw, before anything was fetched.
                media["risk"] = plan.risk.as_risk(filename)
            skip_archive = archive and (
                plan.ask
                or not read_media
                or (not transcribe_media and kind in _SPOKEN_KINDS)
            )
            if skip_archive:
                # An archive import asks nothing and spends nothing unless told.
                media["processed"] = True
                media["skipped"] = "archive"
            elif plan.ask:
                # Nothing is downloaded now. The worker turns this into a
                # question in Telegram, and only a yes brings the file over.
                media["approval"] = {
                    "state": approvals.PENDING,
                    "reason": plan.ask_reason,
                    # WP-46: the owner's own files and group files wait in
                    # /savollar; only what others send privately is pushed.
                    "pushable": _media_pushable(message, monitor),
                }

        meta = await owner_address.demote_if_namesake(
            session,
            _message_meta(message, monitor),
            text or "",
            mentioned=bool(getattr(message, "mentioned", False)),
            chat_id=message.chat_id,
            sender=person,
            aliases=_owner_aliases(),
        )
        if message.out:
            # JV-10: an outgoing message MIYA sent from the outbox is tagged as
            # such, whichever path stores it first (live, catch-up, backfill).
            echo = await authorship.find_echo(
                session,
                channel=authorship.CHANNEL_TELEGRAM,
                external_id=authorship.telegram_external_id(message.chat_id, message.id),
            )
            if echo is not None:
                meta = authorship.tagged(meta, echo)
        warn = _warn_owner(
            incoming=not message.out,
            archive=archive,
            occurred_at=message.date.astimezone(settings.tz),
        )
        if plan.risk is not None and plan.risk.dangerous:
            meta = safety.with_file_verdict(meta, plan.risk, notify=warn)
        if shield is not None:
            meta = safety.with_message_verdict(meta, shield, notify=warn)
        try:
            async with session.begin_nested():
                interaction = await create_interaction(
                    session,
                    source=InteractionSource.telegram_userbot,
                    direction=Direction.out if message.out else Direction.in_,
                    person_id=person.id if person else None,
                    tg_chat_id=message.chat_id,
                    text=text,
                    occurred_at=message.date.astimezone(settings.tz),
                    media=media,
                    meta={**meta, "archive": True} if archive else meta,
                )
                if archive or quarantined:
                    interaction.processed = True
        except sa.exc.IntegrityError as exc:
            # The catch-up and the live handler raced for the same message:
            # the other path stored it (ux_interactions_tg_message).
            constraint = getattr(
                getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None
            )
            if constraint != TG_MESSAGE_INDEX:
                raise
            log.debug("message %s in %s already stored", message.id, message.chat_id)
            return False
        interaction_id = interaction.id
        if media is None or plan.ask or media.get("skipped") == "archive":
            return True

    # The message is durable now. Everything slow happens with no database
    # connection and no advisory lock held.
    outcome = await fetch_media(client, message, plan, _suffix_for(kind, filename))

    # Transaction 2: record what came back.
    async with session_scope() as session:
        interaction = await session.get(Interaction, interaction_id)
        if interaction is None:  # purged while the download ran
            return True
        await persist_media(session, interaction, outcome)
    return True


# --- the reply candidate (JV-16) -----------------------------------------------


async def enqueue_reply(message) -> bool:
    """Queue a live incoming private message for Jarvis's responder, once it
    is stored. Only SQL here: no model is called in a Telethon handler, and
    the responder reads the chat later in the worker. Never called for the
    catch-up, a backfill or an edit (only ``on_new_message`` calls it);
    miya.jarvis is imported only with JARVIS_ENABLED (rule 20)."""
    if not settings.jarvis_enabled or getattr(message, "out", False):
        return False
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "id", None)
    sent_at = getattr(message, "date", None)
    if not isinstance(chat_id, int) or chat_id <= 0 or not isinstance(message_id, int):
        return False  # a group or a channel: the responder answers private chats only
    now = datetime.now(settings.tz)
    if sent_at is None or sent_at < now - timedelta(
        seconds=settings.jarvis_max_message_age_seconds
    ):
        return False
    from miya.jarvis import responder

    async with session_scope() as session:
        interaction = await session.scalar(
            sa.select(Interaction).where(
                Interaction.source == InteractionSource.telegram_userbot,
                Interaction.tg_chat_id == chat_id,
                Interaction.meta.has_key("tg_message_id"),
                Interaction.meta["tg_message_id"].astext.cast(sa.BigInteger)
                == message_id,
            )
        )
        if interaction is None:
            return False
        return await responder.enqueue_candidate(session, interaction, now=now)


async def on_new_message(client: TelegramClient, message) -> None:
    """The live NewMessage handler: store the message, then (JV-16) queue it
    as a reply candidate when ``ingest_message`` returned True. Each step is
    contained: one bad message never takes the reader down."""
    try:
        stored = await ingest_message(client, message)
    except Exception:
        # One bad message must never take the reader down; the next
        # message still lands.
        log.exception("failed to ingest message %s", getattr(message, "id", "?"))
        return
    if not stored:
        return
    try:
        await enqueue_reply(message)
    except Exception as exc:
        log.error(
            "reply candidate not queued",
            extra={
                "tg_chat_id": getattr(message, "chat_id", None),
                "tg_message_id": getattr(message, "id", None),
                "error": type(exc).__name__,
            },
        )


# --- who a message was aimed at ----------------------------------------------
#
# The Latin→Cyrillic table and alias_pattern live in services/text.py (WP-28).


def addressed_to_owner(message: object, chat_type: ChatType) -> bool:
    """Was this message aimed at the owner rather than at the room?

    Only meaningful in a group: in a private chat every message is addressed
    to him, so the flag would mark everything and distinguish nothing. His
    own outgoing messages are never addressed to him either — he writes his
    company's name and signs with his own, and a flag there would list his
    words under "Sizga murojaatlar" and put the group on the instant path.

    Telethon sets `mentioned` for both an @-mention and a reply to one of the
    owner's own messages, which is exactly the question being asked — the two
    are the same act from where he is sitting. People mostly do neither: they
    type "Bekzod aka, konteyner qachon?" in plain text, so the message text is
    also searched for the owner's aliases (OWNER_ALIASES), whole word, either
    script, any case.
    """
    if chat_type in (ChatType.private, ChatType.channel):
        # A channel post is a broadcast: it can never be aimed at the owner.
        return False
    if getattr(message, "out", False):
        return False
    if getattr(message, "mentioned", False):
        return True
    return mentions_owner(getattr(message, "message", None) or "")


# The owner's own @username, learned from get_me() at start (WP-37): it need
# not be written into .env to count.
_RUNTIME_ALIASES: tuple[str, ...] = ()


def _owner_aliases() -> tuple[str, ...]:
    return settings.owner_aliases_parsed + _RUNTIME_ALIASES


def mentions_owner(text: str) -> bool:
    pattern = alias_pattern(_owner_aliases())
    return bool(pattern and pattern.search(fold_apostrophes(text or "")))


def _message_meta(message: object, monitor: ChatMonitor) -> dict:
    meta = {"tg_message_id": message.id}
    if addressed_to_owner(message, monitor.chat_type):
        meta["to_me"] = True
    return meta


# --- approved media ----------------------------------------------------------
#
# The owner answers in the assistant bot, which has no Telegram *user* session
# and so cannot fetch anything from a private group. Only this process can, so
# it polls for the answers rather than being told about them.

APPROVED_POLL_SECONDS = 60


async def fetch_approved(client: TelegramClient) -> int:
    """Download and process everything the owner has said yes to."""
    async with session_scope() as session:
        jobs = [
            (
                interaction.id,
                interaction.tg_chat_id,
                (interaction.meta or {}).get("tg_message_id"),
                dict(interaction.media or {}),
            )
            for interaction in await approvals.approved_for_fetch(session)
        ]

    done = 0
    for interaction_id, chat_id, message_id, media in jobs:
        kind = MediaKind(media.get("type", MediaKind.other.value))
        filename = media.get("filename")
        try:
            message = await client.get_messages(chat_id, ids=message_id)
        except Exception:
            log.exception("could not re-read approved message %s", message_id)
            message = None

        if message is None:
            # Deleted, or the account lost access to the chat. Nothing will
            # ever make this fetchable, so it must not stay in the queue.
            async with session_scope() as session:
                interaction = await session.get(Interaction, interaction_id)
                if interaction is not None:
                    approvals.set_state(interaction, approvals.DONE, error="gone")
                    interaction.needs_review = True
            continue

        plan = forced_plan(kind, filename, mime=media.get("mime"))
        outcome = await fetch_media(client, message, plan, _suffix_for(kind, filename))

        async with session_scope() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None:  # purged while the download ran
                continue
            await persist_media(session, interaction, outcome)
            # persist_media rewrites media wholesale, so the approval is
            # stamped after it rather than before.
            approvals.set_state(interaction, approvals.DONE)
        done += 1

    if done:
        log.info("fetched %d approved attachment(s)", done)
    return done


async def fetch_backfills(client: TelegramClient) -> int:
    """Read the last week of every group the owner just said yes to.

    The same shape as ``fetch_approved``: the bot recorded the request on the
    monitor row (``chats.accept_join``), and this process — the only one with
    a Telegram user session — performs it. The reading itself is delegated to
    ``miya.tools.backfill``, so this package still contains no history call
    of its own. A chat is stamped done after one successful pass; a failing
    one is retried a bounded number of sweeps and then left alone, switched
    on, with the failure in the log.
    """
    from miya.tools import backfill as backfill_tool

    async with session_scope() as session:
        jobs = [
            (monitor.id, monitor.tg_chat_id, monitor.title)
            for monitor in await chats.pending_backfills(session)
        ]

    done = 0
    for monitor_id, chat_id, title in jobs:
        try:
            stored = await backfill_tool.backfill_chat(
                client, chat_id, chats.BACKFILL_DAYS
            )
        except Exception:
            log.exception("backfill of %s (%s) failed", title, chat_id)
            stored = None
        async with session_scope() as session:
            monitor = await session.get(ChatMonitor, monitor_id)
            if monitor is None:
                continue
            if stored is None:
                chats.mark_backfill_failed(monitor)
            else:
                chats.mark_backfilled(monitor)
                done += 1
                log.info("backfilled %s (%s): %d message(s)", title, chat_id, stored)
    return done


# The style import's pause (JV-14): a long FloodWait sets the monotonic time
# before which no sweep asks Telegram for history again.
_style_import_resume_at = 0.0


async def fetch_style_imports(client: TelegramClient) -> int:
    """Read the private chats the owner asked to learn their style from
    (JV-14, ``/uslub yukla``).

    The same shape as ``fetch_backfills``: the bot recorded the request on
    each monitor row (``chats.request_style_import``), and this process
    reads it through ``miya.tools.backfill.fetch_style_history``, so this
    package still contains no history call of its own. Read-only: what is
    read is stored as archive rows, nothing is sent. Each chat is contained
    on its own: a failure is logged with its ids, counted on the row (a
    chat is given up on after a few sweeps) and the next chat goes on. A
    long FloodWait ends the sweep and is no failure: the paused chat stays
    pending with its attempts untouched, and no sweep reads anything until
    Telegram's wait is over.
    """
    global _style_import_resume_at
    from miya.tools import backfill as backfill_tool

    if time.monotonic() < _style_import_resume_at:
        return 0

    async with session_scope() as session:
        jobs = [
            (monitor.id, monitor.tg_chat_id)
            for monitor in await chats.pending_style_imports(session)
        ]

    done = 0
    for monitor_id, chat_id in jobs:
        try:
            result = await backfill_tool.fetch_style_history(client, chat_id)
        except Exception as exc:
            log.error(
                "style import of a chat failed",
                extra={
                    "monitor_id": monitor_id,
                    "tg_chat_id": chat_id,
                    "error": type(exc).__name__,
                },
            )
            result = None
        if result is not None and result.paused:
            # Telegram's limit, not the chat's failure: the row stays pending
            # as it is, and the sweeps wait the whole FloodWait out.
            _style_import_resume_at = time.monotonic() + result.wait_seconds
            log.info(
                "style import paused by Telegram",
                extra={
                    "monitor_id": monitor_id,
                    "tg_chat_id": chat_id,
                    "seconds": result.wait_seconds,
                },
            )
            break
        try:
            async with session_scope() as session:
                monitor = await session.get(ChatMonitor, monitor_id)
                if monitor is None:
                    continue
                if result is None:
                    chats.mark_style_import_failed(monitor)
                else:
                    chats.mark_style_imported(monitor)
                    done += 1
                    log.info(
                        "style import read a chat",
                        extra={
                            "monitor_id": monitor_id,
                            "tg_chat_id": chat_id,
                            "stored": result.stored,
                            "seen": result.seen,
                        },
                    )
        except Exception as exc:
            log.error(
                "style import state not saved",
                extra={
                    "monitor_id": monitor_id,
                    "tg_chat_id": chat_id,
                    "error": type(exc).__name__,
                },
            )
    return done


async def beat_userbot(
    *, enabled: bool, connected: bool | None = None, user_id: int | None = None
) -> bool:
    """The userbot's heartbeat (build step 5). Never raises.

    ``{"enabled": false}`` is the switched-off process saying so once, which
    /holat shows as ⏸ rather than ❌; a live one records whether Telethon is
    still connected and as whom. A database hiccup is a log line: the reader
    and its loop must not stop over a missed beat.
    """
    detail: dict = {"enabled": enabled}
    if enabled:
        detail.update({"connected": bool(connected), "user": user_id})
        # A catch-up line must survive the next beat, which rewrites the
        # detail whole (WP-21).
        detail.update(_last_catch_up or {})
    try:
        async with session_scope() as session:
            await health.beat(session, "userbot", detail=detail)
        return True
    except Exception:
        log.warning("userbot heartbeat could not be written", exc_info=True)
        return False


async def approved_media_loop(
    client: TelegramClient, *, user_id: int | None = None
) -> None:
    """The owner's answers, polled: approved attachments, requested backfills
    and the style history import (JV-14).

    Every pass also beats the ``userbot`` heartbeat, so a reader whose
    Telegram session died shows up as silent within USERBOT_STALE_MINUTES.
    """
    last_catch_up = time.monotonic()
    while True:
        if time.monotonic() - last_catch_up >= settings.userbot_catchup_minutes * 60:
            last_catch_up = time.monotonic()
            try:
                await catch_up_sweep(client)
            except Exception:
                log.exception("catch-up sweep failed")
        await beat_userbot(enabled=True, connected=client.is_connected(), user_id=user_id)
        try:
            await fetch_approved(client)
        except Exception:
            # A failure here must never take the reader down with it.
            log.exception("approved-media sweep failed")
        try:
            await fetch_backfills(client)
        except Exception:
            log.exception("backfill sweep failed")
        try:
            await fetch_style_imports(client)
        except Exception as exc:
            log.error("style import sweep failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(APPROVED_POLL_SECONDS)


# --- catch-up after downtime (WP-21) ------------------------------------------
#
# Telethon's live handler sees only what arrives while it runs, and a
# StringSession keeps no update state, so every restart, deploy or network
# drop used to lose what was said meanwhile. After each dialog sync the
# chats whose newest message is past the cursor are read forward from it —
# never before the chat was switched on — through the same ingestion path.

_last_catch_up: dict | None = None
_dialog_tops: dict[int, int] = {}


async def catch_up_sweep(client: TelegramClient) -> int:
    """Read every allowed chat forward from its cursor; returns messages stored."""
    from miya.tools import backfill as backfill_tool

    global _last_catch_up
    await sync_from_client(client)
    async with session_scope() as session:
        monitors = list(
            await session.scalars(
                sa.select(ChatMonitor)
                .where(ChatMonitor.monitor_enabled.is_(True))
                .order_by(ChatMonitor.monitoring_since.nulls_first(), ChatMonitor.id)
            )
        )
        jobs = [
            (m.id, m.tg_chat_id, m.last_seen_message_id, m.monitoring_since)
            for m in monitors
            if (_dialog_tops.get(m.tg_chat_id) or 0) > (m.last_seen_message_id or 0)
        ][: settings.userbot_catchup_max_chats]

    stored = chats_read = 0
    now = datetime.now(settings.tz)
    for monitor_id, chat_id, after_id, since in jobs:
        n, top = await backfill_tool.catch_up_chat(
            client,
            chat_id,
            after_id=after_id,
            since=since or now,
            limit=settings.userbot_catchup_max_per_chat,
        )
        async with session_scope() as session:
            monitor = await session.get(ChatMonitor, monitor_id)
            if monitor is not None and top:
                monitor.last_seen_message_id = max(monitor.last_seen_message_id or 0, top)
        stored += n
        chats_read += 1
    if stored:
        _last_catch_up = {"caught_up": stored, "caught_up_at": now.isoformat()}
        log.info("caught up %d message(s) in %d chat(s)", stored, chats_read)
    return stored


async def _startup_catch_up(client: TelegramClient) -> None:
    try:
        await catch_up_sweep(client)
    except Exception:
        log.exception("startup catch-up failed")


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
                is_bot=bool(getattr(entity, "bot", False)),
                top_message_id=getattr(getattr(dialog, "message", None), "id", None),
            )
        )
    async with session_scope() as session:
        created, renamed = await sync_dialogs(session, dialogs)
    global _dialog_tops
    _dialog_tops = {
        d.tg_chat_id: d.top_message_id for d in dialogs if d.top_message_id is not None
    }
    log.info(
        "dialog sync: %d chats seen, %d new, %d renamed", len(dialogs), created, renamed
    )
    return created, renamed


# --- process -----------------------------------------------------------------


async def _idle_forever() -> None:  # pragma: no cover - patched in tests
    await asyncio.Event().wait()


async def run() -> None:
    setup_logging("userbot")
    if not settings.userbot_enabled:
        # Idle instead of exiting: the container uses `restart:
        # unless-stopped` (so it survives a VPS reboot), and a clean exit here
        # would make Docker restart it in a tight loop forever.
        log.warning("USERBOT_ENABLED=false — the passive reader stays off")
        # Said once, so /holat shows the reader as switched off, not silent.
        # The pool is released afterwards: an idle process holds no
        # connection for weeks.
        await beat_userbot(enabled=False)
        await engine.dispose()
        await _idle_forever()
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

    # A session string is ~350 characters and starts with a version digit.
    # Telethon rejects anything else with a bare `ValueError: Not a valid
    # string` under twenty lines of traceback, which tells the owner nothing
    # about the actual mistake — and the mistake is nearly always mechanical:
    # the TELETHON_SESSION= prefix pasted twice, surviving quotes, or a line
    # truncated by the terminal it was copied out of.
    # Catching Exception, not ValueError: a string that starts with the right
    # digit but was cut short gets far enough to fail unpacking instead, as
    # `struct.error: unpack requires a buffer of 275 bytes` — and a truncated
    # paste is the likeliest slip of all.
    try:
        session = StringSession(settings.telethon_session)
    except Exception as exc:
        raise SystemExit(
            f"TELETHON_SESSION is not a valid session string ({exc}). "
            f"It should be one unbroken line of about 350 characters starting "
            f"with '1'; this one is {len(settings.telethon_session)} and starts "
            f"with {settings.telethon_session[:6]!r}. Check .env for a repeated "
            "TELETHON_SESSION= prefix, surrounding quotes, or a truncated "
            "paste, then re-run miya.tools.userbot_login if it is lost."
        ) from exc

    client = TelegramClient(
        session,
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    # connect(), not start(): start() would prompt for a phone number on stdin
    # if the session were invalid, which in a container never returns.
    # Retried with backoff: a container often comes up before the network
    # does, and one failed connect must not burn a restart-policy attempt.
    for attempt in range(5):
        try:
            await client.connect()
            break
        except (OSError, ConnectionError) as exc:
            wait = 2**attempt * 5
            log.warning("connect failed (%s); retrying in %ds", exc, wait)
            await asyncio.sleep(wait)
    else:
        raise SystemExit("could not reach Telegram after 5 attempts")
    try:
        if not await client.is_user_authorized():
            raise SystemExit(
                "TELETHON_SESSION is not authorised — regenerate it with "
                "`make userbot-login`"
            )

        me = await client.get_me()
        global _RUNTIME_ALIASES
        if getattr(me, "username", None):
            _RUNTIME_ALIASES = ("@" + me.username,)
        log.info("userbot connected as %s (read-only)", display_name_of(me))
        await beat_userbot(enabled=True, connected=True, user_id=me.id)
        await sync_from_client(client)

        async def _on_message(event) -> None:
            await on_new_message(client, event.message)

        async def _on_edit(event) -> None:
            try:
                await record_edit(client, event.message)
            except Exception:
                log.exception("failed to record an edit of %s", getattr(event, "id", "?"))

        client.add_event_handler(_on_message, events.NewMessage(incoming=True))
        client.add_event_handler(_on_message, events.NewMessage(outgoing=True))
        client.add_event_handler(_on_edit, events.MessageEdited())
        sweeper = asyncio.create_task(approved_media_loop(client, user_id=me.id))
        # Whatever was said while this process was down (WP-21), read once
        # the live handlers are in place so nothing falls between the two.
        catcher = asyncio.create_task(_startup_catch_up(client))
        log.info("listening for new messages in monitored chats")
        try:
            await client.run_until_disconnected()
        finally:
            sweeper.cancel()
            catcher.cancel()
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
