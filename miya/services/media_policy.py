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
| video                | ask the owner; `/process` still forces one directly.    |
| oversized audio/doc  | ask the owner, up to MEDIA_ASK_MAX_BYTES.               |
| sticker / gif        | ignored entirely — no interaction row.                  |

"Ask" is the third outcome, between doing it and dropping it: the message is
stored, nothing is downloaded, and the owner gets a Telegram question with two
buttons. Silence costs nothing and the file is never fetched.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from miya.config import settings
from miya.services import documents


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


def forced_plan(kind: MediaKind, filename: str | None = None) -> MediaPlan:
    """The plan for media the owner has explicitly approved.

    The same handling as the automatic path minus every gate: the size caps
    and the per-chat toggles have already been overruled by the person they
    exist to protect. Only genuine impossibility still refuses — a document
    format nothing here can read.
    """
    if kind in (MediaKind.voice, MediaKind.audio):
        return MediaPlan(kind=kind, download=True, transcribe=True)
    if kind in (MediaKind.video, MediaKind.video_note):
        return MediaPlan(kind=kind, download=True, transcribe=True, extract_audio=True)
    if kind is MediaKind.photo:
        return MediaPlan(kind=kind, download=True, vision=True)
    if kind is MediaKind.document:
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
) -> MediaPlan:
    """Decide how one piece of media should be handled."""
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

    # Documents.
    if not docs_enabled:
        return MediaPlan(kind=kind, skip_reason="docs_disabled")
    if size is not None and size > settings.doc_max_bytes:
        return _ask_or_skip(kind, size, "too_large")
    if not filename or not documents.is_supported(Path(filename)):
        return MediaPlan(kind=kind, skip_reason="unsupported_type")
    return MediaPlan(kind=kind, download=True, read_document=True)
