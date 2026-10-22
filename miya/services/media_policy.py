"""What to do with each kind of media in a monitored chat (spec §6).

Kept free of Telethon and aiogram types on purpose: the policy is the part
worth testing exhaustively, and it should not need a Telegram connection to
exercise. The transport layers classify a message into a ``MediaKind`` and then
do what the returned plan says.

| Media                | Action                                                  |
|----------------------|---------------------------------------------------------|
| voice / video note   | download → (ffmpeg) → Scribe transcript. Always.        |
| photo                | caption always; the image only if `vision_enabled`.     |
| document             | if `docs_enabled` and readable and ≤ DOC_MAX_BYTES.     |
| image file           | a .jpg/.png/.webp sent as a file: like a photo (JV-04). |
| video                | ask the owner; `/process` still forces one directly.    |
| oversized audio/doc  | ask the owner, up to MEDIA_ASK_MAX_BYTES.               |
| sticker / gif        | ignored entirely — no interaction row.                  |
| executable / archive | never fetched, never offered; the owner is warned.      |

The dangerous-file shield (JV-01, ``services/safety.py``) runs before every
other rule, on every channel: an installer or an archive is refused whatever
its size and whatever the chat's toggles say, and the owner's "yes" cannot
fetch one either.

"Ask" is the third outcome, between doing it and dropping it: the message is
stored, nothing is downloaded, and the owner gets a Telegram question with two
buttons. Silence costs nothing and the file is never fetched.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from miya.config import settings
from miya.services import documents, safety, vision

Channel = Literal["telegram", "whatsapp", "gmail"]
CHANNELS: tuple[Channel, ...] = ("telegram", "whatsapp", "gmail")

# Why a download was declined (media.skipped).
SKIP_DANGEROUS = "dangerous_file"
SKIP_HTML = "html_file"
# Downloaded, but the bytes are not what the name claims: never parsed.
SKIP_MISMATCH = "mismatched_type"
# An image sent as a file that vision cannot take (JV-04).
SKIP_HEIC = "heic_image"
SKIP_IMAGE_TOO_LARGE = "image_too_large"


class MediaKind(str, enum.Enum):
    text = "text"
    voice = "voice"
    audio = "audio"
    video_note = "video_note"
    photo = "photo"
    video = "video"
    document = "document"
    sticker = "sticker"
    other = "other"


@dataclass(slots=True)
class MediaPlan:
    kind: MediaKind
    ignore: bool = False  # drop the message entirely, no interaction row
    download: bool = False
    transcribe: bool = False
    extract_audio: bool = False  # video note: pull the audio track first
    vision: bool = False
    read_document: bool = False
    skip_reason: str | None = None  # why a download was declined, for metadata
    # Too big or too expensive to fetch unasked, but not worthless: MIYA asks
    # the owner in Telegram and downloads only if he says yes. `ask_reason` is
    # what the question tells him.
    ask: bool = False
    ask_reason: str | None = None
    # What the shield found (exec, archive, macro, html); None for a safe file.
    risk: safety.Verdict | None = None
    channel: Channel = "telegram"


def _check_channel(channel: str) -> Channel:
    if channel not in CHANNELS:
        raise ValueError(f"unknown media channel {channel!r}")
    return cast(Channel, channel)


def _shield(
    kind: MediaKind, filename: str | None, mime: str | None, channel: Channel
) -> tuple[MediaPlan | None, safety.Verdict | None]:
    """The refusal for a dangerous or HTML attachment, else the macro risk.

    Voice notes and photos carry no file name; documents, audio files and
    videos do, and any of them can be an installer under another label.
    """
    if kind in (MediaKind.text, MediaKind.other, MediaKind.sticker):
        return None, None
    verdict = safety.classify_attachment(filename, mime)
    if verdict.dangerous:
        reason = SKIP_DANGEROUS
    elif verdict.kind == safety.HTML:
        reason = SKIP_HTML
    else:
        return None, verdict if verdict.kind == safety.MACRO else None
    return MediaPlan(
        kind=kind, skip_reason=reason, risk=verdict, channel=channel
    ), verdict


def forced_plan(
    kind: MediaKind,
    filename: str | None = None,
    *,
    mime: str | None = None,
    channel: Channel = "telegram",
) -> MediaPlan:
    """The plan for media the owner has explicitly approved.

    The same handling as the automatic path minus every gate: the size caps
    and the per-chat toggles have already been overruled by the person they
    exist to protect. Only genuine impossibility still refuses — a document
    format nothing here can read — and so does the shield: an installer or an
    archive is never fetched, approved or not.
    """
    channel = _check_channel(channel)
    refused, risk = _shield(kind, filename, mime, channel)
    if refused is not None:
        return refused
    plan = _forced(kind, filename)
    plan.risk = risk
    plan.channel = channel
    return plan


def _forced(kind: MediaKind, filename: str | None) -> MediaPlan:
    if kind in (MediaKind.voice, MediaKind.audio):
        return MediaPlan(kind=kind, download=True, transcribe=True)
    if kind in (MediaKind.video, MediaKind.video_note):
        return MediaPlan(kind=kind, download=True, transcribe=True, extract_audio=True)
    if kind is MediaKind.photo:
        return MediaPlan(kind=kind, download=True, vision=True)
    if kind is MediaKind.document:
        if vision.is_image_document(filename):
            return MediaPlan(kind=kind, download=True, vision=True)
        if not filename or not documents.is_supported(Path(filename)):
            return MediaPlan(kind=kind, skip_reason="unsupported_type")
        return MediaPlan(kind=kind, download=True, read_document=True)
    return MediaPlan(kind=kind)


def _ask_or_skip(kind: MediaKind, size: int | None, reason: str) -> MediaPlan:
    """Offer the download to the owner, unless it is beyond any sane size.

    An unknown size is treated as askable: Telegram omits it on some forwards,
    and refusing to even ask would put the decision back where it was.
    """
    if size is not None and size > settings.media_ask_max_bytes:
        return MediaPlan(kind=kind, skip_reason="beyond_ask_limit")
    return MediaPlan(kind=kind, ask=True, ask_reason=reason, skip_reason=reason)


def plan_for(
    kind: MediaKind,
    *,
    vision_enabled: bool,
    docs_enabled: bool,
    filename: str | None = None,
    size: int | None = None,
    mime: str | None = None,
    channel: Channel = "telegram",
) -> MediaPlan:
    """Decide how one piece of media should be handled.

    The same policy serves every channel (Telegram, WhatsApp, Gmail); the
    shield runs first, before the size branch, so a large installer is
    refused rather than offered.
    """
    channel = _check_channel(channel)
    refused, risk = _shield(kind, filename, mime, channel)
    if refused is not None:
        return refused
    plan = _plan(
        kind,
        vision_enabled=vision_enabled,
        docs_enabled=docs_enabled,
        filename=filename,
        size=size,
        mime=mime,
    )
    plan.risk = risk
    plan.channel = channel
    return plan


def _plan(
    kind: MediaKind,
    *,
    vision_enabled: bool,
    docs_enabled: bool,
    filename: str | None,
    size: int | None,
    mime: str | None = None,
) -> MediaPlan:
    if kind in (MediaKind.text, MediaKind.other):
        return MediaPlan(kind=kind)

    if kind is MediaKind.sticker:
        return MediaPlan(kind=kind, ignore=True)

    if kind is MediaKind.voice:
        return MediaPlan(kind=kind, download=True, transcribe=True)

    if kind is MediaKind.audio:
        # An audio *file* (music, a podcast) is not a voice note: transcribing
        # a shared 3-hour mix would cost real Scribe money for nothing. Small
        # files still go through — forwarded call recordings arrive this way.
        if size is not None and size > settings.audio_max_bytes:
            return _ask_or_skip(kind, size, "too_large")
        return MediaPlan(kind=kind, download=True, transcribe=True)

    if kind is MediaKind.video_note:
        return MediaPlan(kind=kind, download=True, transcribe=True, extract_audio=True)

    if kind is MediaKind.photo:
        # The caption reaches the extractor either way; only the pixels are gated.
        return MediaPlan(
            kind=kind,
            download=vision_enabled,
            vision=vision_enabled,
            skip_reason=None if vision_enabled else "vision_disabled",
        )

    if kind is MediaKind.video:
        # Never automatic: a 200 MB video costs bandwidth and Scribe minutes for
        # content that is usually not about the business. But silently ignoring
        # it lost the occasional video that mattered, so the owner is asked.
        # `/process` still forces one directly.
        return _ask_or_skip(kind, size, "video")

    # Documents. An image sent as a file, uncompressed, is a photo to vision
    # (JV-04): the same `vision_enabled` gate, its bytes checked by
    # safety.sniff_magic after the download and before the model sees them.
    if vision.is_image_document(filename):
        if not vision_enabled:
            return MediaPlan(kind=kind, skip_reason="vision_disabled")
        if size is not None and size > vision.MAX_IMAGE_BYTES:
            return MediaPlan(kind=kind, skip_reason=SKIP_IMAGE_TOO_LARGE)
        return MediaPlan(kind=kind, download=True, vision=True)
    if vision.is_heic(filename, mime):
        return MediaPlan(kind=kind, skip_reason=SKIP_HEIC)
    if not docs_enabled:
        return MediaPlan(kind=kind, skip_reason="docs_disabled")
    if size is not None and size > settings.doc_max_bytes:
        return _ask_or_skip(kind, size, "too_large")
    if not filename or not documents.is_supported(Path(filename)):
        return MediaPlan(kind=kind, skip_reason="unsupported_type")
    return MediaPlan(kind=kind, download=True, read_document=True)
