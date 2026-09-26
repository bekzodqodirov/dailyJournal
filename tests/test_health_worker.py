"""Self-monitoring (build step 5): the worker's and the userbot's side.

The core (heartbeats, status, problems, the alert ledger) is covered in
test_health.py. Here: the scheduler listener that records every job's
outcome, the worker's own heartbeat, the health job's alert/recovery
discipline against quiet hours and an unreachable Telegram, the nightly
backup's copy to Telegram in pieces, and the userbot's beats. Telegram is
stubbed; the database is real.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot.formatting import clock, short_date
from miya.config import settings
from miya.db import models as m
from miya.services import backup, health
from miya.userbot import main as userbot
from miya.worker import main as worker

TZ = settings.tz
NOW = datetime(2026, 9, 15, 21, 40, tzinfo=TZ)
GB = 1024**3
# The backup under test is two hours old by the real clock, so it never ages
# into "stale" however long after writing the suite is run.
STAMP = (datetime.now(TZ) - timedelta(hours=2)).replace(second=0, microsecond=0)
STAMP_LABEL = f"{short_date(STAMP.date())} {clock(STAMP)}"


class _Bot:
    """Records what the owner would have received; Telegram can be made to
    fail for messages, for documents, or for both."""

    def __init__(self, *, reachable: bool = True, documents_ok: bool = True) -> None:
        self.sent: list[str] = []
        self.documents: list[tuple[str, dict]] = []
        self.reachable = reachable
        self.documents_ok = documents_ok

    async def send_message(self, chat_id, text, **kwargs) -> None:
        if not self.reachable:
            raise RuntimeError("telegram is down")
        self.sent.append(text)

    async def send_document(self, chat_id, document, **kwargs) -> None:
        if not self.reachable or not self.documents_ok:
            raise RuntimeError("document upload failed")
        # aiogram's FSInputFile keeps the path; that is what we assert on.
        self.documents.append((str(document.path), kwargs))


class _KeepEngine:
    """Stands in for the module's engine where a process would dispose it —
    the test's own session still needs the real pool."""

    async def dispose(self) -> None:
        return None


async def _rows(session) -> dict[str, m.Heartbeat]:
    return await health.beats(session)


async def _ledger(session, kind: str) -> list[datetime]:
    return list(
        await session.scalars(
            sa.select(m.ReminderLog.sent_at)
            .where(m.ReminderLog.kind == kind)
            .order_by(m.ReminderLog.sent_at)
        )
    )


async def _age_ledger(session, hours: int) -> None:
    """Pretend every alert/recovery row was written ``hours`` ago."""
    await session.execute(
        sa.update(m.ReminderLog)
        .where(
            sa.or_(
                m.ReminderLog.kind.like(f"{health.ALERT_KIND}%"),
                m.ReminderLog.kind.like(f"{health.RECOVERED_KIND}%"),
            )
        )
        .values(sent_at=m.ReminderLog.sent_at - timedelta(hours=hours))
    )
    await session.commit()


# --- the job listener -------------------------------------------------------


async def test_the_listener_records_a_successful_job(session):
    event = SimpleNamespace(job_id="backup", exception=None)

    future = worker._on_job_event(event, loop=asyncio.get_running_loop())
    await asyncio.wrap_future(future)

    rows = await _rows(session)
    assert rows["job:backup"].detail == {"ok": True, "error": None}
    assert rows["job:backup"].last_seen_at is not None


async def test_the_listener_records_a_failed_job_with_its_error(session):
    event = SimpleNamespace(job_id="windows", exception=RuntimeError("boom"))

    await asyncio.wrap_future(
        worker._on_job_event(event, loop=asyncio.get_running_loop())
    )
    rows = await _rows(session)
    assert rows["job:windows"].detail == {"ok": False, "error": "RuntimeError: boom"}

    # The next successful run replaces the record — the failure is not
    # merged into it.
    await worker.record_job_event("windows", ok=True, error=None)
    rows = await _rows(session)
    assert rows["job:windows"].detail == {"ok": True, "error": None}


