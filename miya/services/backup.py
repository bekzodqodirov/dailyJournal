"""Nightly encrypted database backup (spec §10).

``pg_dump`` piped into `age`, kept for ``BACKUP_RETENTION_DAYS``. The dump
contains every debt, message and transcript in the system, so it is never
written to disk in the clear: age encrypts it to the recipient public key in
``BACKUP_AGE_RECIPIENT`` before it lands.

With no recipient configured the job logs once and does nothing — an
unencrypted copy of this database is not an acceptable fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from miya.config import settings

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".sql.age"
_STAMP_RE = re.compile(r"(\d{8}-\d{6})")
PG_DUMP_TIMEOUT_SECONDS = 1800


@dataclass(slots=True)
class BackupResult:
    path: Path | None = None
    size: int = 0
    pruned: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.path is not None


def _dsn() -> str:
    """SQLAlchemy URL → libpq URL (pg_dump does not know the driver suffix)."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", settings.database_url)


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
    target = backup_dir() / f"miya-{now.strftime('%Y%m%d-%H%M%S')}{BACKUP_SUFFIX}"
    partial = target.with_suffix(target.suffix + ".partial")

    # pg_dump | age -r <recipient> -o <file>. The plaintext only ever exists
    # inside this pipe — it is never written to disk unencrypted.
    #
    # A real OS pipe, not `dump.stdout`: asyncio hands back a StreamReader,
    # which has no file descriptor to give the second process.
    read_fd, write_fd = os.pipe()
    try:
        dump = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            _dsn(),
            stdout=write_fd,
            stderr=asyncio.subprocess.PIPE,
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


def prune_old(*, now: datetime | None = None) -> int:
    """Delete backups older than the retention window. Returns how many."""
    now = now or datetime.now(settings.tz)
    cutoff = now - timedelta(days=settings.backup_retention_days)
    pruned = 0
    for path in backup_dir().glob(f"*{BACKUP_SUFFIX}"):
        match = _STAMP_RE.search(path.name)
        if match is None:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(
                tzinfo=settings.tz
            )
        except ValueError:
            continue
        if stamp < cutoff:
            try:
                path.unlink()
                pruned += 1
            except OSError:
                log.warning("could not prune old backup %s", path, exc_info=True)
    return pruned
