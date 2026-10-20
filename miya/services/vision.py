"""Photo reading with the extraction model's vision (spec §6, JV-04).

Receipts sent to the assistant bot are always processed — that is what they are
for. In monitored Telegram chats the image itself is only looked at when the
chat has `vision_enabled`; the caption always goes to extraction either way.

One EXTRACT_MODEL call per image returns a `VisionReading`: the description
the extractor has always read (``meta.vision``), and whether the image is a
payment receipt with its printed fields. The receipt fields are the image's
word, never a source of figures on their own: ``services.receipts`` parses
and checks them, and nothing here books money.

The call follows the extraction pattern: structured outputs, with a
prompted-JSON fallback once the server says it cannot compile the schema.
Two attempts through ``retry.with_backoff`` cover an answer that is not a
usable object (truncated, or failing validation); transport errors are the
shared client's own retries (``extraction.MODEL_CLIENT_MAX_RETRIES``) and are
not retried again here. ``read_image`` never raises.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from miya.config import settings
from miya.services import safety
from miya.services.extraction import (
    API_FAILURES,
    CurrencyCode,
    UsageTally,
    get_client,
    is_grammar_failure,
    json_payload,
)
from miya.services.prompts import VISION_READ_PROMPT
from miya.services.retry import with_backoff

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
# The JSON envelope (description, is_receipt, the receipt fields) must fit
# whole: a cut-off answer is a failed read. The extraction budget.
MAX_VISION_TOKENS = 4096
READ_ATTEMPTS = 2
_RETRY_BASE_SECONDS = 0.5
_RETRY_CAP_SECONDS = 2.0

# Images sent as files, uncompressed, that go to vision like a photo once
# their bytes passed safety.sniff_magic (JV-04).
IMAGE_DOCUMENT_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
# The iPhone's own format: the vision call cannot read it, so the owner is
# asked to send a JPG instead.
HEIC_SUFFIXES = frozenset({".heic", ".heif"})
HEIC_MIMES = frozenset(
    {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
)


def _suffix(name: str | None) -> str:
    return Path(safety.normalise_filename(name)).suffix


def is_image_document(name: str | None) -> bool:
    """A file whose name says it is an image the vision call can read."""
    return _suffix(name) in IMAGE_DOCUMENT_SUFFIXES


def is_heic(name: str | None, mime: str | None = None) -> bool:
    label = (mime or "").split(";")[0].strip().casefold()
    return _suffix(name) in HEIC_SUFFIXES or label in HEIC_MIMES


class ReceiptFields(BaseModel):
    """What a payment receipt prints, as the model copied it."""

    amount: float | None = None
    currency: CurrencyCode = "UZS"
    paid_at: str | None = None
    payer_name: str | None = None
    payee_name: str | None = None
    payer_card_last4: str | None = None
    payee_card_last4: str | None = None
    issuer: str | None = None
    reference: str | None = None


class VisionPayload(BaseModel):
    """The JSON object the vision call returns."""

    description: str = ""
    is_receipt: bool = False
    receipt: ReceiptFields | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _no_none_description(cls, value: object) -> object:
        return value or ""


@dataclass(slots=True)
class VisionReading:
    """One image, read. ``error`` is set when nothing usable came back."""

    description: str
    model: str
    is_receipt: bool = False
    receipt: ReceiptFields | None = None
    usage: object | None = None
    error: str | None = None
    image_sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class _Refused(Exception):
    """The model declined to read the image: asking again changes nothing."""


class _Unusable(Exception):
    """The answer was truncated or not a valid object: worth one more try."""


# Like extraction's flag, but for this schema: set once the server cannot
# compile it, kept for the life of the process.
_structured_output_unusable = False


@lru_cache(maxsize=1)
def _schema_instructions() -> str:
    schema = VisionPayload.model_json_schema()
    return (
        "\n\nReturn a JSON object valid against this JSON Schema. "
        "Emit the object only — no prose, no markdown fences.\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def parse_vision_json(text: str) -> VisionPayload | None:
    try:
        return VisionPayload.model_validate_json(json_payload(text))
    except ValidationError as exc:
        log.warning("vision returned an invalid object: %s", type(exc).__name__)
        return None


def _text_of(response: Any) -> str:
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


async def _ask(client: Any, content: list[dict[str, Any]]) -> tuple[Any, Any]:
    """One request: (the response, the parsed payload or None)."""
    global _structured_output_unusable
    messages = [{"role": "user", "content": content}]
    if not _structured_output_unusable:
        try:
            response = await client.messages.parse(
                model=settings.extract_model,
                max_tokens=MAX_VISION_TOKENS,
                messages=messages,
                output_format=VisionPayload,
            )
        except API_FAILURES as exc:
            if not is_grammar_failure(exc):
                raise
            log.warning("structured outputs unavailable for vision; prompted JSON")
            _structured_output_unusable = True
        else:
            return response, response.parsed_output
    prompted = [*content, {"type": "text", "text": _schema_instructions()}]
    response = await client.messages.create(
        model=settings.extract_model,
        max_tokens=MAX_VISION_TOKENS,
        messages=[{"role": "user", "content": prompted}],
    )
    return response, parse_vision_json(_text_of(response))


def _content(
    raw: bytes, media_type: str, now: datetime, *, retry: bool
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(raw).decode(),
            },
        },
        {"type": "text", "text": VISION_READ_PROMPT},
        {"type": "text", "text": f"CURRENT_DATE: {now.date().isoformat()}"},
    ]
    if retry:
        content.append(
            {
                "type": "text",
                "text": "Your previous reply was not valid. Return ONLY a JSON "
                "object matching the schema — no prose, no markdown fences.",
            }
        )
    return content


async def read_image(path: str | Path, *, now: datetime | None = None) -> VisionReading:
    """Read one image. Never raises — failures come back as ``error``."""
    file_path = Path(path)
    model = settings.extract_model
    now = now or datetime.now(settings.tz)

    if not file_path.is_file():
        return VisionReading(description="", model=model, error="file not found")
    raw = file_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    media_type = safety.image_media_type(raw[:16])
    if media_type is None:
        # The shield already refused anything dangerous; what is left is not
        # an image the model could read, whatever its name says.
        return VisionReading(
            description="", model=model, error="not an image", image_sha256=digest
        )
    if len(raw) > MAX_IMAGE_BYTES:
        return VisionReading(
            description="",
            model=model,
            error=f"image too large: {len(raw)} bytes",
            image_sha256=digest,
        )

    tally = UsageTally()
    attempts = 0

    async def _attempt() -> VisionPayload:
        nonlocal attempts
        attempts += 1
        content = _content(raw, media_type, now, retry=attempts > 1)
        try:
            response, parsed = await _ask(client, content)
        except ValidationError as exc:
            # A structured answer the SDK could not validate: try once more.
            raise _Unusable("invalid") from exc
        tally.add(response.usage)
        if response.stop_reason == "refusal":
            raise _Refused()
        if response.stop_reason == "max_tokens":
            raise _Unusable("max_tokens")
        if parsed is None:
            raise _Unusable("invalid")
        return parsed

    try:
        client = get_client()
        payload = await with_backoff(
            _attempt,
            attempts=READ_ATTEMPTS,
            base=_RETRY_BASE_SECONDS,
            cap=_RETRY_CAP_SECONDS,
            retry_on=(_Unusable,),
        )
    except _Refused:
        error = "refusal"
    except _Unusable as exc:
        error = f"unusable answer: {exc}"
    except API_FAILURES as exc:
        error = type(exc).__name__
    else:
        receipt = payload.receipt if payload.is_receipt else None
        return VisionReading(
            description=payload.description.strip(),
            model=model,
            is_receipt=payload.is_receipt,
            receipt=receipt,
            usage=tally.or_none(),
            image_sha256=digest,
        )
    log.warning(
        "vision read failed after %d attempt(s): %s",
        attempts,
        error,
        extra={"attempt": attempts, "error": error},
    )
    return VisionReading(
        description="",
        model=model,
        usage=tally.or_none(),
        error=error,
        image_sha256=digest,
    )