async def test_the_listener_never_raises_when_the_row_cannot_be_written(
    session, monkeypatch
):
    async def _explode(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(worker.health, "beat", _explode)
    await worker.record_job_event("embed", ok=True, error=None)  # no raise
    assert "job:embed" not in await _rows(session)


# --- the worker's own heartbeat ---------------------------------------------


async def test_heartbeat_job_beats_the_worker_with_its_job_count(session):
    scheduler = SimpleNamespace(get_jobs=lambda: [1, 2, 3])

    await worker.heartbeat_job(scheduler)

    rows = await _rows(session)
    assert rows["worker"].detail == {"jobs": 3}
    assert health.component_of("worker", rows["worker"]).stale is False


async def test_the_scheduler_registers_the_monitoring_jobs_and_the_listener(
    monkeypatch,
):
    """run() wires heartbeat, health and the listener; the scheduler is
    stubbed so nothing actually ticks."""
    registered: list[str] = []
    listeners: list[tuple] = []

    class _Scheduler:
        def __init__(self, *args, **kwargs):
            pass

        def add_job(self, func, trigger, *, id, **kwargs):
            registered.append(id)

        def add_listener(self, callback, mask):
            listeners.append((callback, mask))

        def start(self):
            pass

        def get_jobs(self):
            return registered

        def shutdown(self, wait=False):
            pass

    class _StopAtOnce:
        def __init__(self):
            pass

        def set(self):
            pass

        async def wait(self):
            return None

    beats: list[tuple] = []

    async def _beat(session, component, *, detail=None, now=None):
        beats.append((component, detail))

    async def _no_catch_up(bot):
        return None

    class _FakeBot:
        def __init__(self, **kwargs):
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            return None

    monkeypatch.setattr(settings, "assistant_bot_token", "123:abc")
    monkeypatch.setattr(settings, "owner_telegram_id", 1)
    monkeypatch.setattr(worker, "AsyncIOScheduler", _Scheduler)
    monkeypatch.setattr(worker, "Bot", _FakeBot)
    monkeypatch.setattr(worker.asyncio, "Event", _StopAtOnce)
    monkeypatch.setattr(worker.health, "beat", _beat)
    monkeypatch.setattr(worker, "catch_up", _no_catch_up)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: None)

    monkeypatch.setattr(worker, "engine", _KeepEngine())

    await worker.run()

    assert "heartbeat" in registered
    assert "health" in registered
    [(callback, mask)] = listeners
    assert mask == worker.EVENT_JOB_EXECUTED | worker.EVENT_JOB_ERROR
    # One beat before the catch-up, carrying the job count.
    assert beats[0] == ("worker", {"jobs": len(registered)})


# --- the health job ---------------------------------------------------------


def _healthy_components() -> dict[str, health.Component]:
    return {
        name: health.component_of(
            name,
            m.Heartbeat(component=name, last_seen_at=NOW - timedelta(minutes=1)),
            now=NOW,
        )
        for name in health.COMPONENTS
    }


def _status(**overrides) -> health.Status:
    base = {
        "now": NOW,
        "components": _healthy_components(),
        "jobs": {},
        "userbot_last_message_at": NOW - timedelta(minutes=6),
        "db_ok": True,
        "db_size_bytes": 512 * 1024 * 1024,
        "disk_free_bytes": 34 * GB,
        "disk_total_bytes": 80 * GB,
        "disk_low": False,
        "backup": health.BackupInfo(
            path=Path("/data/backups/miya-20260915-033000.dump.age"),
            created_at=NOW - timedelta(hours=18),
            size=12 * 1024 * 1024,
            sent_to_telegram=True,
            sent_at=NOW - timedelta(hours=18),
            stale=False,
            configured=True,
        ),
        "windows_pending": 0,
        "windows_submitted": 0,
        "windows_failed": 0,
        "needs_review": 0,
        "claims_pending": 0,
        "cost_today_usd": Decimal("0.10"),
        "cost_month_usd": Decimal("3.50"),
        "anthropic_last_ok_at": NOW - timedelta(minutes=12),
        "anthropic_failing": False,
    }
    base.update(overrides)
    return health.Status(**base)


def _worker_silent() -> health.Status:
    components = _healthy_components()
    components["worker"] = health.component_of("worker", None, now=NOW)
    return _status(components=components)


def _serve(monkeypatch, status: health.Status) -> None:
    """The health job sees this status, whatever the database says."""

    async def _gather(session, *, now=None):
        return status

    monkeypatch.setattr(worker.health, "gather", _gather)


def _quiet(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: value)


