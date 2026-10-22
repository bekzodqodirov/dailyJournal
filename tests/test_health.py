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


def _sheets(
    *, ok: bool = False, error: str = "http_403", failing_for: float = 15
) -> health.Component:
    detail = {
        "ok": ok,
        "error": None if ok else error,
        "last_ok_at": (NOW - timedelta(hours=2)).isoformat(),
        "failing_since": None
        if ok
        else (NOW - timedelta(minutes=failing_for)).isoformat(),
    }
    return health.Component(
        health.SHEETS_COMPONENT, NOW, timedelta(0), detail, stale=False, disabled=False
    )


def _jarvis(**overrides) -> health.JarvisHealth:
    """Jarvis's share of a healthy status (JV-21): Telegram sending works."""
    base = {
        "mode": "draft",
        "paused": False,
        "drafts": 4,
        "sent": 3,
        "escalations": 1,
        "daily_used": 2,
        "daily_cap": 40,
        "queued": 0,
        "backlog_stuck": 0,
        "leases_stuck": 0,
        "stuck_since": None,
        "unknown": 0,
        "cost_usd": Decimal("0.42"),
        "cost_cap_usd": Decimal("3.00"),
        "cost_unpriced": 0,
        "cost_capped": False,
        "channels": (
            health.ChannelSend("telegram", send_enabled=True),
            health.ChannelSend("whatsapp", send_enabled=False),
            health.ChannelSend("gmail", send_enabled=False),
        ),
    }
    base.update(overrides)
    return health.JarvisHealth(**base)


def _limited(until: datetime = NOW + timedelta(hours=24)) -> tuple:
    return (
        health.ChannelSend(
            "telegram",
            send_enabled=True,
            breaker_until=until,
            breaker_reason="peer_flood",
        ),
        health.ChannelSend("whatsapp", send_enabled=False),
        health.ChannelSend("gmail", send_enabled=False),
    )


@pytest.fixture
def jarvis_on(monkeypatch):
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "telegram_send_enabled", True)


def test_every_problem_key_is_reachable_and_has_its_severity(monkeypatch, jarvis_on):
    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    monkeypatch.setattr(settings, "userbot_stale_minutes", 5)
    monkeypatch.setattr(settings, "sheets_enabled", True)
    cases = {
        "db_down": _status(db_ok=False),
        "disk_low": _status(disk_low=True, disk_free_bytes=GB),
        "worker_silent": _status(components=_with("worker", _silent("worker"))),
        "userbot_silent": _status(components=_with("userbot", _silent("userbot"))),
        "api_silent": _status(components=_with("api", _silent("api"))),
        "backup_unconfigured": _status(backup=_backup_info(configured=False)),
        "backup_stale": _status(backup=_backup_info(stale=True)),
        "backup_failed": _status(
            backup=_backup_info(sent_to_telegram=False, send_failed=True, send_error="x")
        ),
        "anthropic_failing": _status(anthropic_failing=True, windows_pending=3),
        "review_backlog": _status(needs_review=health.REVIEW_BACKLOG_THRESHOLD),
        "spend_high": _status(cost_today_usd=Decimal("5")),
        "api_restarting": _status(api_starts_last_hour=3),
        "search_down": _status(embed_backlog=1, embed_oldest_at=NOW - timedelta(hours=2)),
        "phone_silent": _status(phone_seen=_phone("phone_seen", hours=5)),
        "restore_drill_failed": _status(
            restore_drill=health.Component(
                "restore_drill", NOW, None, {"ok": False, "error": "x"}, False, False
            )
        ),
        "sheets_failing": _status(sheets=_sheets()),
        "phone_stream_off": _status(
            phone_app=_phone(
                "phone_app",
                hours=0,
                wanted={"sms": True},
                granted={"sms": False},
                ever_granted={"sms": True},
            )
        ),
        # JV-21: Jarvis's own, each only with its switch on.
        "outbox_stuck": _status(
            jarvis=_jarvis(
                queued=2, backlog_stuck=2, stuck_since=NOW - timedelta(minutes=25)
            )
        ),
        "telegram_send_limited": _status(jarvis=_jarvis(channels=_limited())),
        "jarvis_cost_cap": _status(
            jarvis=_jarvis(cost_usd=Decimal("3.10"), cost_capped=True)
        ),
    }
    assert set(cases) == set(health.PROBLEM_KEYS)
    for key, status in cases.items():
        found = health.problems(status)
        assert [p.key for p in found] == [key], key
        problem = found[0]
        expected = "critical" if key in {"db_down", "disk_low"} else "warning"
        assert problem.severity == expected, key
        assert problem.text.strip(), key


