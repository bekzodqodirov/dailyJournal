"""A weekly restore drill (WP-81): a backup is proven only when it restores.

The newest encrypted backup is restored into a scratch database, the row
counts are compared with what the backup job counted at dump time, and the
scratch database is dropped whatever happens. The outcome is the
``restore_drill`` heartbeat, which /holat shows and health.problems judges.
Blocking work (psql, age, pg_restore) runs in a thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from miya.config import settings
from miya.services import backup

log = logging.getLogger(__name__)

DRILL_DATABASE = "miya_restore_drill"
COMPONENT = "restore_drill"
# What the backup job counts at dump time, and the drill after restoring.
COUNTED_TABLES = ("people", "debts", "transactions", "interactions")
COUNTS_COMPONENT = "backup_counts"


@dataclass(slots=True)
class DrillResult:
    ok: bool
    skipped: str | None = None
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    expected: dict[str, int] | None = None
    seconds: float = 0.0
    path: str | None = None

    def detail(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error,
            "counts": self.counts,
            "expected": self.expected,
            "seconds": round(self.seconds, 1),
            "path": self.path,
        }


def _libpq(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def with_database(url: str, name: str) -> str:
    parts = urlsplit(_libpq(url))
    return urlunsplit(parts._replace(path=f"/{name}"))


def count_rows(url: str) -> dict[str, int]:
    """Row counts of the tables a restore must bring back."""
    import psycopg
    from psycopg import sql

    counts = {}
    with psycopg.connect(_libpq(url), connect_timeout=10) as conn:
        for table in COUNTED_TABLES:
            try:
                with conn.transaction():
                    row = conn.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                    ).fetchone()
            except psycopg.errors.UndefinedTable:
                continue
            counts[table] = int(row[0])
    return counts


def _admin(url: str, statement: str) -> None:
    import psycopg

    with psycopg.connect(_libpq(url), autocommit=True, connect_timeout=10) as conn:
        conn.execute(statement)


def _restore(file: Path, identity: Path, target: str) -> None:
    from miya.tools import restore

    code = restore.restore_into(target, file, identity)
    if code != restore.EXIT_OK:
        raise RuntimeError(f"restore exited with {code}")


def _drill_sync(file: Path, identity: Path, url: str) -> dict[str, int]:
    target = with_database(url, DRILL_DATABASE)
    _admin(url, f'DROP DATABASE IF EXISTS "{DRILL_DATABASE}"')
    _admin(url, f'CREATE DATABASE "{DRILL_DATABASE}"')
    try:
        _restore(file, identity, target)
        return count_rows(target)
    finally:
        try:
            _admin(url, f'DROP DATABASE IF EXISTS "{DRILL_DATABASE}" WITH (FORCE)')
        except Exception:
            log.exception("could not drop the drill database")


def _close(got: int | None, want: int) -> bool:
    """The counts are taken just after the dump, so a few rows may have
    arrived (or been purged) in between; a broken restore is far off."""
    if got is None:
        return False
    return abs(got - want) <= max(5, want // 100)


async def run(
    *, identity: Path | None = None, expected: dict | None = None
) -> DrillResult:
    """Restore the newest backup into a scratch database and compare."""
    from miya.services import health

    identity = identity or Path(settings.backup_identity_path)
    free, _ = health._disk_usage()
    if health._is_disk_low(free):
        return DrillResult(ok=True, skipped="disk_low")
    if not identity.is_file():
        log.warning("restore drill skipped: no key at %s", identity)
        return DrillResult(ok=True, skipped="no_key")
    newest = await backup.newest_backup()
    if newest is None:
        return DrillResult(ok=True, skipped="no_backup")
    started = time.monotonic()
    result = DrillResult(ok=False, path=str(newest), expected=expected)
    try:
        result.counts = await asyncio.to_thread(
            _drill_sync, newest, identity, settings.database_url
        )
        mismatch = {
            table: (want, result.counts.get(table))
            for table, want in (expected or {}).items()
            if not _close(result.counts.get(table), want)
        }
        if mismatch:
            result.error = "counts differ: " + ", ".join(
                f"{t} {want}≠{got}" for t, (want, got) in mismatch.items()
            )
        else:
            result.ok = True
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("restore drill failed: %s", result.error)
    result.seconds = time.monotonic() - started
    return result