async def test_a_warning_is_sent_once_and_again_after_alert_repeat_hours(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "alert_repeat_hours", 6)
    _serve(monkeypatch, _worker_silent())
    _quiet(monkeypatch, False)
    bot = _Bot()

    await worker.health_job(bot)
    assert len(bot.sent) == 1
    assert "Rejalashtiruvchi" in bot.sent[0]
    assert "docker compose restart worker" in bot.sent[0]
    assert len(await _ledger(session, "alert:worker_silent")) == 1

    # Still broken five minutes later: not repeated.
    await worker.health_job(bot)
    assert len(bot.sent) == 1

    # Seven hours on: said again.
    await _age_ledger(session, 7)
    await worker.health_job(bot)
    assert len(bot.sent) == 2
    assert len(await _ledger(session, "alert:worker_silent")) == 2


async def test_a_critical_problem_ignores_quiet_hours_and_a_warning_waits(
    session, monkeypatch
):
    components = _healthy_components()
    components["worker"] = health.component_of("worker", None, now=NOW)
    _serve(monkeypatch, _status(components=components, disk_low=True))
    _quiet(monkeypatch, True)
    bot = _Bot()

    await worker.health_job(bot)

    [text] = bot.sent
    assert "Diskda joy kam" in text
    assert await _ledger(session, "alert:disk_low")
    # The warning is not lost — it is still due once quiet hours end.
    assert await _ledger(session, "alert:worker_silent") == []
    _quiet(monkeypatch, False)
    await worker.health_job(bot)
    assert len(bot.sent) == 2
    assert "Rejalashtiruvchi" in bot.sent[1]


async def test_a_recovery_notice_goes_out_once_when_the_problem_clears(
    session, monkeypatch
):
    _quiet(monkeypatch, False)
    bot = _Bot()
    _serve(monkeypatch, _worker_silent())
    await worker.health_job(bot)
    assert len(bot.sent) == 1

    _serve(monkeypatch, _status())
    await worker.health_job(bot)
    assert len(bot.sent) == 2
    assert bot.sent[1] == health.recovery_text("worker_silent")
    assert "tiklandi" in bot.sent[1]
    assert len(await _ledger(session, "recovered:worker_silent")) == 1

    await worker.health_job(bot)
    assert len(bot.sent) == 2


async def test_a_recovery_notice_waits_for_quiet_hours_to_end(session, monkeypatch):
    _quiet(monkeypatch, False)
    bot = _Bot()
    _serve(monkeypatch, _worker_silent())
    await worker.health_job(bot)

    _quiet(monkeypatch, True)
    _serve(monkeypatch, _status())
    await worker.health_job(bot)
    assert len(bot.sent) == 1
    assert await _ledger(session, "recovered:worker_silent") == []

    _quiet(monkeypatch, False)
    await worker.health_job(bot)
    assert len(bot.sent) == 2
    assert "tiklandi" in bot.sent[1]


async def test_an_undelivered_alert_is_not_marked_and_is_retried(session, monkeypatch):
    _serve(monkeypatch, _worker_silent())
    _quiet(monkeypatch, False)
    bot = _Bot(reachable=False)

    await worker.health_job(bot)

    assert bot.sent == []
    assert await _ledger(session, "alert:worker_silent") == []

    bot.reachable = True
    await worker.health_job(bot)
    assert len(bot.sent) == 1
    assert len(await _ledger(session, "alert:worker_silent")) == 1


async def test_a_dead_database_is_reported_from_memory_not_the_ledger(
    session, monkeypatch
):
    """With the database gone the ledger is gone too: the critical alert
    still goes out, once per ALERT_REPEAT_HOURS, from an in-process memo."""
    monkeypatch.setattr(settings, "alert_repeat_hours", 6)
    worker._offline_alerted_at.clear()
    _serve(monkeypatch, _status(db_ok=False))
    _quiet(monkeypatch, True)
    bot = _Bot()

    await worker.health_job(bot)
    await worker.health_job(bot)

    [text] = bot.sent
    assert "Baza javob bermayapti" in text
    assert await _ledger(session, "alert:db_down") == []
    worker._offline_alerted_at["db_down"] -= timedelta(hours=7)
    await worker.health_job(bot)
    assert len(bot.sent) == 2
    worker._offline_alerted_at.clear()