def test_sheets_failing_only_when_enabled_and_failing_for_a_while(monkeypatch):
    monkeypatch.setattr(settings, "sheets_enabled", True)
    [problem] = health.problems(_status(sheets=_sheets()))
    assert problem.key == "sheets_failing" and problem.severity == "warning"
    assert problem.text == (
        "📊 Google Sheets yangilanmayapti: jadvalga ruxsat yo'q (403). Jadval xizmat "
        "hisobiga «Muharrir» sifatida ulashilganini tekshiring."
    )
    # One failure a few minutes old is not worth the owner's attention yet.
    assert _keys(_status(sheets=_sheets(failing_for=3))) == []
    assert _keys(_status(sheets=_sheets(ok=True))) == []
    assert _keys(_status(sheets=None)) == []
    # An unknown code is shown escaped, as it is.
    odd = health.problems(_status(sheets=_sheets(error="<Odd>")))[0]
    assert "&lt;Odd&gt;" in odd.text
    monkeypatch.setattr(settings, "sheets_enabled", False)
    assert _keys(_status(sheets=_sheets())) == []
    assert health.recovery_text("sheets_failing") == (
        "✅ Google Sheets bilan sinxronlash tiklandi"
    )


async def test_gather_reads_the_sheets_beat(session):
    await health.beat(
        session, health.SHEETS_COMPONENT, detail={"ok": True, "last_ok_at": None}, now=NOW
    )
    status = await health.gather(session, now=NOW)
    assert status.sheets is not None and status.sheets.detail["ok"] is True
    assert health.SHEETS_COMPONENT not in status.jobs


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


# --- an unconfigured backup (WP-04) ------------------------------------------


def test_an_unconfigured_backup_is_a_warning_not_silence():
    status = _status(backup=_backup_info(configured=False, stale=False, created_at=None))
    [problem] = health.problems(status)
    assert problem.key == "backup_unconfigured"
    assert problem.severity == "warning"
    assert "make backup-key" in problem.text
    assert "backup_stale" not in _keys(status)


def test_a_configured_backup_is_never_reported_unconfigured():
    for info in (
        _backup_info(),
        _backup_info(stale=True),
        _backup_info(send_failed=True),
    ):
        assert "backup_unconfigured" not in _keys(_status(backup=info))


def test_the_unconfigured_backup_has_a_recovery_line():
    assert (
        health.recovery_text("backup_unconfigured")
        == "✅ Zaxira nusxa sozlandi — tiklandi"
    )


# --- WP-25: spend alarms ---------------------------------------------------------


def test_spend_high_at_the_daily_threshold_not_below():
    assert "spend_high" not in [
        p.key for p in health.problems(_status(cost_today_usd=Decimal("4.99")))
    ]
    [problem] = health.problems(_status(cost_today_usd=Decimal("5.00")))
    assert problem.key == "spend_high" and "kunlik chegara" in problem.text


def test_spend_high_monthly_branch_and_zero_disables(monkeypatch):
    [problem] = health.problems(_status(cost_month_usd=Decimal("60")))
    assert "oylik chegara" in problem.text
    monkeypatch.setattr(settings, "spend_alert_monthly_usd", Decimal("0"))
    monkeypatch.setattr(settings, "spend_alert_daily_usd", Decimal("0"))
    assert (
        health.problems(
            _status(cost_today_usd=Decimal("99"), cost_month_usd=Decimal("999"))
        )
        == []
    )
    assert (
        health._RECOVERY["spend_high"] == "✅ API xarajati yana chegara ichida — tiklandi"
    )


# --- WP-26: restart loop and dead search -----------------------------------------


def test_the_search_threshold():
    fresh = _status(embed_backlog=1, embed_oldest_at=NOW - timedelta(minutes=10))
    assert health.problems(fresh) == []
    stale = _status(embed_backlog=1, embed_oldest_at=NOW - timedelta(hours=2))
    assert [p.key for p in health.problems(stale)] == ["search_down"]
    assert health.problems(_status(api_starts_last_hour=2)) == []


async def test_api_starts_are_counted_within_the_hour(session):
    now = datetime.now(settings.tz)
    for minutes in (5, 20, 40):
        session.add(
            m.ReminderLog(
                kind=health.API_START_KIND,
                ref="v",
                sent_at=now - timedelta(minutes=minutes),
            )
        )
    for hours in (2, 3, 4):
        session.add(
            m.ReminderLog(
                kind=health.API_START_KIND, ref="v", sent_at=now - timedelta(hours=hours)
            )
        )
    await session.flush()
    status = await health.gather(session, now=now)
    assert status.api_starts_last_hour == 3
    assert "api_restarting" in [p.key for p in health.problems(status)]


# --- phone liveness (WP-66) -------------------------------------------------


def _phone(name: str, *, hours: float, now=NOW, **detail) -> health.Component:
    seen = now - timedelta(hours=hours)
    return health.Component(
        name, seen, timedelta(hours=hours), dict(detail), stale=False, disabled=False
    )


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "23:30-07:30")
    monkeypatch.setattr(settings, "phone_silent_hours", 4)


def test_awake_between_skips_quiet_hours(quiet):
    assert health.awake_between(_at(1, 23), _at(2, 7, 40)) == timedelta(minutes=40)
    assert health.awake_between(_at(1, 7), _at(1, 12)) == timedelta(hours=4, minutes=30)
    assert health.awake_between(_at(1, 10), _at(1, 14, 30)) == timedelta(
        hours=4, minutes=30
    )
    # Two nights: only the day parts count.
    assert health.awake_between(_at(1, 12), _at(3, 12)) == timedelta(hours=32)
    assert health.awake_between(_at(1, 12), _at(1, 11)) == timedelta(0)


