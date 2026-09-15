"""Self-monitoring core (build step 5): heartbeats, status, problems, alerts."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource, WindowStatus
from miya.services import backup, health

TZ = settings.tz
NOW = datetime(2026, 9, 15, 21, 40, tzinfo=TZ)
GB = 1024**3


# --- heartbeats -------------------------------------------------------------


async def test_beat_upserts_one_row_and_replaces_the_detail(session):
    await health.beat(session, "worker", detail={"jobs": 17}, now=NOW)
    await health.beat(
        session, "worker", detail={"other": True}, now=NOW + timedelta(minutes=1)
    )

    rows = await health.beats(session)

    assert list(rows) == ["worker"]
    assert rows["worker"].last_seen_at == NOW + timedelta(minutes=1)
    assert rows["worker"].detail == {"other": True}  # replaced, not merged
    count = await session.scalar(sa.select(sa.func.count()).select_from(m.Heartbeat))
    assert count == 1


async def test_beat_without_detail_stores_an_empty_dict(session):
    await health.beat(session, "api", detail={"x": 1}, now=NOW)
    await health.beat(session, "api", now=NOW)
    rows = await health.beats(session)
    assert rows["api"].detail == {}


async def test_beats_keys_every_component_and_job(session):
    await health.beat(session, "bot", now=NOW)
    await health.beat(session, "job:backup", detail={"ok": True}, now=NOW)

    rows = await health.beats(session)

    assert set(rows) == {"bot", "job:backup"}
    assert rows["job:backup"].detail == {"ok": True}


# --- Component thresholds ---------------------------------------------------


def _row(name: str, *, minutes: float, detail: dict | None = None) -> m.Heartbeat:
    return m.Heartbeat(
        component=name,
        last_seen_at=NOW - timedelta(minutes=minutes),
        detail=detail or {},
    )


def _silent(name: str, minutes: float = 30) -> health.Component:
    return health.component_of(name, _row(name, minutes=minutes), now=NOW)


def test_a_missing_heartbeat_is_stale_and_never_seen():
    component = health.component_of("worker", None, now=NOW)
    assert component.stale is True
    assert component.disabled is False
    assert component.last_seen_at is None
    assert component.age is None
    assert component.detail == {}


def test_components_use_the_general_threshold(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    fresh = health.component_of("worker", _row("worker", minutes=9), now=NOW)
    stale = health.component_of("worker", _row("worker", minutes=11), now=NOW)
    assert fresh.stale is False
    assert fresh.age == timedelta(minutes=9)
    assert stale.stale is True
    bot = health.component_of("bot", _row("bot", minutes=11), now=NOW)
    assert bot.stale is True


def test_the_userbot_has_its_own_shorter_fuse(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    monkeypatch.setattr(settings, "userbot_stale_minutes", 5)
    ok = health.component_of("userbot", _row("userbot", minutes=4), now=NOW)
    silent = health.component_of("userbot", _row("userbot", minutes=6), now=NOW)
    assert ok.stale is False
    assert silent.stale is True
    assert health.stale_after("userbot") == timedelta(minutes=5)
    assert health.stale_after("worker") == timedelta(minutes=10)


def test_a_userbot_switched_off_on_purpose_is_disabled_not_a_fault():
    row = _row("userbot", minutes=60 * 24, detail={"enabled": False})
    component = health.component_of("userbot", row, now=NOW)
    assert component.disabled is True
    assert component.stale is True  # it is old — but see problems()
    status = _status(components={**_healthy_components(), "userbot": component})
    assert "userbot_silent" not in {p.key for p in health.problems(status)}


def test_a_future_heartbeat_has_zero_age():
    row = _row("api", minutes=-3)
    assert health.component_of("api", row, now=NOW).age == timedelta(0)


# --- gather -----------------------------------------------------------------


async def test_gather_works_on_an_empty_database_and_no_backup_dir(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "missing" / "dir"))
    monkeypatch.setattr(settings, "backup_age_recipient", "")

    status = await health.gather(session, now=NOW)

    assert status.now == NOW
    assert status.db_ok is True
    assert status.db_size_bytes is not None and status.db_size_bytes > 0
    assert set(status.components) == set(health.COMPONENTS)
    assert all(c.stale for c in status.components.values())
    assert status.jobs == {}
    assert status.userbot_last_message_at is None
    assert (status.windows_pending, status.windows_submitted) == (0, 0)
    assert status.windows_failed == 0
    assert status.needs_review == 0
    assert status.claims_pending == 0
    assert status.cost_today_usd == Decimal("0")
    assert status.cost_month_usd == Decimal("0")
    assert status.anthropic_last_ok_at is None
    assert status.anthropic_failing is False
    assert status.backup.path is None
    assert status.backup.configured is False
    assert status.backup.stale is False  # nothing to expect without a recipient
    assert status.backup.sent_to_telegram is False
    assert status.disk_free_bytes is not None
    assert status.disk_total_bytes is not None


async def test_gather_reads_heartbeats_queue_spend_and_the_backup(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    monkeypatch.setattr(settings, "backup_max_age_hours", 30)
    monkeypatch.setattr(settings, "disk_min_free_gb", 0)

    await health.beat(session, "worker", detail={"jobs": 17}, now=NOW)
    await health.beat(
        session, "userbot", detail={"enabled": True, "connected": True}, now=NOW
    )
    await health.beat(session, "job:backup", detail={"ok": True}, now=NOW)
    await health.beat(
        session,
        "job:reminders",
        detail={"ok": False, "error": "boom"},
        now=NOW - timedelta(hours=1),
    )
    newest = tmp_path / f"miya-20260915-033000{backup.BACKUP_SUFFIX}"
    newest.write_bytes(b"x" * 10)
    (tmp_path / f"miya-20260914-033000{backup.BACKUP_SUFFIX}").write_bytes(b"x")
    await health.beat(
        session,
        "backup",
        detail={
            "sent": True,
            "sent_at": (NOW - timedelta(hours=18)).isoformat(),
            "path": str(newest),
            "size": 10,
        },
        now=NOW,
    )
    session.add_all(
        [
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.in_,
                occurred_at=NOW - timedelta(minutes=6),
                raw_text="salom",
            ),
            m.Interaction(
                source=InteractionSource.assistant_bot,
                direction=Direction.na,
                occurred_at=NOW - timedelta(minutes=1),
                raw_text="later, but not the userbot",
                needs_review=True,
            ),
            m.UsageLog(
                provider="anthropic",
                operation="extract",
                cost_usd=Decimal("0.25"),
                created_at=NOW - timedelta(minutes=12),
            ),
            m.UsageLog(
                provider="elevenlabs",
                operation="transcribe",
                cost_usd=Decimal("1.00"),
                created_at=NOW - timedelta(minutes=2),
            ),
            _window("w-pending", WindowStatus.pending),
            _window("w-submitted", WindowStatus.submitted),
            _window(
                "w-failed-old", WindowStatus.failed, created_at=NOW - timedelta(days=3)
            ),
        ]
    )
    await session.flush()

    status = await health.gather(session, now=NOW)

    assert status.components["worker"].stale is False
    assert status.components["worker"].detail == {"jobs": 17}
    assert status.components["bot"].stale is True
    assert set(status.jobs) == {"backup", "reminders"}
    assert status.jobs["reminders"].detail["error"] == "boom"
    assert status.userbot_last_message_at == NOW - timedelta(minutes=6)
    assert (status.windows_pending, status.windows_submitted) == (1, 1)
    assert status.windows_failed == 0  # three days old: outside the horizon
    assert status.needs_review == 1
    assert status.anthropic_last_ok_at == NOW - timedelta(minutes=12)
    assert status.anthropic_failing is False
    assert status.backup.path == newest
    assert status.backup.created_at == datetime(2026, 9, 15, 3, 30, tzinfo=TZ)
    assert status.backup.size == 10
    assert status.backup.configured is True
    assert status.backup.stale is False
    assert status.backup.sent_to_telegram is True
    assert status.backup.sent_at == NOW - timedelta(hours=18)
    assert status.backup.send_failed is False
    assert status.disk_low is False


async def test_gather_spend_is_today_and_this_month(session):
    today = datetime.now(TZ)
    session.add_all(
        [
            m.UsageLog(provider="anthropic", cost_usd=Decimal("0.5")),
            m.UsageLog(provider="anthropic", cost_usd=Decimal("0.25")),
        ]
    )
    await session.flush()

    status = await health.gather(session, now=today)

    assert status.cost_today_usd == Decimal("0.75")
    assert status.cost_month_usd == Decimal("0.75")


async def test_a_recent_failed_window_means_anthropic_is_failing(session):
    session.add(_window("w-failed", WindowStatus.failed, created_at=NOW))
    await session.flush()
    status = await health.gather(session, now=NOW)
    assert status.windows_failed == 1
    assert status.anthropic_failing is True


async def test_pending_windows_with_no_recent_success_means_failing(session):
    session.add(_window("w-pending", WindowStatus.pending))
    session.add(
        m.UsageLog(
            provider="anthropic",
            cost_usd=Decimal("0.1"),
            created_at=NOW - timedelta(hours=4),
        )
    )
    await session.flush()
    status = await health.gather(session, now=NOW)
    assert status.anthropic_failing is True
    status = await health.gather(session, now=NOW - timedelta(hours=2))
    assert status.anthropic_failing is False


async def test_a_backup_the_worker_could_not_send_is_flagged(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    newest = tmp_path / f"miya-20260915-033000{backup.BACKUP_SUFFIX}"
    newest.write_bytes(b"x")
    await health.beat(
        session,
        "backup",
        detail={"sent": False, "path": str(newest), "error": "Telegram unreachable"},
        now=NOW,
    )

    status = await health.gather(session, now=NOW)

    assert status.backup.sent_to_telegram is False
    assert status.backup.send_failed is True
    assert status.backup.send_error == "Telegram unreachable"
    assert {p.key for p in health.problems(status)} >= {"backup_failed"}


async def test_a_sent_record_for_an_older_file_does_not_cover_the_newest(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    older = tmp_path / f"miya-20260914-033000{backup.BACKUP_SUFFIX}"
    newest = tmp_path / f"miya-20260915-033000{backup.BACKUP_SUFFIX}"
    older.write_bytes(b"x")
    newest.write_bytes(b"x")
    await health.beat(
        session, "backup", detail={"sent": True, "path": str(older)}, now=NOW
    )

    status = await health.gather(session, now=NOW)

    assert status.backup.path == newest
    assert status.backup.sent_to_telegram is False
    assert status.backup.send_failed is False


async def test_a_missing_backup_is_stale_only_when_configured(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    status = await health.gather(session, now=NOW)
    assert status.backup.stale is True
    assert "backup_stale" in {p.key for p in health.problems(status)}

    (tmp_path / f"miya-20260913-033000{backup.BACKUP_SUFFIX}").write_bytes(b"x")
    status = await health.gather(session, now=NOW)
    assert status.backup.stale is True  # 2.75 days > 30 h

    (tmp_path / f"miya-20260915-033000{backup.BACKUP_SUFFIX}").write_bytes(b"x")
    status = await health.gather(session, now=NOW)
    assert status.backup.stale is False


def _window(custom_id: str, status: WindowStatus, *, created_at=None):
    window = m.ConversationWindow(
        tg_chat_id=1,
        started_at=NOW - timedelta(hours=1),
        ended_at=NOW - timedelta(minutes=30),
        message_count=1,
        char_count=5,
        text="[THEM] salom",
        status=status,
        custom_id=custom_id,
    )
    if created_at is not None:
        window.created_at = created_at
    return window


# --- problems() ---------------------------------------------------------------


def _healthy_components() -> dict[str, health.Component]:
    return {
        name: health.component_of(name, _row(name, minutes=1), now=NOW)
        for name in health.COMPONENTS
    }


def _backup_info(**overrides) -> health.BackupInfo:
    base = {
        "path": Path("/data/backups/miya-20260915-033000.dump.age"),
        "created_at": NOW - timedelta(hours=18),
        "size": 12 * 1024 * 1024,
        "sent_to_telegram": True,
        "sent_at": NOW - timedelta(hours=18),
        "stale": False,
        "configured": True,
    }
    base.update(overrides)
    return health.BackupInfo(**base)


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
        "backup": _backup_info(),
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


def _with(name: str, component: health.Component) -> dict[str, health.Component]:
    return {**_healthy_components(), name: component}


def _keys(status: health.Status) -> list[str]:
    return [p.key for p in health.problems(status)]


def test_a_healthy_status_has_no_problems():
    assert health.problems(_status()) == []


def test_every_problem_key_is_reachable_and_has_its_severity(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    monkeypatch.setattr(settings, "userbot_stale_minutes", 5)
    cases = {
        "db_down": _status(db_ok=False),
        "disk_low": _status(disk_low=True, disk_free_bytes=GB),
        "worker_silent": _status(components=_with("worker", _silent("worker"))),
        "userbot_silent": _status(components=_with("userbot", _silent("userbot"))),
        "api_silent": _status(components=_with("api", _silent("api"))),
        "backup_stale": _status(backup=_backup_info(stale=True)),
        "backup_failed": _status(
            backup=_backup_info(sent_to_telegram=False, send_failed=True, send_error="x")
        ),
        "anthropic_failing": _status(anthropic_failing=True, windows_pending=3),
        "review_backlog": _status(needs_review=health.REVIEW_BACKLOG_THRESHOLD),
    }
    assert set(cases) == set(health.PROBLEM_KEYS)
    for key, status in cases.items():
        found = health.problems(status)
        assert [p.key for p in found] == [key], key
        problem = found[0]
        expected = "critical" if key in {"db_down", "disk_low"} else "warning"
        assert problem.severity == expected, key
        assert problem.text.strip(), key


def test_problem_texts_name_the_remedy_in_uzbek():
    texts = {p.key: p.text for p in health.problems(_status(db_ok=False, disk_low=True))}
    assert "docker compose" in texts["db_down"]
    assert "Baza" in texts["db_down"]
    assert "Disk" in texts["disk_low"]

    never = health.component_of("worker", None, now=NOW)
    worker = health.problems(_status(components=_with("worker", never)))[0]
    assert "restart worker" in worker.text
    assert "hali bir marta ham" in worker.text

    quiet = _silent("userbot", minutes=25)
    userbot = health.problems(_status(components=_with("userbot", quiet)))[0]
    assert "25 daqiqadan beri jim" in userbot.text
    assert "userbot-login" in userbot.text


def test_a_down_database_hides_the_judgements_that_depend_on_it():
    nothing = {n: health.component_of(n, None, now=NOW) for n in health.COMPONENTS}
    status = _status(
        db_ok=False,
        components=nothing,
        backup=_backup_info(stale=True),
        anthropic_failing=True,
    )
    assert _keys(status) == ["db_down"]


def test_critical_problems_come_first():
    status = _status(db_ok=True, disk_low=True, backup=_backup_info(stale=True))
    assert _keys(status) == ["disk_low", "backup_stale"]


def test_backup_failed_also_comes_from_the_job_ledger():
    row = _row("job:backup", minutes=5, detail={"ok": False, "error": "pg_dump: x"})
    job = health.component_of("backup", row, now=NOW)
    problems = health.problems(_status(jobs={"backup": job}))
    assert [p.key for p in problems] == ["backup_failed"]
    assert "pg_dump: x" in problems[0].text
    # A stale backup outranks the send record: one problem, not two.
    stale = _status(jobs={"backup": job}, backup=_backup_info(stale=True))
    assert _keys(stale) == ["backup_stale"]


def test_the_review_backlog_threshold_is_inclusive():
    below = health.REVIEW_BACKLOG_THRESHOLD - 1
    assert _keys(_status(needs_review=below)) == []
    at = health.REVIEW_BACKLOG_THRESHOLD
    assert _keys(_status(needs_review=at)) == ["review_backlog"]


def test_dynamic_strings_in_problem_texts_are_escaped():
    info = _backup_info(
        path=Path("/data/<b>x</b>.dump.age"), sent_to_telegram=False, send_failed=True
    )
    text = health.problems(_status(backup=info))[0].text
    assert "<b>x</b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text


def test_recovery_texts_say_tiklandi():
    for key in health.PROBLEM_KEYS:
        text = health.recovery_text(key)
        assert text.startswith("✅"), key
        assert text.endswith("tiklandi"), key
    assert health.recovery_text("<odd>") == "✅ &lt;odd&gt; — tiklandi"


def test_size_label_uses_the_owners_units():
    assert health.size_label(None) == "?"
    assert health.size_label(0) == "0 MB"
    assert health.size_label(512 * 1024 * 1024) == "512 MB"
    assert health.size_label(34 * GB) == "34 GB"
    assert health.size_label(int(1.5 * GB)) == "1.5 GB"


# --- alert memory -------------------------------------------------------------


def _problem(key: str) -> health.Problem:
    return health.Problem(key, "warning", f"problem {key}")


async def test_a_new_problem_is_due_at_once(session):
    due, recovered = await health.alerts_due(
        session, [_problem("worker_silent")], now=NOW
    )
    assert [p.key for p in due] == ["worker_silent"]
    assert recovered == []


async def test_an_alerted_problem_waits_alert_repeat_hours(session, monkeypatch):
    monkeypatch.setattr(settings, "alert_repeat_hours", 6)
    problems = [_problem("worker_silent"), _problem("backup_stale")]
    health.mark_alerted(session, ["worker_silent"], now=NOW)
    await session.flush()

    due, recovered = await health.alerts_due(
        session, problems, now=NOW + timedelta(hours=1)
    )
    assert [p.key for p in due] == ["backup_stale"]
    assert recovered == []

    due, _ = await health.alerts_due(session, problems, now=NOW + timedelta(hours=7))
    assert [p.key for p in due] == ["worker_silent", "backup_stale"]


async def test_a_problem_that_cleared_is_reported_recovered_once(session):
    health.mark_alerted(session, ["userbot_silent"], now=NOW)
    await session.flush()

    due, recovered = await health.alerts_due(session, [], now=NOW + timedelta(hours=1))
    assert due == []
    assert recovered == ["userbot_silent"]

    health.mark_recovered(session, recovered, now=NOW + timedelta(hours=1))
    await session.flush()
    _, recovered = await health.alerts_due(session, [], now=NOW + timedelta(hours=2))
    assert recovered == []

    # It comes back: alerted again, newer than the recovery → recovers again.
    health.mark_alerted(session, ["userbot_silent"], now=NOW + timedelta(hours=3))
    await session.flush()
    _, recovered = await health.alerts_due(session, [], now=NOW + timedelta(hours=4))
    assert recovered == ["userbot_silent"]


async def test_a_problem_never_alerted_never_recovers(session):
    _, recovered = await health.alerts_due(session, [], now=NOW)
    assert recovered == []


async def test_a_still_present_problem_is_not_recovered(session):
    health.mark_alerted(session, ["disk_low"], now=NOW)
    await session.flush()
    due, recovered = await health.alerts_due(
        session, [_problem("disk_low")], now=NOW + timedelta(minutes=5)
    )
    assert due == []
    assert recovered == []


async def test_mark_helpers_write_ledger_rows_the_bot_and_worker_share(session):
    health.mark_alerted(session, ["worker_silent", "db_down"], now=NOW)
    health.mark_recovered(session, ["db_down"], now=NOW + timedelta(hours=1))
    await session.flush()

    rows = await session.execute(
        sa.select(m.ReminderLog.kind, m.ReminderLog.ref, m.ReminderLog.sent_at).order_by(
            m.ReminderLog.sent_at, m.ReminderLog.kind
        )
    )
    assert [tuple(r) for r in rows] == [
        (f"{health.ALERT_KIND}db_down", "db_down", NOW),
        (f"{health.ALERT_KIND}worker_silent", "worker_silent", NOW),
        (f"{health.RECOVERED_KIND}db_down", "db_down", NOW + timedelta(hours=1)),
    ]


async def test_mark_alerted_without_a_time_uses_the_database_clock(session):
    health.mark_alerted(session, ["api_silent"])
    await session.flush()
    sent_at = await session.scalar(sa.select(m.ReminderLog.sent_at))
    assert sent_at is not None
    assert datetime.now(TZ) - sent_at < timedelta(minutes=5)


def test_the_contract_names_are_fixed():
    assert health.COMPONENTS == ("bot", "worker", "userbot", "api")
    assert health.JOB_PREFIX == "job:"
    assert health.ALERT_KIND == "alert:"
    assert health.RECOVERED_KIND == "recovered:"
    assert m.Heartbeat.__tablename__ == "heartbeats"
    assert {c.name for c in m.Heartbeat.__table__.c} == {
        "component",
        "last_seen_at",
        "detail",
    }
    with pytest.raises(TypeError):
        health.Problem()  # key, severity and text are required