# --- the backup's copy to Telegram ------------------------------------------


def _backup_file(tmp_path: Path, size: int) -> Path:
    path = tmp_path / f"miya-{STAMP:%Y%m%d-%H%M%S}{backup.BACKUP_SUFFIX}"
    path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return path


def _dump(monkeypatch, path: Path) -> None:
    async def _create(*, now=None):
        return backup.BackupResult(path=path, size=path.stat().st_size)

    monkeypatch.setattr(worker.backup, "create_backup", _create)
    monkeypatch.setattr(settings, "backup_dir", str(path.parent))
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    monkeypatch.setattr(settings, "backup_to_telegram", True)


async def test_a_small_backup_goes_out_as_one_document(session, monkeypatch, tmp_path):
    path = _backup_file(tmp_path, 2048)
    _dump(monkeypatch, path)
    _quiet(monkeypatch, True)
    bot = _Bot()

    await worker.backup_job(bot)

    [(sent_path, kwargs)] = bot.documents
    assert sent_path == str(path)
    assert kwargs["caption"] == f"🗄 Zaxira nusxa {STAMP_LABEL} · 2 KB · 1/1"
    assert kwargs["disable_notification"] is True
    assert bot.sent == []  # silence means it worked
    rows = await _rows(session)
    detail = rows[health.BACKUP_COMPONENT].detail
    assert detail["sent"] is True
    assert detail["path"] == str(path)
    assert detail["size"] == 2048
    assert datetime.fromisoformat(detail["sent_at"]).tzinfo is not None
    assert path.exists()
    assert sorted(tmp_path.iterdir()) == [path]


async def test_a_big_backup_goes_out_in_ordered_pieces_that_are_removed_after(
    session, monkeypatch, tmp_path
):
    """A 100 MB file is the real case; the part size is shrunk instead of
    writing 100 MB to disk in a test."""
    monkeypatch.setattr(worker.backup, "TELEGRAM_PART_BYTES", 1024)
    path = _backup_file(tmp_path, 3 * 1024 + 500)
    _dump(monkeypatch, path)
    bot = _Bot()

    await worker.backup_job(bot)

    assert [p for p, _ in bot.documents] == [f"{path}.part0{n}" for n in (1, 2, 3, 4)]
    captions = [kwargs["caption"] for _, kwargs in bot.documents]
    assert [c.rsplit(" ", 1)[1] for c in captions] == ["1/4", "2/4", "3/4", "4/4"]
    assert all(c.startswith(f"🗄 Zaxira nusxa {STAMP_LABEL} · ") for c in captions)
    assert sorted(tmp_path.iterdir()) == [path]  # pieces gone, backup kept
    rows = await _rows(session)
    assert rows[health.BACKUP_COMPONENT].detail["sent"] is True


