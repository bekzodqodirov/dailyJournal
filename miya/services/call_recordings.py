"""Call-recording ingestion (spec §7C).

Syncthing drops the phone's call recordings into ``CALL_RECORDINGS_DIR``; a
worker job scans that directory every minute and pushes each new file through
the same pipeline as everything else: transcript → extraction → persisted rows.

The Android companion app (POST /v1/recordings) drops files into the same
directory, next to a ``<audio>.<ext>.json`` sidecar carrying what the phone
knows and a filename cannot: the call's true start with its UTC offset, the
direction, the contact's name, the number and the duration. When a sidecar is
there it wins outright; the filename guesswork below is the fallback for files
that arrive over Syncthing.

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
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from miya.services.people import find_by_phone, resolve_person

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".m4a", ".mp3", ".amr", ".wav", ".3ga", ".ogg", ".aac", ".opus"}

# Syncthing writes into hidden temp files before renaming; never touch them.
_SYNC_TEMP_MARKERS = ("~syncthing~", ".syncthing.", ".tmp", ".part")

# A file must be untouched this long before we trust that the sync finished.
MIN_FILE_AGE_SECONDS = 30

# The uploader stages bytes under this suffix and renames on completion; it is
# one of _SYNC_TEMP_MARKERS above, so a half-written upload is already
# invisible to the sweep without a second rule.
UPLOAD_TEMP_SUFFIX = ".part"

# Largest single recording POST /v1/recordings will spool to disk. An hour of
# AMR is ~2 MB and an hour of Samsung's m4a ~30 MB, so 200 MB is generous for
# a phone call and still small enough that a broken client cannot fill the
# volume before the cap trips. Nothing else in the stack imposes a body limit:
# uvicorn does not, there is no reverse proxy, and Content-Length is whatever
# the client claims — so the handler counts the bytes itself.
RECORDING_UPLOAD_MAX_BYTES = 200 * 1024 * 1024

# The longest a phone call is allowed to claim to be. It bounds the duration
# the phone reports, which is what transcription usage is billed against, so an
# unbounded value would corrupt the owner's only view of what MIYA costs.
MAX_CALL_SECONDS = 6 * 3600

# How long a `.part` file may sit untouched before the sweep decides the
# request that was writing it will never come back. A 200 MB upload over a bad
# 3G link takes minutes, not an hour.
STALE_UPLOAD_SECONDS = 3600

# How far apart two recordings sharing a call_id may be before we stop
# believing they are the same call. Android's CallLog._ID restarts from 1 when
# the user clears the call log's storage, so a call_id can be reused by a
# genuinely different call months later — and answering "duplicate" to that
# makes the phone delete a recording the server never saw.
CALL_ID_AGREEMENT_MINUTES = 10

# What Android's CallLog.Calls.TYPE means to us. Anything else (missed,
# rejected, voicemail) is a call with no conversation in it, so `na` is honest.
DIRECTION_BY_NAME = {
    "incoming": Direction.in_,
    "outgoing": Direction.out,
}


@dataclass(slots=True)
class ParsedRecording:
    """What is known about a recording before it is transcribed.

    A filename fills in the first three fields at best; a sidecar from the
    phone fills in all of them. Any field may be missing — the app must stay
    useful on a handset where READ_CALL_LOG cannot be granted at all.
    """

    counterparty: str | None = None  # contact name or phone number as recorded
    phone: str | None = None  # digits-only phone if the counterparty looks like one
    recorded_at: datetime | None = None
    direction: Direction | None = None  # who called whom; a filename never says
    duration_seconds: int | None = None  # true audio length, for usage accounting
    language: str | None = None  # locale hint → Scribe language_code
    call_id: str | None = None  # "<device_id>:<CallLog._ID>", survives re-encoding


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


def sidecar_path(audio: Path) -> Path:
    """``recording.m4a`` → ``recording.m4a.json``.

    Appended, not substituted, so the sidecar cannot collide with a second
    recording whose stem happens to match, and so the audio's own extension
    stays readable in the sidecar's name.
    """
    return audio.with_suffix(audio.suffix + ".json")


def staged_upload_paths(
    directory: Path, *, started_at: datetime, sha256: str, suffix: str
) -> tuple[Path, Path]:
    """Where an uploaded recording and its sidecar belong.

    Deterministic in the upload's own content, so a phone that retries after a
    lost response writes the identical name instead of a second copy. That
    makes `started_at` load-bearing: it must be the client's own value, never
    a server-side `now()`, or a handset with a wrong clock — the one case the
    correction exists for — would land on a new filename every single retry
    and stage a fresh copy of the same call each time. The corrected start
    belongs in the sidecar, which is what the sweep reads anyway.

    The client's filename never reaches the path — only its suffix does.
    """
    stem = (
        f"{started_at.year:04d}{started_at.month:02d}{started_at.day:02d}"
        f"-{started_at.hour:02d}{started_at.minute:02d}{started_at.second:02d}"
        f"-{sha256[:12]}"
    )
    audio = directory / f"{stem}{suffix}"
    return audio, sidecar_path(audio)


def read_sidecar(audio: Path) -> ParsedRecording | None:
    """The phone's own account of a call, or None when there is no sidecar.

    Every field is optional and a malformed value is dropped rather than
    raised: a recording that arrived is worth more than a metadata field, and
    the fallbacks (filename, mtime) are all still there underneath.
    """
    path = sidecar_path(audio)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Loud, because the fallback is bad: without the sidecar the direction,
        # duration, locale and the person link are all lost, and a staged
        # filename parses into a hash prefix that would reach the extractor as
        # the counterparty's name.
        log.warning("unreadable sidecar %s; falling back to the filename", path.name)
        return None
    if not isinstance(raw, dict):
        log.warning("sidecar %s is not an object; falling back", path.name)
        return None

    parsed = ParsedRecording(
        counterparty=raw.get("counterparty_name") or raw.get("phone_e164"),
        phone=re.sub(r"\D", "", raw.get("phone_e164") or "") or None,
        language=raw.get("locale") or None,
        call_id=raw.get("call_id") or None,
        direction=DIRECTION_BY_NAME.get(raw.get("direction") or ""),
    )
    started_at = raw.get("started_at")
    if isinstance(started_at, str):
        try:
            parsed.recorded_at = datetime.fromisoformat(started_at)
        except ValueError:
            parsed.recorded_at = None
    duration = raw.get("duration_seconds")
    # Clamped again here, not only at the API edge: this value bills the
    # transcription, and a sidecar written by an older server (or by hand)
    # never went through the edge's validation.
    if isinstance(duration, int | float) and 0 < duration <= MAX_CALL_SECONDS:
        parsed.duration_seconds = int(duration)
    return parsed


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
    if sidecar_path(path).exists():
        # An upload becomes visible under this name by os.replace, and only
        # after its sidecar is already on disk. The rename is atomic, so the
        # file is whole the instant it is seen — waiting out the quiet period
        # would only delay the owner's notification by half a minute.
        return True
    now = now if now is not None else time.time()
    return (now - stat.st_mtime) >= MIN_FILE_AGE_SECONDS


async def find_ingested(
    session: AsyncSession,
    sha256: str,
    *,
    call_id: str | None = None,
    occurred_at: datetime | None = None,
) -> int | None:
    """The interaction that already holds this recording, or None.

    Two independent keys, checked as two statements rather than one OR so the
    sweep's hot path keeps using ix_interactions_media_sha256: the hash is
    what the once-a-minute scan asks about, and it is index-backed. The
    call_id is the second key — it survives a re-encode, which the hash does
    not — but it is a phone-local counter, not a fingerprint, so it is only
    believed when the two recordings also agree about *when* the call was.

    That agreement matters because of what a "duplicate" verdict costs: the
    uploader answers 200 and the companion app then marks the row done and may
    delete the source file from the phone. Android's `CallLog._ID` restarts at
    1 when the call log's storage is cleared while the companion's device id
    survives (they are different apps), so without the time check the first
    call after a call-log wipe would be declared a duplicate of a months-old
    one and destroyed. With no time to compare against, the caller is trading
    nothing irreversible, and the bare call_id still applies.
    """
    found = await session.scalar(
        sa.select(Interaction.id)
        .where(Interaction.source == InteractionSource.phone_call)
        .where(Interaction.media["sha256"].astext == sha256)
        .limit(1)
    )
    if found is not None:
        return found
    if not call_id:
        return None
    stmt = (
        sa.select(Interaction.id)
        .where(Interaction.source == InteractionSource.phone_call)
        .where(Interaction.media["call_id"].astext == call_id)
    )
    if occurred_at is not None:
        window = timedelta(minutes=CALL_ID_AGREEMENT_MINUTES)
        stmt = stmt.where(
            Interaction.occurred_at.between(occurred_at - window, occurred_at + window)
        )
    return await session.scalar(stmt.limit(1))


async def already_ingested(
    session: AsyncSession,
    sha256: str,
    *,
    call_id: str | None = None,
    occurred_at: datetime | None = None,
) -> bool:
    """Have we ingested this recording before, under any name?"""
    return (
        await find_ingested(session, sha256, call_id=call_id, occurred_at=occurred_at)
        is not None
    )


async def remember_duplicate_path(
    session: AsyncSession, interaction_id: int, path: Path
) -> None:
    """Note that `path` is another copy of a recording we already ingested.

    A copy the database does not know about is a copy nothing can manage:
    `scan_directory` re-hashes it on every sweep forever, `purge_old_audio`
    refuses to delete a file no interaction claims, and an /unut of that person
    would leave it behind. Recording it under the interaction that owns the
    conversation puts it back under all three.
    """
    interaction = await session.get(Interaction, interaction_id)
    if interaction is None:
        return
    media = dict(interaction.media or {})
    known = list(media.get("duplicate_paths") or [])
    target = str(path)
    if target == media.get("path") or target == media.get("audio_path"):
        return
    if target in known:
        return
    known.append(target)
    media["duplicate_paths"] = known
    interaction.media = media


def _context_line(parsed: ParsedRecording) -> str | None:
    """The one line of context the extractor gets about who was on the call.

    It matters more than it looks: `apply_extraction` resolves people from the
    *model's output strings*, not from `interaction.person_id`, so a debt only
    lands on the right Person when the name reached the prompt. This used to be
    skipped whenever a phone number was parsed, which meant a call with a known
    contact gave the model less to go on than a call with a stranger.
    """
    who = parsed.counterparty or parsed.phone
    if not who:
        return None
    if parsed.direction is Direction.out:
        return f"[qo'ng'iroq → {who}]"
    if parsed.direction is Direction.in_:
        return f"[qo'ng'iroq ← {who}]"
    return f"[qo'ng'iroq: {who}]"


async def ingest_recording(
    session: AsyncSession, path: Path, *, meta: ParsedRecording | None = None
) -> IngestResult | None:
    """Push one audio file through the pipeline. None means already ingested.

    `meta` is what a caller already knows about the recording — the sweep
    passes the phone's sidecar. Supplied metadata is trusted outright: it comes
    from Android's call log, so there is nothing left to guess and nothing to
    second-guess it with.
    """
    # Off the event loop: a long recording must not stall the whole worker.
    sha256 = await asyncio.to_thread(file_sha256, path)

    parsed = meta or read_sidecar(path)
    trusted = parsed is not None
    if parsed is None:
        parsed = parse_filename(path)

    existing = await find_ingested(
        session, sha256, call_id=parsed.call_id, occurred_at=parsed.recorded_at
    )
    if existing is not None:
        log.debug("skipping already-ingested recording %s", path.name)
        await remember_duplicate_path(session, existing, path)
        return None

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=settings.tz)
    occurred_at = parsed.recorded_at or mtime
    # Samsung stamps the filename in the *phone's* local time. On a China trip
    # that is UTC+8 while we pin it to Tashkent (UTC+5) — three hours off,
    # sometimes across a report-day boundary. The file's mtime is absolute
    # (Syncthing preserves it), so when the two disagree by more than a long
    # call could explain, the mtime wins. A phone-supplied start carries a real
    # UTC offset, so there is nothing to drift and the guard is skipped.
    if not trusted and parsed.recorded_at is not None:
        drift = abs((mtime - parsed.recorded_at).total_seconds())
        if drift > 90 * 60:
            occurred_at = mtime

    person = None
    if trusted and parsed.counterparty:
        # A real contact name from the phone's address book: resolve_person
        # strips honorifics, matches fuzzily, learns the spelling as an alias
        # and backfills the number. A name guessed out of a filename gets none
        # of that — "random-audio" must never become a Person.
        person = await resolve_person(session, parsed.counterparty, phone=parsed.phone)
    elif parsed.phone:
        person = await find_by_phone(session, parsed.phone)

    interaction = await create_interaction(
        session,
        source=InteractionSource.phone_call,
        # A filename never says who called whom; Android's call log does.
        direction=parsed.direction or Direction.na,
        person_id=person.id if person else None,
        occurred_at=occurred_at,
        media={
            "type": "call_recording",
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": sha256,
            "counterparty": parsed.counterparty,
            "phone": parsed.phone,
            "call_id": parsed.call_id,
            "duration_seconds": parsed.duration_seconds,
            "processed": False,
        },
        meta={"filename": path.name},
    )

    text = await transcribe_into(
        session,
        interaction,
        path,
        language_hint=parsed.language,
        duration_hint=parsed.duration_seconds,
    )
    if text is None:
        # needs_review is already set; the hash row keeps the scan from
        # retrying a permanently broken file every minute. The owner reviews it.
        return IngestResult(interaction=interaction, applied=None, error="transcription")

    context = _context_line(parsed)
    if context:
        interaction.raw_text = context

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

    await asyncio.to_thread(sweep_stale_uploads, directory)

    # One query, not one hash per file per sweep: the phone pushes recordings
    # in continuously and hashing every settled file in a growing folder once a
    # minute is unbounded disk I/O. A path already in the database is already
    # ingested; the hash check below stays as the backstop for everything else
    # (a renamed copy, a Syncthing conflict file).
    known_paths = await _ingested_audio_paths(session)

    results: list[IngestResult] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or not is_ready_recording(path):
            continue
        if str(path) in known_paths:
            continue
        try:
            result = await ingest_recording(session, path)
        except Exception:
            log.exception("failed to ingest recording %s", path)
            await session.rollback()
            continue
        # Each recording is its own unit of work, and each Scribe call costs
        # money: commit per file so a crash later in the sweep (or in the
        # caller) can never roll back a transcription already paid for. Covers
        # the needs_review row of a failed file, and the duplicate-path note
        # that a None result may have left pending.
        await session.commit()
        if result is not None:
            results.append(result)
    return results


def sweep_stale_uploads(
    directory: Path, *, now: float | None = None, older_than: int = STALE_UPLOAD_SECONDS
) -> int:
    """Unlink `.part` files nobody is writing any more. Returns how many.

    A process killed mid-upload leaves its staging file behind, up to
    RECORDING_UPLOAD_MAX_BYTES of it. Nothing else would ever remove it: the
    sweep skips `.part`, and so does retention, because it is not an audio
    suffix. So the sweep that skips them is the right place to reap them.
    """
    now = now if now is not None else time.time()
    removed = 0
    for path in directory.rglob(f"*{UPLOAD_TEMP_SUFFIX}"):
        if not path.is_file():
            continue
        try:
            if now - path.stat().st_mtime < older_than:
                continue
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("could not remove abandoned upload %s", path)
    if removed:
        log.info("removed %d abandoned upload file(s)", removed)
    return removed


async def protected_audio_paths(session: AsyncSession) -> set[str]:
    """Paths whose audio is the only copy of unprocessed owner data.

    A file whose transcription failed (needs_review) or whose interaction was
    never processed must survive retention — deleting it would destroy raw
    input the owner has not seen yet, which the spec forbids.
    """
    unprocessed = sa.or_(
        Interaction.needs_review.is_(True),
        Interaction.processed.is_(False),
    )
    rows = await session.scalars(
        sa.select(Interaction.media["path"].astext).where(
            Interaction.media["path"].astext.isnot(None), unprocessed
        )
    )
    paths = {p for p in rows if p}
    extra = await session.scalars(_duplicate_paths_stmt().where(unprocessed))
    return paths | {p for p in extra if p}


def _duplicate_paths_stmt() -> sa.Select:
    """Every path recorded as a second copy of an already-ingested recording."""
    return sa.select(
        sa.func.jsonb_array_elements_text(Interaction.media["duplicate_paths"])
    ).where(Interaction.media["duplicate_paths"].isnot(None))


async def _ingested_audio_paths(session: AsyncSession) -> set[str]:
    """Every audio path the database has a row for (any source)."""
    rows = await session.execute(
        sa.select(
            Interaction.media["path"].astext,
            Interaction.media["audio_path"].astext,
        ).where(Interaction.media.isnot(None))
    )
    paths = {p for row in rows for p in row if p}
    # Second copies of a recording we already have. They belong here for both
    # of this set's readers: the sweep must not re-hash them every minute, and
    # retention must be allowed to delete them.
    extra = await session.scalars(_duplicate_paths_stmt())
    return paths | {p for p in extra if p}


async def purge_old_audio(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete audio older than AUDIO_RETENTION_DAYS from both media folders.

    Only files are removed — interactions and transcripts stay, and audio that
    is still the sole copy of unreviewed data is skipped. So is any file the
    database has never ingested: the first Syncthing sync can deliver months
    of historical recordings whose mtimes are already past retention, and
    deleting them before the scan job has worked through the backlog would
    destroy the archive unheard. Returns the number of files deleted.
    """
    now_ts = (now or datetime.now(settings.tz)).timestamp()
    cutoff = now_ts - settings.audio_retention_days * 86400
    protected = await protected_audio_paths(session)
    ingested = await _ingested_audio_paths(session)
    deleted = 0
    for directory in (Path(settings.call_recordings_dir), settings.media_dir):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            if str(path) in protected or str(path) not in ingested:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    # The sidecar names the counterparty and holds their phone
                    # number. Retaining it past the audio it describes would
                    # keep exactly the part retention exists to drop.
                    sidecar_path(path).unlink(missing_ok=True)
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
