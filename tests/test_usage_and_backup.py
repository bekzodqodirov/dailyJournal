"""`/xarajat` cost reporting and the nightly encrypted backup (spec §9, §10)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from miya.bot import replies
from miya.bot.formatting import usd
from miya.config import settings
from miya.db import models as m
from miya.services import backup, queries

TZ = settings.tz


def _log(**kwargs) -> m.UsageLog:
    defaults = {
        "provider": "anthropic",
        "model": settings.extract_model,
        "operation": "extract",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "cost_usd": Decimal("0.002000"),
    }
    return m.UsageLog(**{**defaults, **kwargs})


# --- usage -------------------------------------------------------------------


async def test_usage_groups_by_operation_and_totals_the_spend(session):
    session.add_all(
        [
            _log(cost_usd=Decimal("0.002")),
            _log(cost_usd=Decimal("0.003")),
            _log(
                provider="elevenlabs",
                model="scribe_v1",
                operation="transcribe",
                input_tokens=0,
                output_tokens=0,
                audio_seconds=Decimal("120.00"),
                cost_usd=Decimal("0.007333"),
            ),
        ]
    )
    await session.flush()

    today = datetime.now(TZ).date()
    summary = await queries.usage_summary(session, today, today)

    assert len(summary.rows) == 2  # extract (2 calls) + transcribe
    extract = next(r for r in summary.rows if r.operation == "extract")
    assert extract.calls == 2
    assert extract.cost_usd == Decimal("0.005")
    assert summary.total_usd == Decimal("0.012333")
    assert summary.today_usd == summary.total_usd


async def test_usage_ignores_calls_outside_the_window(session):
    old = _log()
    session.add(old)
    await session.flush()
    old.created_at = datetime.now(TZ) - timedelta(days=40)
    await session.flush()

    today = datetime.now(TZ).date()
    summary = await queries.usage_summary(session, today, today)

    assert summary.rows == []
    assert summary.total_usd == Decimal("0")


async def test_the_cached_share_shows_prompt_caching_working(session):
    session.add(_log(input_tokens=200, cache_read_tokens=800))
    await session.flush()

    today = datetime.now(TZ).date()
    summary = await queries.usage_summary(session, today, today)

    assert summary.cached_share == 0.8


async def test_the_usage_report_reads_as_uzbek_money(session):
    session.add(_log(cost_usd=Decimal("1.230000")))
    await session.flush()
    today = datetime.now(TZ).date()

    body = replies.usage_report(await queries.usage_summary(session, today, today))

    assert "xabarlardan ajratish" in body
    assert "$1.23" in body


def test_an_empty_period_says_so():
    summary = queries.UsageSummary(
        date_from=datetime.now(TZ).date(), date_to=datetime.now(TZ).date()
    )
    assert "yozilmagan" in replies.usage_report(summary)


def test_fractions_of_a_cent_stay_visible():
    # money() would render this as "$0" and make the whole report useless.
    assert usd(Decimal("0.0034")) == "$0.0034"
    assert usd(Decimal("12.5")) == "$12.50"
    assert usd(Decimal("0")) == "$0.00"


# --- backup ------------------------------------------------------------------


async def test_no_recipient_means_no_backup_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))

    result = await backup.create_backup()

    assert result.ok is False
    assert result.error == "no_recipient"
    # Nothing was written — an unencrypted dump is never the fallback.
    assert list(tmp_path.iterdir()) == []


async def test_a_failing_dump_leaves_no_half_written_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_age_recipient", "age1invalid")
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg://nobody@127.0.0.1:1/nothing"
    )

    result = await backup.create_backup()

    assert result.ok is False
    assert not list(tmp_path.glob(f"*{backup.BACKUP_SUFFIX}"))
    assert not list(tmp_path.glob("*.partial"))


def test_the_dsn_drops_the_driver_and_hides_the_password(monkeypatch):
    """The password must reach pg_dump via PGPASSWORD, never argv —
    /proc/*/cmdline is world-readable."""
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg://miya:pw@db:5432/miya"
    )
    dsn, env = backup._dsn_and_env()
    assert dsn == "postgresql://miya@db:5432/miya"
    assert "pw" not in dsn
    assert env["PGPASSWORD"] == "pw"


def test_a_passwordless_dsn_passes_through_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://db:5432/miya")
    dsn, env = backup._dsn_and_env()
    assert dsn == "postgresql://db:5432/miya"
    assert "PGPASSWORD" not in env or env.get("PGPASSWORD") is None


def test_pruning_keeps_the_retention_window(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_retention_days", 14)
    now = datetime(2026, 8, 18, 3, 30, tzinfo=TZ)

    fresh = tmp_path / f"miya-20260817-033000{backup.BACKUP_SUFFIX}"
    stale = tmp_path / f"miya-20260701-033000{backup.BACKUP_SUFFIX}"
    unrelated = tmp_path / "notes.txt"
    for path in (fresh, stale, unrelated):
        path.write_bytes(b"x")

    pruned = backup.prune_old(now=now)

    assert pruned == 1
    assert fresh.exists()
    assert not stale.exists()
    assert unrelated.exists()  # only MIYA's own backups are touched


async def test_a_real_backup_round_trips_through_age(session, monkeypatch, tmp_path):
    """End to end: pg_dump | age → file → decrypt → a usable SQL dump.

    Skipped where the binaries or the database are missing; where they exist
    this is the only test that proves the pipe between the two processes is
    wired correctly.
    """
    import shutil
    import subprocess

    import pytest

    if not (
        shutil.which("age") and shutil.which("age-keygen") and shutil.which("pg_dump")
    ):
        pytest.skip("age and pg_dump are needed for the round-trip test")

    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = next(
        line.split(": ")[1].strip()
        for line in key.read_text().splitlines()
        if line.startswith("# public key:")
    )
    monkeypatch.setattr(settings, "backup_age_recipient", recipient)
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))

    result = await backup.create_backup()

    assert result.ok, result.error
    assert result.path.stat().st_mode & 0o777 == 0o600  # owner-only
    plaintext = subprocess.run(
        ["age", "-d", "-i", str(key), str(result.path)],
        check=True,
        capture_output=True,
    ).stdout.decode()
    assert "PostgreSQL database dump" in plaintext
    assert "CREATE TABLE public.debts" in plaintext


def test_a_file_without_a_timestamp_is_never_deleted(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    odd = tmp_path / f"manual-copy{backup.BACKUP_SUFFIX}"
    odd.write_bytes(b"x")

    assert backup.prune_old(now=datetime(2030, 1, 1, tzinfo=TZ)) == 0
    assert odd.exists()