async def test_a_failed_send_warns_with_the_path_and_records_it(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(worker.backup, "TELEGRAM_PART_BYTES", 1024)
    path = _backup_file(tmp_path, 2500)
    _dump(monkeypatch, path)
    _quiet(monkeypatch, False)
    bot = _Bot(documents_ok=False)

    await worker.backup_job(bot)

    [text] = bot.sent
    assert "Telegramga yuborilmadi" in text
    assert str(path) in text
    assert "document upload failed" in text
    assert path.exists()
    assert sorted(tmp_path.iterdir()) == [path]  # pieces removed on failure too
    rows = await _rows(session)
    detail = rows[health.BACKUP_COMPONENT].detail
    assert detail["sent"] is False
    assert detail["path"] == str(path)
    assert "document upload failed" in detail["error"]
    # Logged as the backup_failed alert so the health job does not say it
    # again five minutes later.
    assert len(await _ledger(session, "alert:backup_failed")) == 1


async def test_a_failed_send_inside_quiet_hours_is_left_to_the_health_job(
    session, monkeypatch, tmp_path
):
    path = _backup_file(tmp_path, 2048)
    _dump(monkeypatch, path)
    _quiet(monkeypatch, True)
    bot = _Bot(documents_ok=False)

    await worker.backup_job(bot)

    assert bot.sent == []
    rows = await _rows(session)
    assert rows[health.BACKUP_COMPONENT].detail["sent"] is False
    assert await _ledger(session, "alert:backup_failed") == []
    # The morning health sweep reads the record and says it.
    info = await health.gather(session, now=STAMP + timedelta(hours=1))
    assert info.backup.send_failed is True
    assert "backup_failed" in [p.key for p in health.problems(info)]


async def test_a_backup_past_max_parts_is_kept_and_the_owner_told(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(worker.backup, "TELEGRAM_PART_BYTES", 1024)
    monkeypatch.setattr(worker.backup, "MAX_PARTS", 2)
    path = _backup_file(tmp_path, 3 * 1024)
    _dump(monkeypatch, path)
    _quiet(monkeypatch, False)
    bot = _Bot()

    await worker.backup_job(bot)

    assert bot.documents == []
    [text] = bot.sent
    assert "Telegramga yuborilmadi" in text
    assert str(path) in text
    assert sorted(tmp_path.iterdir()) == [path]
    rows = await _rows(session)
    assert rows[health.BACKUP_COMPONENT].detail["sent"] is False


async def test_backup_to_telegram_false_sends_nothing(session, monkeypatch, tmp_path):
    path = _backup_file(tmp_path, 2048)
    _dump(monkeypatch, path)
    monkeypatch.setattr(settings, "backup_to_telegram", False)
    bot = _Bot()

    await worker.backup_job(bot)

    assert bot.documents == []
    assert bot.sent == []
    assert health.BACKUP_COMPONENT not in await _rows(session)


async def test_a_failed_dump_is_still_reported_and_nothing_is_sent(session, monkeypatch):
    async def _create(*, now=None):
        return backup.BackupResult(error="pg_dump: connection refused")

    monkeypatch.setattr(worker.backup, "create_backup", _create)
    monkeypatch.setattr(settings, "backup_to_telegram", True)
    bot = _Bot()

    await worker.backup_job(bot)

    [text] = bot.sent
    assert "muvaffaqiyatsiz" in text
    assert "connection refused" in text
    assert bot.documents == []


# --- the userbot's beats ----------------------------------------------------


async def test_the_idle_userbot_beats_once_as_disabled(session):
    assert await userbot.beat_userbot(enabled=False) is True

    rows = await _rows(session)
    assert rows["userbot"].detail == {"enabled": False}
    component = health.component_of("userbot", rows["userbot"])
    assert component.disabled is True


async def test_the_kill_switch_branch_writes_the_disabled_beat(session, monkeypatch):
    monkeypatch.setattr(settings, "userbot_enabled", False)
    idled = []

    async def _no_idle():
        idled.append(True)

    monkeypatch.setattr(userbot, "_idle_forever", _no_idle)
    monkeypatch.setattr(userbot, "engine", _KeepEngine())

    await userbot.run()

    assert idled
    rows = await _rows(session)
    assert rows["userbot"].detail == {"enabled": False}


async def test_a_connected_userbot_records_the_session_it_holds(session):
    await userbot.beat_userbot(enabled=True, connected=True, user_id=42)

    rows = await _rows(session)
    assert rows["userbot"].detail == {"enabled": True, "connected": True, "user": 42}
    assert health.component_of("userbot", rows["userbot"]).disabled is False


async def test_a_database_hiccup_never_stops_the_loop(monkeypatch):
    async def _explode(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(userbot.health, "beat", _explode)
    assert await userbot.beat_userbot(enabled=True, connected=False) is False


class _StopLoop(BaseException):
    """Ends approved_media_loop from inside a sweep: the loop swallows
    Exception, and cancelling it mid-heartbeat would leave a transaction
    open on the shared database."""


async def test_the_media_loop_beats_every_pass(session, monkeypatch):
    passes = 0

    async def _sweep(client):
        nonlocal passes
        passes += 1
        if passes >= 2:
            raise _StopLoop

    async def _noop(client):
        return None

    monkeypatch.setattr(userbot, "fetch_approved", _sweep)
    monkeypatch.setattr(userbot, "fetch_backfills", _noop)
    monkeypatch.setattr(userbot, "APPROVED_POLL_SECONDS", 0)
    client = SimpleNamespace(is_connected=lambda: False)

    with pytest.raises(_StopLoop):
        await userbot.approved_media_loop(client, user_id=7)
    assert passes == 2

    rows = await _rows(session)
    assert rows["userbot"].detail == {"enabled": True, "connected": False, "user": 7}