def test_phone_silent_after_four_awake_hours(quiet):
    now = _at(15, 16, 0)
    raised = _status(now=now, phone=_phone("phone", hours=4 + 1 / 60, now=now))
    calm = _status(now=now, phone=_phone("phone", hours=4 - 1 / 60, now=now))
    assert "phone_silent" in _keys(raised)
    assert "phone_silent" not in _keys(calm)


def test_the_night_does_not_count_as_silence(quiet):
    now = _at(16, 7, 45)
    status = _status(now=now, phone_seen=_phone("phone_seen", hours=8.75, now=now))
    assert "phone_silent" not in _keys(status)


def test_phone_silent_is_off_at_zero_and_without_a_phone(quiet, monkeypatch):
    assert "phone_silent" not in _keys(_status())
    monkeypatch.setattr(settings, "phone_silent_hours", 0)
    assert "phone_silent" not in _keys(_status(phone=_phone("phone", hours=30)))


def test_a_revoked_stream_is_named(quiet):
    status = _status(
        phone_app=_phone(
            "phone_app",
            hours=0,
            wanted={"sms": True, "call_log": True},
            granted={"sms": False, "call_log": True},
            ever_granted={"sms": True, "call_log": True},
        )
    )
    [problem] = [p for p in health.problems(status) if p.key == "phone_stream_off"]
    assert "SMS" in problem.text


def test_a_never_granted_stream_is_shown_not_alerted(quiet):
    from miya.bot import replies

    status = _status(
        phone_app=_phone(
            "phone_app",
            hours=0,
            wanted={"sms": True},
            granted={"sms": False},
            ever_granted={},
        )
    )
    assert "phone_stream_off" not in _keys(status)
    line = replies._phone_line(status)
    assert line.startswith("📱 Telefon — oxirgi aloqa: hozirgina")
    assert line.endswith(" · ruxsat berilmagan: SMS")


def test_the_holat_line_shows_silence(quiet):
    from miya.bot import replies

    status = _status(
        phone=_phone("phone", hours=6), phone_seen=_phone("phone_seen", hours=5)
    )
    assert replies._phone_line(status).startswith("⚠️ Telefon — 5 soatdan beri jim · ")


# --- Jarvis (JV-21) ---------------------------------------------------------------


def test_jarvis_problem_texts_and_their_recoveries(jarvis_on):
    from miya.bot import replies

    stuck = _status(
        jarvis=_jarvis(
            queued=2,
            backlog_stuck=1,
            leases_stuck=1,
            stuck_since=NOW - timedelta(minutes=25),
        )
    )
    [problem] = health.problems(stuck)
    assert problem.text == (
        "📤 2 ta xabar 25 daqiqadan beri yuborilmay turibdi. "
        "Userbot ishlayotganini tekshiring."
    )
    # The repeats are the very notices their own jobs send first.
    [limited] = health.problems(_status(jarvis=_jarvis(channels=_limited())))
    assert limited.text == replies.JARVIS_TELEGRAM_LIMITED == health.TELEGRAM_SEND_LIMITED
    [capped] = health.problems(_status(jarvis=_jarvis(cost_capped=True)))
    assert capped.text == replies.JARVIS_COST_STOP == health.JARVIS_COST_CAP
    assert health.recovery_text("outbox_stuck") == "✅ Xabar yuborish tiklandi"
    assert (
        health.recovery_text("telegram_send_limited") == "✅ Telegram yuborish tiklandi"
    )
    assert health.recovery_text("jarvis_cost_cap") == (
        "✅ Jarvis xarajat chegarasi tiklandi"
    )
    assert set(health.JARVIS_PROBLEM_KEYS) <= set(health.PROBLEM_KEYS)
    assert {"telegram_send_limited", "jarvis_cost_cap"} == health.TOLD_FIRST_ELSEWHERE


def test_disabled_feature_no_problem(monkeypatch):
    """Rule 20: with JARVIS_ENABLED off nothing Jarvis is judged or shown,
    whatever the status carries; the Telegram limit also needs
    TELEGRAM_SEND_ENABLED."""
    from miya.bot import replies

    broken = _status(
        jarvis=_jarvis(
            channels=_limited(),
            backlog_stuck=3,
            stuck_since=NOW - timedelta(hours=1),
            cost_capped=True,
        )
    )
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    monkeypatch.setattr(settings, "telegram_send_enabled", True)
    assert _keys(broken) == []
    assert replies.jarvis_status_lines(broken) is None
    report = replies.status_report(broken, health.problems(broken))
    assert (
        "Jarvis" not in report and "📤" not in report and "Javob beruvchi" not in report
    )
    assert not any(health.feature_on(key) for key in health.JARVIS_PROBLEM_KEYS)

    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "telegram_send_enabled", False)
    assert _keys(broken) == ["outbox_stuck", "jarvis_cost_cap"]
    assert not health.feature_on("telegram_send_limited")
    assert health.feature_on("outbox_stuck")
    # A status gathered with Jarvis off carries nothing Jarvis at all.
    assert _keys(_status()) == []


