"""Ingestion pipeline: raw input → interaction → extraction → persisted rows.

Every capture source (assistant bot now; calls and the userbot later) funnels
through `process_interaction`, so extraction behaviour stays identical no matter
where the text came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Interaction
from miya.services import usage as usage_service
from miya.services.extraction import extract
from miya.services.persistence import Applied, apply_extraction
from miya.services.transcription import TranscriptionError, get_transcriber
from miya.services.vision import describe_image

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestResult:
    interaction: Interaction
    applied: Applied | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def create_interaction(
    session: AsyncSession,
    *,
    source: InteractionSource,
    text: str | None = None,
    direction: Direction = Direction.in_,
    person_id: int | None = None,
    tg_chat_id: int | None = None,
    occurred_at: datetime | None = None,
    media: dict | None = None,
    meta: dict | None = None,
) -> Interaction:
    interaction = Interaction(
        source=source,
        direction=direction,
        person_id=person_id,
        tg_chat_id=tg_chat_id,
        occurred_at=occurred_at or datetime.now(settings.tz),
        raw_text=text,
        media=media,
        meta=meta,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def transcribe_into(
    session: AsyncSession, interaction: Interaction, audio_path: str | Path
) -> str | None:
    """Transcribe audio onto the interaction. Returns the text, or None on failure."""
    transcriber = get_transcriber()
    try:
        transcript = await transcriber.transcribe(audio_path)
    except TranscriptionError as exc:
        log.error("transcription failed for interaction %s: %s", interaction.id, exc)
        interaction.needs_review = True
        return None

    interaction.transcript = transcript.text
    await usage_service.record_transcription_usage(
        session,
        provider=transcriber.name,
        model=transcriber.model,
        seconds=transcript.duration,
        source_interaction_id=interaction.id,
    )
    if transcript.language:
        interaction.meta = {
            **(interaction.meta or {}),
            "asr_language": transcript.language,
        }
    return transcript.text


async def describe_into(
    session: AsyncSession, interaction: Interaction, image_path: str | Path
) -> str | None:
    """Run vision triage and append the description to the interaction's metadata."""
    result = await describe_image(image_path)
    if result.usage is not None:
        await usage_service.record_anthropic_usage(
            session,
            model=result.model,
            operation="vision",
            usage=result.usage,
            source_interaction_id=interaction.id,
        )
    if result.error:
        log.warning("vision triage failed for %s: %s", interaction.id, result.error)
        interaction.needs_review = True
        return None
    interaction.meta = {**(interaction.meta or {}), "vision": result.text}
    return result.text


def text_for_extraction(interaction: Interaction) -> str:
    """Everything known about this interaction, as one block for the extractor."""
    parts: list[str] = []
    if interaction.raw_text:
        parts.append(interaction.raw_text.strip())
    if interaction.transcript:
        parts.append(interaction.transcript.strip())
    vision = (interaction.meta or {}).get("vision")
    if vision:
        parts.append(f"[image content]\n{vision.strip()}")
    return "\n\n".join(p for p in parts if p)


async def process_interaction(
    session: AsyncSession, interaction: Interaction
) -> IngestResult:
    """Extract from an interaction and persist what comes back.

    A failed extraction sets `needs_review` and leaves the interaction in place —
    the raw input is never lost (spec §14).
    """
    text = text_for_extraction(interaction)
    if not text:
        interaction.processed = True
        return IngestResult(interaction=interaction, applied=Applied())

    # The recording's own moment, not "now": a backfilled call from last
    # Tuesday must resolve "ertaga" against last Tuesday.
    outcome = await extract(text, now=interaction.occurred_at)
    if outcome.usage is not None:
        await usage_service.record_anthropic_usage(
            session,
            model=outcome.model,
            operation="extract",
            usage=outcome.usage,
            source_interaction_id=interaction.id,
        )

    if not outcome.ok:
        interaction.needs_review = True
        log.error(
            "extraction failed for interaction %s: %s", interaction.id, outcome.error
        )
        return IngestResult(interaction=interaction, applied=None, error=outcome.error)

    applied = await apply_extraction(session, interaction, outcome.result)
    return IngestResult(interaction=interaction, applied=applied)
