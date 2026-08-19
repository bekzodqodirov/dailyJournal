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
| video                | metadata + caption only; `/process` handles it on demand.|
| sticker / gif        | ignored entirely — no interaction row.                  |
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
            return MediaPlan(kind=kind, skip_reason="too_large")
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
        # content that is usually not about the business. `/process` opts in.
        return MediaPlan(kind=kind, skip_reason="video_on_demand")

    # Documents.
    if not docs_enabled:
        return MediaPlan(kind=kind, skip_reason="docs_disabled")
    if size is not None and size > settings.doc_max_bytes:
        return MediaPlan(kind=kind, skip_reason="too_large")
    if not filename or not documents.is_supported(Path(filename)):
        return MediaPlan(kind=kind, skip_reason="unsupported_type")
    return MediaPlan(kind=kind, download=True, read_document=True)