async def test_disabled_feature_never_reads_or_recovers(session, monkeypatch):
    from miya.jarvis import status as jarvis_status

    calls: list[datetime] = []

    async def _facts(session, now, **kwargs):
        calls.append(now)
        raise AssertionError("Jarvis read with JARVIS_ENABLED off")

    monkeypatch.setattr(jarvis_status, "health_facts", _facts)
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    status = await health.gather(session, now=NOW)
    assert calls == []
    assert status.jarvis is None and status.jarvis_unreadable is False
    # An episode alerted while Jarvis was on is not announced as recovered
    # once the switch is off.
    health.mark_alerted(session, ["telegram_send_limited"], now=NOW - timedelta(hours=1))
    await session.flush()
    _due, recovered = await health.alerts_due(session, [], now=NOW)
    assert recovered == []


async def test_a_failed_jarvis_read_is_one_line_not_a_failed_status(
    session, monkeypatch, jarvis_on
):
    from miya.bot import replies
    from miya.jarvis import status as jarvis_status

    async def _broken(session, now, **kwargs):
        await session.execute(sa.text("SELECT no_such_column FROM outbox_messages"))

    monkeypatch.setattr(jarvis_status, "health_facts", _broken)
    status = await health.gather(session, now=NOW)
    assert status.db_ok is True
    assert status.jarvis is None and status.jarvis_unreadable is True
    assert replies.jarvis_status_lines(status) == [replies.JARVIS_HEALTH_UNREADABLE]
    assert not set(_keys(status)) & set(health.JARVIS_PROBLEM_KEYS)
    # The savepoint kept the session usable: the rest of the status is whole
    # and the same session reads on.
    assert status.db_size_bytes is not None
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Heartbeat)) == 0


async def test_a_failed_jarvis_read_never_announces_a_recovery(
    session, monkeypatch, jarvis_on
):
    """A state that was not read is not a recovery: an open Telegram limit
    stays open in the ledger, and is judged again once the read works."""
    from miya.jarvis import status as jarvis_status

    health.mark_alerted(
        session,
        ["telegram_send_limited", "outbox_stuck", "jarvis_cost_cap"],
        now=NOW - timedelta(hours=1),
    )
    await session.flush()

    async def _broken(session, now, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jarvis_status, "health_facts", _broken)
    status = await health.gather(session, now=NOW)
    assert status.jarvis_unreadable is True
    assert health.unjudged(status) == frozenset(health.JARVIS_PROBLEM_KEYS)
    found = health.problems(status)
    _due, recovered = await health.alerts_due(
        session, found, now=NOW, unjudged=health.unjudged(status)
    )
    assert not set(recovered) & set(health.JARVIS_PROBLEM_KEYS)
    # A status that read Jarvis judges it again.
    healthy = _status(jarvis=_jarvis())
    assert health.unjudged(healthy) == frozenset()
    _due, recovered = await health.alerts_due(
        session, [], now=NOW, unjudged=health.unjudged(healthy)
    )
    assert set(health.JARVIS_PROBLEM_KEYS) <= set(recovered)


# The DB tests below run on the real clock: the outbox and the ledger stamp
# their rows with the database's now().


def _outbox(
    *,
    state: str,
    key: str,
    channel: str = "telegram",
    **fields,
) -> m.OutboxMessage:
    return m.OutboxMessage(
        channel=channel,
        chat_key=f"tg:{abs(hash(key)) % 100000}",
        origin="owner_command",
        authored_by="owner",
        body="Salom",
        body_sha256="0" * 64,
        state=state,
        idempotency_key=key,
        **fields,
    )


async def _trip(session, now: datetime, *, until: datetime) -> None:
    from miya.jarvis import policy

    await policy.trip_breaker(
        session,
        channel="telegram",
        reason="peer_flood",
        until=until,
        by="adapter",
        now=now,
    )


