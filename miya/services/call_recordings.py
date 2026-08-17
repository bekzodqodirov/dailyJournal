"""Call-recording ingestion (spec §7C).

Syncthing drops the phone's call recordings into ``CALL_RECORDINGS_DIR``; a
worker job scans that directory every minute and pushes each new file through
the same pipeline as everything else: transcript → extraction → persisted rows.

Design points, all from the spec:
  * Samsung's filename format varies between firmware versions — parse
    defensively and fall back to the file's mtime.
  * Dedupe by file hash, so a Syncthing re-sync or a renamed file is never
    ingested twice.
  * The original audio stays on disk (encrypted volume); a retention job
    deletes it after ``AUDIO_RETENTION_DAYS``. The interaction and transcript
    outlive the audio.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Interaction
from miya.services.ingest import (
    IngestResult,
    create_interaction,
    process_interaction,
    transcribe_into,
)
from miya.services.people import find_by_phone

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".m4a", ".mp3", ".amr", ".wav", ".3ga", ".ogg", ".aac", ".opus"}

# Syncthing writes into hidden temp files before renaming; never touch them.
_SYNC_TEMP_MARKERS = ("~syncthing~", ".syncthing.", ".tmp", ".part")

# A file must be untouched this long before we trust that the sync finished.
MIN_FILE_AGE_SECONDS = 30


@dataclass(slots=True)
class ParsedRecording:
    """What the filename alone tells us — any field may be missing."""

    counterparty: str | None = None  # contact name or phone number as recorded
    phone: str | None = None  # digits-only phone if the counterparty looks like one
    recorded_at: datetime | None = None


# Samsung stamps recordings with _YYMMDD_HHMMSS before the extension:
#   "Call recording Akmal aka_250817_143025.m4a"
#   "Call recording +998901234567_250817_143025.m4a"
#   "Ovozli qo'ng'iruv 001_250817_143025.amr"
_TIMESTAMP_RE = re.compile(r"[_\-](\d{6})[_\-](\d{6})$")
_PREFIXES = re.compile(
    r"^(call recording|call|voice recording|voice|запись вызова|запись)\s+",
    re.IGNORECASE,
)


def _looks_like_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if 7 <= len(digits) <= 15 and re.fullmatch(r"[+\d][\d\s\-()]*", text.strip()):
        return digits
    return None


def parse_filename(path: Path) -> ParsedRecording:
    """Best-effort parse of a Samsung call-recording filename.

    Every branch tolerates absence: an unrecognised name simply yields an
    empty ParsedRecording and the caller falls back to mtime / no person.
    """
    stem = path.stem
    parsed = ParsedRecording()

    match = _TIMESTAMP_RE.search(stem)
    if match:
        try:
            parsed.recorded_at = datetime.strptime(
                match.group(1) + match.group(2), "%y%m%d%H%M%S"
            ).replace(tzinfo=settings.tz)
        except ValueError:
            parsed.recorded_at = None
        stem = stem[: match.start()]

    stem = _PREFIXES.sub("", stem).strip(" _-")
    if not stem:
        return parsed

    # A bare counter ("001") identifies nothing.
    if re.fullmatch(r"\d{1,4}", stem):
        return parsed

    parsed.counterparty = stem
    parsed.phone = _looks_like_phone(stem)
    return parsed


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_ready_recording(path: Path, *, now: float | None = None) -> bool:
    """True for a settled audio file; False for temp files and fresh syncs."""
    name = path.name.lower()
    if name.startswith(".") or any(marker in name for marker in _SYNC_TEMP_MARKERS):
        return False
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size == 0:
        return False
    now = now if now is not None else time.time()
    return (now - stat.st_mtime) >= MIN_FILE_AGE_SECONDS


async def already_ingested(session: AsyncSession, sha256: str) -> bool:
    found = await session.scalar(
        sa.select(Interaction.id)
        .where(Interaction.source == InteractionSource.phone_call)
        .where(Interaction.media["sha256"].astext == sha256)
        .limit(1)
    )
    return found is not None


async def ingest_recording(session: AsyncSession, path: Path) -> IngestResult | None:
    """Push one audio file through the pipeline. None means already ingested."""
    # Off the event loop: a long recording must not stall the whole worker.
    sha256 = await asyncio.to_thread(file_sha256, path)
    if await already_ingested(session, sha256):
        log.debug("skipping already-ingested recording %s", path.name)
        return None

    parsed = parse_filename(path)
    occurred_at = parsed.recorded_at or datetime.fromtimestamp(
        path.stat().st_mtime, tz=settings.tz
    )

    person = None
    if parsed.phone:
        person = await find_by_phone(session, parsed.phone)

    interaction = await create_interaction(
        session,
        source=InteractionSource.phone_call,
        # Samsung filenames don't say who called whom.
        direction=Direction.na,
        person_id=person.id if person else None,
        occurred_at=occurred_at,
        media={
            "type": "call_recording",
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": sha256,
            "counterparty": parsed.counterparty,
            "phone": parsed.phone,
            "processed": False,
        },
        meta={"filename": path.name},
    )

    text = await transcribe_into(session, interaction, path)
    if text is None:
        # needs_review is already set; the hash row keeps the scan from
        # retrying a permanently broken file every minute. The owner reviews it.
        return IngestResult(interaction=interaction, applied=None, error="transcription")

    # Give the extractor the little context the filename carries.
    if parsed.counterparty and not parsed.phone:
        interaction.raw_text = f"[qo'ng'iroq: {parsed.counterparty}]"

    result = await process_interaction(session, interaction)
    if result.ok:
        interaction.media = {**(interaction.media or {}), "processed": True}
    return result


async def scan_directory(
    session: AsyncSession, directory: Path | None = None
) -> list[IngestResult]:
    """One sweep of the recordings directory. Returns what was newly ingested."""
    directory = directory or Path(settings.call_recordings_dir)
    if not directory.is_dir():
        log.debug("call recordings directory %s does not exist yet", directory)
        return []

    results: list[IngestResult] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or not is_ready_recording(path):
            continue
        try:
            result = await ingest_recording(session, path)
        except Exception:
            log.exception("failed to ingest recording %s", path)
            await session.rollback()
            continue
        if result is not None:
            # Each recording is its own unit of work, and each Scribe call
            # costs money: commit per file so a crash later in the sweep (or
            # in the caller) can never roll back a transcription already paid
            # for. Covers the needs_review row of a failed file too.
            await session.commit()
            results.append(result)
    return results


def purge_old_audio(*, now: datetime | None = None) -> int:
    """Delete audio older than AUDIO_RETENTION_DAYS from both media folders.

    Only files are removed — interactions and transcripts stay. Returns the
    number of files deleted.
    """
    now_ts = (now or datetime.now(settings.tz)).timestamp()
    cutoff = now_ts - settings.audio_retention_days * 86400
    deleted = 0
    for directory in (Path(settings.call_recordings_dir), settings.media_dir):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError:
                log.warning("could not delete expired audio %s", path)
    if deleted:
        log.info(
            "retention: deleted %d audio file(s) older than %d days",
            deleted,
            settings.audio_retention_days,
        )
    return deleted
