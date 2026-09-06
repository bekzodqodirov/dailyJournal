"""Photo triage with Haiku vision (spec §6).

Receipts sent to the assistant bot are always processed — that is what they are
for. In monitored Telegram chats the image itself is only looked at when the
chat has `vision_enabled`; the caption always goes to extraction either way.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from miya.config import settings
from miya.services.extraction import API_FAILURES, get_client
from miya.services.prompts import VISION_TRIAGE_PROMPT

log = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(slots=True)
class VisionResult:
    text: str
    model: str
    usage: object | None = None
    error: str | None = None


def media_type_for(path: Path) -> str | None:
    return _MEDIA_TYPES.get(path.suffix.lower())


async def describe_image(path: str | Path) -> VisionResult:
    """One vision call. Never raises — failures come back as `error`."""
    file_path = Path(path)
    model = settings.extract_model

    if not file_path.is_file():
        return VisionResult(text="", model=model, error=f"file not found: {file_path}")
    media_type = media_type_for(file_path)
    if media_type is None:
        return VisionResult(
            text="", model=model, error=f"unsupported image type: {file_path.suffix}"
        )
    raw = file_path.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        return VisionResult(
            text="", model=model, error=f"image too large: {len(raw)} bytes"
        )

    try:
        response = await get_client().messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(raw).decode(),
                            },
                        },
                        {"type": "text", "text": VISION_TRIAGE_PROMPT},
                    ],
                }
            ],
        )
    except API_FAILURES as exc:
        return VisionResult(text="", model=model, error=f"{type(exc).__name__}: {exc}")

    if response.stop_reason == "refusal":
        return VisionResult(text="", model=model, usage=response.usage, error="refusal")

    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return VisionResult(text=text, model=model, usage=response.usage)