async def test_gather_reads_jarvis_facts(session, jarvis_on, monkeypatch):
    from miya.services import reminders

    monkeypatch.setattr(settings, "jarvis_daily_cost_cap_usd", Decimal("3.00"))
    now = datetime.now(TZ)
    session.add_all(
        [
            # Due 25 minutes ago and nobody claimed it: stuck.
            _outbox(
                state="approved",
                key="stuck",
                approved_by="button",
                approved_at=now - timedelta(minutes=25),
            ),
            # Approved a minute ago: queued, not stuck.
            _outbox(
                state="approved",
                key="fresh",
                approved_by="button",
                approved_at=now - timedelta(minutes=1),
            ),
            # Claimed 20 minutes ago, the lease long run out, never reconciled.
            _outbox(
                state="sending",
                key="lease",
                approved_by="button",
                approved_at=now - timedelta(minutes=21),
                lease_until=now - timedelta(minutes=15),
            ),
            _outbox(state="unknown", key="doubt", approved_by="button"),
            _outbox(
                state="sent",
                key="sent",
                approved_by="button",
                sent_at=now - timedelta(minutes=3),
            ),
            m.UsageLog(
                provider="anthropic", operation="jarvis_draft", cost_usd=Decimal("0.25")
            ),
            m.UsageLog(provider="anthropic", operation="extract", cost_usd=Decimal("9")),
        ]
    )
    await session.flush()
    status = await health.gather(session, now=now)
    jarvis = status.jarvis
    assert jarvis is not None and status.jarvis_unreadable is False
    assert jarvis.mode == "shadow" and jarvis.paused is False
    assert jarvis.queued == 2
    assert jarvis.backlog_stuck == 1 and jarvis.leases_stuck == 1
    assert jarvis.stuck_since is not None
    assert abs(jarvis.stuck_since - (now - timedelta(minutes=25))) < timedelta(seconds=1)
    assert jarvis.unknown == 1
    # The sent row and the claimed one are on the wire (caps.day_counts),
    # the claim counted from its moment, which may fall before midnight.
    claimed_today = now - timedelta(minutes=20) >= reminders.day_start(now)
    assert jarvis.sent == 1 + claimed_today
    # The mode's capped count is the draft cards (none: the owner wrote these),
    # not every send.
    assert jarvis.daily_used == 0
    assert jarvis.daily_cap == settings.jarvis_draft_daily_cap
    assert jarvis.backlog_held == 0
    # Only Jarvis operations count against its cap.
    assert jarvis.cost_usd == Decimal("0.25") and jarvis.cost_capped is False
    assert jarvis.cost_cap_usd == Decimal("3.00")
    assert [c.channel for c in jarvis.channels] == ["telegram", "whatsapp", "gmail"]
    telegram = jarvis.channel("telegram")
    assert telegram is not None and telegram.send_enabled and not telegram.limited
    assert _keys(status).count("outbox_stuck") == 1
    [stuck] = [p for p in health.problems(status) if p.key == "outbox_stuck"]
    assert stuck.text.startswith("📤 2 ta xabar 25 daqiqadan beri")


async def test_a_stuck_queue_is_judged_only_where_sending_should_happen(
    session, jarvis_on, monkeypatch
):
    from miya.jarvis import policy

    now = datetime.now(TZ)
    old = now - timedelta(minutes=30)
    session.add_all(
        [
            _outbox(state="approved", key="a", approved_by="button", approved_at=old),
            # The gate held it and looks again later: waiting, not stuck.
            _outbox(
                state="approved",
                key="held",
                approved_by="button",
                approved_at=old,
                next_attempt_at=now + timedelta(minutes=1),
            ),
            # Scheduled for this evening.
            _outbox(
                state="approved",
                key="later",
                approved_by="button",
                approved_at=old,
                not_before=now + timedelta(hours=3),
            ),
        ]
    )
    await session.flush()

    held = 0

    async def stuck(**beat) -> int:
        nonlocal held
        if beat:
            await health.beat(session, "userbot", detail=beat, now=now)
        status = await health.gather(session, now=now)
        assert status.jarvis is not None
        held = status.jarvis.backlog_held
        return status.jarvis.backlog_stuck

    assert await stuck() == 1 and held == 0
    # The sender waits out a flood wait that ended five minutes ago: held.
    recent = (now - timedelta(minutes=5)).isoformat()
    assert await stuck(enabled=True, sender_paused_until=recent) == 0 and held == 1
    long_ago = (now - timedelta(minutes=20)).isoformat()
    assert await stuck(enabled=True, sender_paused_until=long_ago) == 1 and held == 0
    # The owner's pause is their choice, not a fault: the row is held.
    await policy.pause(session, by="command", now=now)
    assert await stuck() == 0 and held == 1
    await policy.resume(session, by="command", now=now)
    assert await stuck() == 1 and held == 0
    # The breaker is its own problem: never doubled as a stuck queue.
    await _trip(session, now, until=now + timedelta(hours=24))
    status = await health.gather(session, now=now)
    assert "telegram_send_limited" in _keys(status)
    assert "outbox_stuck" not in _keys(status)
    assert status.jarvis is not None and status.jarvis.backlog_stuck == 0
    assert status.jarvis.backlog_held == 1
    assert health.unjudged(status) == frozenset({"outbox_stuck"})
    await policy.resume(
        session, scope=policy.channel_scope("telegram"), by="command", now=now
    )
    # With Telegram sending off no adapter is expected to take the row.
    monkeypatch.setattr(settings, "telegram_send_enabled", False)
    assert await stuck() == 0 and held == 1


def _bot():
    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id, text, **kwargs) -> None:
            self.sent.append(text)

    return _Bot()


async def _jarvis_sweep(bot, *, now: datetime) -> None:
    """The health job's alert step over the Jarvis problems of a real status
    (the rest of this database's problems are not under test)."""
    from miya.db.session import session_scope
    from miya.worker import main as worker

    async with session_scope() as s:
        status = await health.gather(s, now=now)
        found = [
            p for p in health.problems(status) if p.key in health.JARVIS_PROBLEM_KEYS
        ]
        await worker._alert_from_ledger(
            s, bot, found, now=now, unjudged=health.unjudged(status)
        )


