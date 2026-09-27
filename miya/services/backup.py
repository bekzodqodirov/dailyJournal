"""Nightly encrypted database backup (spec §10, build step 5).

``pg_dump -Fc`` piped into `age`, kept for ``BACKUP_RETENTION_DAYS``. The dump
contains every debt, message and transcript in the system, so it is never
written to disk in the clear: age encrypts it to the recipient public key in
``BACKUP_AGE_RECIPIENT`` before it lands.

With no recipient configured the job logs once and does nothing — an
unencrypted copy of this database is not an acceptable fallback.

Format (build step 5): ``miya-<stamp>.dump.age`` is pg_dump's *custom*
archive (compressed, restored with ``pg_restore`` — see
``miya.tools.restore``) encrypted with age. Files from before this step are
``miya-<stamp>.sql.age`` (plain SQL for ``psql``); they are left exactly as
they are — never pruned, never counted as the current backup — until the
owner deletes them by hand. Restore one with
``age -d -i secrets/backup-key.txt miya-….sql.age | psql <DSN>``.

The nightly file also goes to the owner's Telegram (his decision): a
document is capped at 50 MB there, so ``split_for_telegram`` cuts a larger
backup into ``.partNN`` pieces that ``miya.tools.restore`` joins back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from miya.config import settings

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".dump.age"
# The pre-step-5 format: plain SQL. Recognised so nothing touches those files.
LEGACY_SUFFIX = ".sql.age"
_STAMP_RE = re.compile(r"(\d{8}-\d{6})")
_STAMP_FORMAT = "%Y%m%d-%H%M%S"
PG_DUMP_TIMEOUT_SECONDS = 1800

# Telegram accepts documents up to 50 MB from a bot; 45 MB leaves margin for
# the multipart envelope. A backup larger than MAX_PARTS pieces is not sent
# at all (the owner is told and the file stays on disk).
TELEGRAM_PART_BYTES = 45 * 1024 * 1024
MAX_PARTS = 10
_PART_RE = re.compile(r"\.part(\d{2,})$")
_CHUNK_BYTES = 1024 * 1024


class BackupTooLarge(Exception):
    """The backup would need more than MAX_PARTS Telegram documents."""


@dataclass(slots=True)
class BackupResult:
    path: Path | None = None
    size: int = 0
    pruned: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.path is not None


def libpq_dsn_and_env(url: str) -> tuple[str, dict[str, str]]:
    """SQLAlchemy URL → libpq URL plus environment for pg_dump / pg_restore.

    The password travels via PGPASSWORD, never argv: `/proc/*/cmdline` is
    world-readable, so a DSN with the password inline would show the database
    credentials to every process on the host for the duration of the dump.
    """
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    parsed = urlsplit(url)
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
        host = parsed.hostname or ""
        netloc = (f"{parsed.username}@" if parsed.username else "") + host
        if parsed.port:
            netloc += f":{parsed.port}"
        url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    return url, env


def _dsn_and_env() -> tuple[str, dict[str, str]]:
    """The configured database, ready for pg_dump."""
    return libpq_dsn_and_env(settings.database_url)


def backup_dir() -> Path:
    path = Path(settings.backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _run(command: list[str], **kwargs) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command, stderr=asyncio.subprocess.PIPE, **kwargs
    )
    _, stderr = await asyncio.wait_for(
        process.communicate(), timeout=PG_DUMP_TIMEOUT_SECONDS
    )
    return process.returncode, stderr or b""


async def create_backup(*, now: datetime | None = None) -> BackupResult:
    """Dump, encrypt, prune. Never raises — the caller is a scheduler job."""
    if not settings.backup_age_recipient:
        log.info("BACKUP_AGE_RECIPIENT is not set — skipping the encrypted backup")
        return BackupResult(error="no_recipient")

    now = now or datetime.now(settings.tz)
    try:
        directory = backup_dir()
    except OSError as exc:
        # mkdir can fail on a misconfigured volume. This function promises not
        # to raise — it is a scheduler job — so report instead.
        log.exception("backup directory is not usable")
        return BackupResult(error=f"backup directory unusable: {exc}")

    target = directory / f"miya-{now.strftime(_STAMP_FORMAT)}{BACKUP_SUFFIX}"
    partial = target.with_suffix(target.suffix + ".partial")

    # pg_dump -Fc | age -r <recipient> -o <file>. The plaintext only ever
    # exists inside this pipe — it is never written to disk unencrypted.
    # Custom format is compressed by pg_dump itself (a dump of transcripts
    # shrinks several-fold), which is what keeps the Telegram copy under the
    # document cap for a long time.
    #
    # A real OS pipe, not `dump.stdout`: asyncio hands back a StreamReader,
    # which has no file descriptor to give the second process.
    dsn, dump_env = _dsn_and_env()
    read_fd, write_fd = os.pipe()
    try:
        dump = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            dsn,
            stdout=write_fd,
            stderr=asyncio.subprocess.PIPE,
            env=dump_env,
        )
        encrypt = await asyncio.create_subprocess_exec(
            "age",
            "-r",
            settings.backup_age_recipient,
            "-o",
            str(partial),
            stdin=read_fd,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        os.close(read_fd)
        os.close(write_fd)
        partial.unlink(missing_ok=True)
        log.exception("backup could not start (pg_dump or age missing?)")
        return BackupResult(error=str(exc))

    # The children hold their own copies now; the parent's must go, or age
    # never sees EOF and both processes hang forever.
    os.close(read_fd)
    os.close(write_fd)

    try:
        _, encrypt_err = await asyncio.wait_for(
            encrypt.communicate(), timeout=PG_DUMP_TIMEOUT_SECONDS
        )
        dump_err = await dump.stderr.read()
        await dump.wait()
    except TimeoutError:
        encrypt.kill()
        dump.kill()
        await asyncio.gather(encrypt.wait(), dump.wait(), return_exceptions=True)
        partial.unlink(missing_ok=True)
        log.error("backup timed out after %ds", PG_DUMP_TIMEOUT_SECONDS)
        return BackupResult(error="timeout")

    if dump.returncode != 0 or encrypt.returncode != 0:
        partial.unlink(missing_ok=True)
        detail = (dump_err or encrypt_err).decode(errors="replace")[-400:]
        log.error("backup failed: %s", detail)
        return BackupResult(error=detail or "backup command failed")

    # Rename only after both processes succeeded, so a crashed run never
    # leaves behind a file that looks like a usable backup.
    partial.replace(target)
    os.chmod(target, 0o600)
    result = BackupResult(path=target, size=target.stat().st_size)
    result.pruned = prune_old(now=now)
    log.info(
        "backup written: %s (%d bytes), %d pruned",
        target.name,
        result.size,
        result.pruned,
    )
    return result


def backup_stamp(path: Path) -> datetime | None:
    """When a backup was taken, read from its name; None for a foreign file."""
    match = _STAMP_RE.search(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(
            tzinfo=settings.tz
        )
    except ValueError:
        return None


def _current_backups() -> list[Path]:
    """Every backup in the current format, oldest first by stamp.

    Only ``*.dump.age``: a legacy ``.sql.age``, a ``.partial`` still being
    written and a ``.partNN`` waiting to go to Telegram all fall outside the
    glob, so none of them is ever pruned or reported as "the newest backup".
    A file without a stamp in its name is not one of ours.
    """
    try:
        candidates = list(backup_dir().glob(f"*{BACKUP_SUFFIX}"))
    except OSError:
        log.warning("backup directory is not readable", exc_info=True)
        return []
    stamped = [(backup_stamp(p), p) for p in candidates]
    return [p for _stamp, p in sorted(s for s in stamped if s[0] is not None)]


async def newest_backup() -> Path | None:
    """The most recent ``*.dump.age`` on disk, or None. Never raises."""
    current = await asyncio.to_thread(_current_backups)
    return current[-1] if current else None


def prune_old(*, now: datetime | None = None) -> int:
    """Delete backups older than the retention window. Returns how many.

    Legacy ``.sql.age`` files are outside the glob and are never touched.
    """
    now = now or datetime.now(settings.tz)
    cutoff = now - timedelta(days=settings.backup_retention_days)
    pruned = 0
    try:
        candidates = list(backup_dir().glob(f"*{BACKUP_SUFFIX}"))
    except OSError:
        log.warning("backup directory is not readable; nothing pruned", exc_info=True)
        return 0
    for path in candidates:
        stamp = backup_stamp(path)
        if stamp is None:
            continue
        if stamp < cutoff:
            try:
                path.unlink()
                pruned += 1
            except OSError:
                log.warning("could not prune old backup %s", path, exc_info=True)
    return pruned


# --- Telegram copy ----------------------------------------------------------


def split_for_telegram(path: Path) -> list[Path]:
    """Cut one backup into pieces Telegram accepts; ``[path]`` when it fits.

    Pieces are ``<name>.part01`` … ``<name>.partNN`` next to the file, each
    TELEGRAM_PART_BYTES except the last, owner-only like the backup itself,
    written with fixed-size reads so a multi-hundred-megabyte file never sits
    in memory. Blocking I/O: the worker calls this through
    ``asyncio.to_thread``. Raises BackupTooLarge (before writing anything)
    past MAX_PARTS pieces. The caller removes the pieces with
    ``remove_parts`` once they are sent — or failed to send.
    """
    size = path.stat().st_size
    if size <= TELEGRAM_PART_BYTES:
        return [path]
    count = -(-size // TELEGRAM_PART_BYTES)
    if count > MAX_PARTS:
        raise BackupTooLarge(
            f"{path.name} is {size} bytes, {count} pieces; the cap is {MAX_PARTS}"
        )

    parts: list[Path] = []
    try:
        with path.open("rb") as source:
            for index in range(1, count + 1):
                part = path.with_name(f"{path.name}.part{index:02d}")
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                parts.append(part)
                remaining = TELEGRAM_PART_BYTES
                with os.fdopen(fd, "wb") as sink:
                    while remaining > 0:
                        chunk = source.read(min(_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        sink.write(chunk)
                        remaining -= len(chunk)
    except OSError:
        remove_parts(parts)
        raise
    return parts


def remove_parts(parts: list[Path]) -> None:
    """Delete the ``.partNN`` pieces; the backup itself is never touched."""
    for part in parts:
        if _PART_RE.search(part.name) is None:
            continue
        try:
            part.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", part, exc_info=True)


def part_number(path: Path) -> int | None:
    """``…part03`` → 3; None for a whole backup."""
    match = _PART_RE.search(path.name)
    return int(match.group(1)) if match else None