async def test_breaker_alert_immediate(session, jarvis_on, monkeypatch):
    """The trip is told by jarvis_send_notices at once (JV-19), not by the
    5-minute health sweep; the sweep records nothing new, repeats it after
    ALERT_REPEAT_HOURS and says «tiklandi» once when it clears."""
    from miya.bot import replies
    from miya.db.session import SessionLocal
    from miya.jarvis import policy
    from miya.worker import main as worker

    monkeypatch.setattr(settings, "alert_repeat_hours", 6)
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _bot()
    monkeypatch.setattr(worker, "notify", lambda b, text, **kw: _record(bot, text))
    now = datetime.now(TZ)
    async with SessionLocal() as s:
        await _trip(s, now, until=now + timedelta(days=2))
        await s.commit()

    # A sweep that runs before the notice job says nothing: not its message.
    await _jarvis_sweep(bot, now=now)
    assert bot.sent == []
    # The notice job tells it at once and records it in the health ledger.
    assert await worker.jarvis_send_notices(None, now=now) == 1
    assert bot.sent == [replies.JARVIS_TELEGRAM_LIMITED]
    assert len(await _alert_rows(session, "telegram_send_limited")) == 1
    # The next sweeps do not double it, nor does the notice job.
    await _jarvis_sweep(bot, now=now + timedelta(minutes=5))
    await worker.jarvis_send_notices(None, now=now + timedelta(minutes=6))
    assert bot.sent == [replies.JARVIS_TELEGRAM_LIMITED]
    # Still limited after ALERT_REPEAT_HOURS: the ledger repeats it once.
    later = now + timedelta(hours=6, minutes=5)
    await _jarvis_sweep(bot, now=later)
    await _jarvis_sweep(bot, now=later + timedelta(minutes=5))
    assert bot.sent == [replies.JARVIS_TELEGRAM_LIMITED] * 2
    # /jarvis yoq telegram lifts it: one «tiklandi», then silence.
    async with SessionLocal() as s:
        await policy.resume(
            s, scope=policy.channel_scope("telegram"), by="command", now=later
        )
        await s.commit()
    await _jarvis_sweep(bot, now=later + timedelta(minutes=10))
    await _jarvis_sweep(bot, now=later + timedelta(minutes=15))
    assert bot.sent[2:] == ["✅ Telegram yuborish tiklandi"]


async def _record(bot, text: str) -> bool:
    bot.sent.append(text)
    return True


async def _alert_rows(session, key: str) -> list:
    return list(
        await session.scalars(
            sa.select(m.ReminderLog.sent_at).where(
                m.ReminderLog.kind == health.ALERT_KIND + key
            )
        )
    )


async def test_cost_cap_told_by_the_responder_then_kept_by_the_ledger(
    session, jarvis_on, monkeypatch
):
    from miya.bot import replies
    from miya.jarvis import responder
    from miya.worker import main as worker

    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    monkeypatch.setattr(settings, "jarvis_daily_cost_cap_usd", Decimal("0.10"))
    bot = _bot()
    monkeypatch.setattr(worker, "notify", lambda b, text, **kw: _record(bot, text))

    async def _capped(*, now, alert=None):
        return responder.RunResult(
            claimed=1,
            outcomes=(responder.Outcome("tg:1", responder.COST_CAP, cost_capped=True),),
        )

    monkeypatch.setattr(responder, "run", _capped)
    session.add(
        m.UsageLog(
            provider="anthropic", operation="jarvis_draft", cost_usd=Decimal("0.20")
        )
    )
    await session.commit()
    now = datetime.now(TZ)
    await _jarvis_sweep(bot, now=now)
    assert bot.sent == []
    await worker.jarvis_respond_job(None, now=now)
    assert bot.sent == [replies.JARVIS_COST_STOP]
    await _jarvis_sweep(bot, now=now + timedelta(minutes=5))
    assert bot.sent == [replies.JARVIS_COST_STOP]
    # The cap is raised (or the next day comes): one «tiklandi».
    monkeypatch.setattr(settings, "jarvis_daily_cost_cap_usd", Decimal("5"))
    await _jarvis_sweep(bot, now=now + timedelta(minutes=10))
    await _jarvis_sweep(bot, now=now + timedelta(minutes=15))
    assert bot.sent[1:] == ["✅ Jarvis xarajat chegarasi tiklandi"]


async def test_a_stuck_queue_is_alerted_by_the_sweep_and_recovers(
    session, jarvis_on, monkeypatch
):
    from miya.worker import main as worker

    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _bot()
    monkeypatch.setattr(worker, "notify", lambda b, text, **kw: _record(bot, text))
    now = datetime.now(TZ)
    row = _outbox(
        state="approved",
        key="stuck",
        approved_by="button",
        approved_at=now - timedelta(minutes=40),
    )
    session.add(row)
    await session.commit()
    await _jarvis_sweep(bot, now=now)
    await _jarvis_sweep(bot, now=now + timedelta(minutes=5))
    assert bot.sent == [
        "📤 1 ta xabar 40 daqiqadan beri yuborilmay turibdi. "
        "Userbot ishlayotganini tekshiring."
    ]
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == row.id)
        .values(state="sent", sent_at=now)
    )
    await session.commit()
    await _jarvis_sweep(bot, now=now + timedelta(minutes=10))
    assert bot.sent[1:] == ["✅ Xabar yuborish tiklandi"]


async def test_a_paused_stuck_queue_is_not_announced_as_recovered(
    session, jarvis_on, monkeypatch
):
    """Rows that only wait on a pause (or a breaker) did not leave: no
    «tiklandi» until they do."""
    from miya.db.session import SessionLocal
    from miya.jarvis import policy
    from miya.worker import main as worker

    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _bot()
    monkeypatch.setattr(worker, "notify", lambda b, text, **kw: _record(bot, text))
    now = datetime.now(TZ)
    row = _outbox(
        state="approved",
        key="stuck",
        approved_by="button",
        approved_at=now - timedelta(minutes=40),
    )
    session.add(row)
    await session.commit()
    await _jarvis_sweep(bot, now=now)
    assert len(bot.sent) == 1 and bot.sent[0].startswith("📤 1 ta xabar")
    async with SessionLocal() as s:
        await policy.pause(s, by="command", now=now)
        await s.commit()
    await _jarvis_sweep(bot, now=now + timedelta(minutes=5))
    assert len(bot.sent) == 1
    async with SessionLocal() as s:
        await policy.resume(s, by="command", now=now)
        await s.commit()
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == row.id)
        .values(state="sent", sent_at=now)
    )
    await session.commit()
    await _jarvis_sweep(bot, now=now + timedelta(minutes=10))
    assert bot.sent[1:] == ["✅ Xabar yuborish tiklandi"]


async def test_a_stuck_queue_is_told_silently_in_quiet_hours(
    session, jarvis_on, monkeypatch
):
    """Approved sends go out at night too: a queue stuck then is told at
    once, without a sound; its recovery waits for morning."""
    from miya.worker import main as worker

    quiet = True
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: quiet)
    sent: list[tuple[str, bool]] = []

    async def _notify(bot, text, *, silent=False, **kwargs) -> bool:
        sent.append((text, silent))
        return True

    monkeypatch.setattr(worker, "notify", _notify)
    now = datetime.now(TZ)
    row = _outbox(
        state="approved",
        key="stuck",
        approved_by="button",
        approved_at=now - timedelta(minutes=40),
    )
    session.add(row)
    await session.commit()
    await _jarvis_sweep(None, now=now)
    await _jarvis_sweep(None, now=now + timedelta(minutes=5))
    assert len(sent) == 1
    assert sent[0][0].startswith("📤 1 ta xabar") and sent[0][1] is True
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == row.id)
        .values(state="sent", sent_at=now)
    )
    await session.commit()
    await _jarvis_sweep(None, now=now + timedelta(minutes=10))
    assert len(sent) == 1
    quiet = False
    await _jarvis_sweep(None, now=now + timedelta(minutes=15))
    assert sent[1:] == [("✅ Xabar yuborish tiklandi", False)]


# --- /holat (JV-21) ----------------------------------------------------------------


def _userbot(**detail) -> dict[str, health.Component]:
    return _with(
        "userbot",
        health.component_of(
            "userbot",
            _row("userbot", minutes=1, detail={"enabled": True, **detail}),
            now=NOW,
        ),
    )


def _job(minutes: float, **detail) -> dict[str, health.Component]:
    return {
        health.JARVIS_RESPOND_JOB: health.component_of(
            health.JARVIS_RESPOND_JOB,
            _row("job:jarvis_respond", minutes=minutes, detail={"ok": True, **detail}),
            now=NOW,
        )
    }


def test_holat_jarvis_lines(jarvis_on, monkeypatch):
    from miya.bot import replies

    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    sent_at = (NOW - timedelta(minutes=7)).isoformat()
    status = _status(
        jarvis=_jarvis(queued=1),
        components=_userbot(sender="running", sender_sent_at=sent_at),
        jobs=_job(0.2),
    )
    assert replies.jarvis_status_lines(status) == [
        # The draft cards against their cap, as /jarvis shows it, not every
        # send of the day (3).
        "🤖 Jarvis: qoralama · bugun 2/40 yuborildi · navbatda 1",
        "🧾 Jarvis bugun: 4 ta qoralama · 1 ta eskalatsiya · xarajat $0.42 / $3.00",
        "📤 Telegram yuborish: ✅ ishlayapti · oxirgi yuborish: 7 daqiqa oldin",
        "✅ Javob beruvchi — oxirgi ishga tushish: hozirgina",
    ]
    report = replies.status_report(status, health.problems(status))
    assert "🤖 Jarvis: qoralama" in report
    assert report.index("🤖 Jarvis") < report.index("<b>Navbatda</b>")


def test_holat_jarvis_send_states(jarvis_on, monkeypatch):
    from miya.bot import replies

    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)

    def send_line(jarvis: health.JarvisHealth, **beat) -> str:
        status = _status(jarvis=jarvis, components=_userbot(**beat), jobs=_job(1))
        [line] = [
            x for x in replies.jarvis_status_lines(status) if x.startswith("📤 Tel")
        ]
        return line

    running = {"sender": "running"}
    assert send_line(_jarvis(channels=_limited(NOW + timedelta(hours=2))), **running) == (
        f"📤 Telegram yuborish: ⛔ cheklangan (bugun "
        f"{(NOW + timedelta(hours=2)).strftime('%H:%M')} gacha)"
    )
    no_end = NOW + timedelta(days=3650)
    assert send_line(_jarvis(channels=_limited(no_end)), **running) == (
        "📤 Telegram yuborish: ⛔ cheklangan (/jarvis yoq telegram bilan yoqilguncha)"
    )
    off = (health.ChannelSend("telegram", send_enabled=False),)
    assert send_line(_jarvis(channels=off), **running) == (
        "📤 Telegram yuborish: ⏸ o'chirilgan (TELEGRAM_SEND_ENABLED=false)"
    )
    paused = (health.ChannelSend("telegram", send_enabled=True, paused=True),)
    assert send_line(_jarvis(channels=paused), **running) == (
        "📤 Telegram yuborish: ⏸ to'xtatilgan (/jarvis yoq)"
    )
    wait = (NOW + timedelta(minutes=30)).isoformat()
    assert send_line(_jarvis(), sender="running", sender_paused_until=wait) == (
        f"📤 Telegram yuborish: ⏳ Telegram kutishni so'radi (bugun "
        f"{(NOW + timedelta(minutes=30)).strftime('%H:%M')} gacha)"
    )
    assert send_line(_jarvis(), sender="restarting", sender_error="<Boom>") == (
        "📤 Telegram yuborish: ⚠️ yuboruvchi ishlamayapti "
        "(qayta ishga tushirilmoqda: <code>&lt;Boom&gt;</code>)"
    )
    assert send_line(_jarvis()) == (
        "📤 Telegram yuborish: ⚠️ yuboruvchi ishlamayapti (hali ishga tushmagan)"
    )
    silent = _status(
        jarvis=_jarvis(), components=_with("userbot", _silent("userbot")), jobs=_job(1)
    )
    assert (
        "📤 Telegram yuborish: ⚠️ yuboruvchi ishlamayapti (Telegram o'quvchi jim)"
        in replies.jarvis_status_lines(silent)
    )
    # Another channel has a line only while its breaker is in force.
    gmail = (
        health.ChannelSend("telegram", send_enabled=True),
        health.ChannelSend(
            "gmail", send_enabled=False, breaker_until=NOW + timedelta(days=1)
        ),
    )
    status = _status(jarvis=_jarvis(channels=gmail), components=_userbot(**running))
    lines = replies.jarvis_status_lines(status)
    assert any(x.startswith("📤 Gmail yuborish: ⛔ cheklangan (ertaga ") for x in lines)
    assert not any(x.startswith("📤 WhatsApp") for x in lines)


def test_holat_jarvis_queue_cost_and_responder_lines(jarvis_on, monkeypatch):
    from miya.bot import replies

    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    status = _status(
        jarvis=_jarvis(
            mode="auto",
            paused=True,
            # 25 sent today, 15 of them on their own: the cap counts those.
            sent=25,
            daily_used=15,
            daily_cap=20,
            leases_stuck=2,
            unknown=1,
            cost_usd=Decimal("3.20"),
            cost_unpriced=1,
            cost_capped=True,
        ),
        components=_userbot(sender="running"),
        jobs=_job(30),
    )
    lines = replies.jarvis_status_lines(status)
    assert (
        lines[0]
        == "🤖 Jarvis: avtomatik, to'xtatilgan · bugun 15/20 yuborildi · navbatda 0"
    )
    assert lines[1] == (
        "🧾 Jarvis bugun: 4 ta qoralama · 1 ta eskalatsiya · xarajat $3.20 / $3.00"
        " · narxi noma'lum: 1 ta chaqiruv — chegaraga yetdi"
    )
    assert "⚠️ Jarvis: 2 ta xabar yuborilish holatida qotib qoldi" in lines
    assert (
        "❓ Jarvis: 1 ta xabar yuborilganmi — aniq emas (24 soat ichida). "
        "Chatni tekshiring."
    ) in lines
    assert lines[-1] == "❌ Javob beruvchi — 30 daqiqadan beri ishlamagan"
    never = _status(jarvis=_jarvis(), components=_userbot(sender="running"))
    assert replies.jarvis_status_lines(never)[-1] == (
        "⚠️ Javob beruvchi — hali ishga tushmagan"
    )
    failed = _status(
        jarvis=_jarvis(),
        components=_userbot(sender="running"),
        jobs={
            health.JARVIS_RESPOND_JOB: health.component_of(
                health.JARVIS_RESPOND_JOB,
                _row(
                    "job:jarvis_respond", minutes=1, detail={"ok": False, "error": "<x>"}
                ),
                now=NOW,
            )
        },
    )
    assert replies.jarvis_status_lines(failed)[-1] == (
        "❌ Javob beruvchi — xato: <code>&lt;x&gt;</code>"
    )
