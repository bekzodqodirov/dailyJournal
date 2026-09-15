"""Build step 5, the owner-facing side: /holat, the bot's watchdog, GET /health.

The judgements live in services/health.py (tests/test_health.py). Here is
what the owner sees of them: the one-screen status, the bot noticing a dead
worker or a dead database, and the api's heartbeat view for a monitor.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from miya.api import main as api_main
from miya.api.main import app
from miya.bot import handlers, replies
from miya.bot import main as bot_main
from miya.bot.formatting import TELEGRAM_LIMIT
from miya.config import settings
from miya.db import models as m
from miya.services import health
from tests.test_close_and_correct import _Message

TZ = settings.tz
NOW = datetime(2026, 9, 15, 21, 40, tzinfo=TZ)
GB = 1024**3
MB = 1024**2


# --- Status objects built by hand, for the pure renderer -----------------------


def _row(name: str, *, minutes: float, detail: dict | None = None) -> m.Heartbeat:
    return m.Heartbeat(
        component=name,
        last_seen_at=NOW - timedelta(minutes=minutes),
        detail=detail or {},
    )


def _component(
    name: str, *, minutes: float = 1, detail: dict | None = None
) -> health.Component:
    return health.component_of(name, _row(name, minutes=minutes, detail=detail), now=NOW)


def _never(name: str) -> health.Component:
    return health.component_of(name, None, now=NOW)


def _healthy() -> dict[str, health.Component]:
    return {name: _component(name) for name in health.COMPONENTS}


def _backup(**overrides) -> health.BackupInfo:
    base = {
        "path": Path("/data/backups/miya-20260915-033000.dump.age"),
        "created_at": datetime(2026, 9, 15, 3, 30, tzinfo=TZ),
        "size": 12 * MB,
        "sent_to_telegram": True,
        "sent_at": datetime(2026, 9, 15, 3, 31, tzinfo=TZ),
        "stale": False,
        "configured": True,
    }
    base.update(overrides)
    return health.BackupInfo(**base)


def _status(**overrides) -> health.Status:
    base = {
        "now": NOW,
        "components": _healthy(),
        "jobs": {},
        "userbot_last_message_at": NOW - timedelta(minutes=6),
        "db_ok": True,
        "db_size_bytes": 512 * MB,
        "disk_free_bytes": 34 * GB,
        "disk_total_bytes": 80 * GB,
        "disk_low": False,
        "backup": _backup(),
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
    return {**_healthy(), name: component}


def _lines(status: health.Status, problems=None) -> list[str]:
    return replies.status_report(status, problems or []).split("\n")


# --- /holat: the renderer ------------------------------------------------------


def test_a_healthy_status_renders_the_contract_shape():
    lines = _lines(_status())

    assert lines == [
        "🩺 <b>MIYA holati</b> · 15-sen 21:40",
        "✅ Bot — ishlayapti",
        "✅ Telegram o'quvchi — ulangan · oxirgi xabar: 6 daqiqa oldin",
        "✅ Rejalashtiruvchi — oxirgi urish: 1 daqiqa oldin",
        "✅ Baza — javob beryapti · 512 MB",
        "✅ Disk — 34 GB bo'sh",
        "✅ Zaxira nusxa — bugun 03:30 · 12 MB · Telegramga yuborildi",
        "✅ Anthropic — oxirgi muvaffaqiyat: 12 daqiqa oldin",
        "<b>Navbatda</b>: kutayotgan suhbatlar 0 · batch'da 0 · "
        "ishlanmagan 0 (/tekshir) · da'volar 0 (/davolar)",
        "<b>Xarajat</b>: bugun $0.10 · bu oy $3.50 (/xarajat)",
    ]


def test_the_bot_line_names_the_bot_when_its_heartbeat_knows_it():
    status = _status(
        components=_with("bot", _component("bot", detail={"username": "miya_bot"}))
    )
    assert "✅ Bot — ishlayapti · @miya_bot" in _lines(status)


def test_the_userbot_line_has_all_four_states():
    disabled = _component("userbot", minutes=600, detail={"enabled": False})
    assert "⏸ Telegram o'quvchi — o'chirilgan (USERBOT_ENABLED=false)" in _lines(
        _status(components=_with("userbot", disabled))
    )

    quiet = _component("userbot", minutes=30)
    stale = _lines(_status(components=_with("userbot", quiet)))
    assert (
        "❌ Telegram o'quvchi — 30 daqiqadan beri jim · oxirgi xabar: 6 daqiqa oldin"
        in stale
    )

    never = _lines(
        _status(
            components=_with("userbot", _never("userbot")),
            userbot_last_message_at=None,
        )
    )
    assert "❌ Telegram o'quvchi — hali ulanmagan · oxirgi xabar: hali yo'q" in never

    dropped = _component("userbot", detail={"enabled": True, "connected": False})
    assert "⚠️ Telegram o'quvchi — Telegramdan uzilgan · oxirgi xabar: 6 daqiqa oldin" in (
        _lines(_status(components=_with("userbot", dropped)))
    )


def test_a_silent_worker_names_the_job_that_has_waited_longest():
    jobs = {
        "reminder": _component("reminder", minutes=45),
        "backup": _component("backup", minutes=2 * 24 * 60),
    }
    lines = _lines(
        _status(components=_with("worker", _component("worker", minutes=45)), jobs=jobs)
    )
    assert (
        "❌ Rejalashtiruvchi — 45 daqiqadan beri jim · eng eski ish: backup (2 kun oldin)"
        in lines
    )

    never = _lines(_status(components=_with("worker", _never("worker"))))
    assert "❌ Rejalashtiruvchi — hali bir marta ham urmagan" in never


def test_database_and_disk_lines_go_red():
    down = _lines(_status(db_ok=False, db_size_bytes=None))
    assert "❌ Baza — javob bermayapti" in down
    low = _lines(_status(disk_free_bytes=int(1.2 * GB), disk_low=True))
    assert "❌ Disk — 1.2 GB bo'sh (chegara 2 GB)" in low
    unknown = _lines(_status(disk_free_bytes=None, disk_total_bytes=None))
    assert "⚠️ Disk — o'lchab bo'lmadi" in unknown


def test_the_backup_line_covers_every_state():
    def line(**overrides) -> str:
        lines = _lines(_status(backup=_backup(**overrides)))
        return next(line for line in lines if "Zaxira nusxa" in line)

    assert line(configured=False) == (
        "⚠️ Zaxira nusxa — sozlanmagan (BACKUP_AGE_RECIPIENT bo'sh)"
    )
    assert line(path=None, created_at=None, size=None, stale=True) == (
        "⚠️ Zaxira nusxa — hali yo'q"
    )
    two_days = datetime(2026, 9, 13, 3, 30, tzinfo=TZ)
    assert line(created_at=two_days, stale=True, sent_to_telegram=False) == (
        "⚠️ Zaxira nusxa — 13-sen 03:30 · 12 MB · eskirgan"
    )
    yesterday = datetime(2026, 9, 14, 3, 30, tzinfo=TZ)
    assert line(created_at=yesterday) == (
        "✅ Zaxira nusxa — kecha 03:30 · 12 MB · Telegramga yuborildi"
    )
    assert line(sent_to_telegram=False, sent_at=None, send_failed=True) == (
        "⚠️ Zaxira nusxa — bugun 03:30 · 12 MB · Telegramga yuborilmadi"
    )
    assert line(sent_to_telegram=False, sent_at=None) == (
        "⚠️ Zaxira nusxa — bugun 03:30 · 12 MB · Telegramga hali yuborilmagan"
    )


def test_a_backup_kept_on_disk_by_choice_is_fine(monkeypatch):
    monkeypatch.setattr(settings, "backup_to_telegram", False)
    lines = _lines(_status(backup=_backup(sent_to_telegram=False, sent_at=None)))
    assert "✅ Zaxira nusxa — bugun 03:30 · 12 MB · faqat diskda" in lines


def test_the_anthropic_line_warns_with_the_queue_counts():
    failing = _lines(_status(anthropic_failing=True, windows_pending=4, windows_failed=2))
    assert (
        "⚠️ Anthropic — oxirgi muvaffaqiyat: 12 daqiqa oldin · 4 ta kutmoqda, 2 ta xato"
        in failing
    )
    fresh = _lines(_status(anthropic_last_ok_at=None))
    assert "✅ Anthropic — hali ishlatilmagan" in fresh
    never_and_failing = _lines(
        _status(anthropic_last_ok_at=None, anthropic_failing=True, windows_pending=1)
    )
    assert any(
        line.startswith("⚠️ Anthropic — oxirgi muvaffaqiyat: hali yo'q")
        for line in never_and_failing
    )


def test_the_queue_and_spend_lines_carry_the_numbers_and_the_commands():
    lines = _lines(
        _status(
            windows_pending=3,
            windows_submitted=7,
            needs_review=21,
            claims_pending=2,
            cost_today_usd=Decimal("1.2345"),
            cost_month_usd=Decimal("40"),
        )
    )
    assert (
        "<b>Navbatda</b>: kutayotgan suhbatlar 3 · batch'da 7 · "
        "ishlanmagan 21 (/tekshir) · da'volar 2 (/davolar)"
    ) in lines
    assert "<b>Xarajat</b>: bugun $1.23 · bu oy $40.00 (/xarajat)" in lines


def test_problem_lines_follow_after_a_blank_line():
    status = _status(components=_with("worker", _component("worker", minutes=30)))
    problems = health.problems(status)
    assert [p.key for p in problems] == ["worker_silent"]

    text = replies.status_report(status, problems)

    body, remedies = text.split("\n\n", 1)
    assert body.endswith("(/xarajat)")
    assert remedies == problems[0].text
    assert "docker compose restart worker" in remedies


def test_hostile_strings_in_the_status_are_escaped():
    jobs = {"<b>evil</b>": _component("<b>evil</b>", minutes=60)}
    bot = _component("bot", detail={"username": "<script>alert(1)</script>"})
    status = _status(
        components={**_with("worker", _component("worker", minutes=30)), "bot": bot},
        jobs=jobs,
    )

    text = replies.status_report(status, health.problems(status))

    assert "<b>evil</b>" not in text and "&lt;b&gt;evil&lt;/b&gt;" in text
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_the_status_is_clipped():
    problems = [health.Problem("x", "warning", "⚠️ " + "a" * 5000)]
    text = replies.status_report(_status(), problems)
    assert len(text) <= TELEGRAM_LIMIT + 40
    assert text.endswith("<i>(qisqartirildi)</i>")


def test_help_teaches_holat():
    assert "/holat — MIYA'ning ahvoli" in replies.HELP


# --- /holat: through the real handler ----------------------------------------


@pytest.fixture
def bound(session, monkeypatch):
    """Handlers run inside the test session (tests/test_close_and_correct)."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def test_holat_on_an_empty_database_says_hali_yoq_everywhere(
    bound, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    message = _Message()

    await handlers.cmd_status(message)

    [(text, markup)] = message.sent
    assert markup is None
    assert text.startswith("🩺 <b>MIYA holati</b> · ")
    assert "✅ Bot — ishlayapti" in text
    assert "❌ Telegram o'quvchi — hali ulanmagan · oxirgi xabar: hali yo'q" in text
    assert "❌ Rejalashtiruvchi — hali bir marta ham urmagan" in text
    assert "✅ Baza — javob beryapti · " in text
    assert " Disk — " in text
    assert "⚠️ Zaxira nusxa — sozlanmagan (BACKUP_AGE_RECIPIENT bo'sh)" in text
    assert "✅ Anthropic — hali ishlatilmagan" in text
    assert "kutayotgan suhbatlar 0 · batch'da 0 · ishlanmagan 0" in text
    assert "bugun $0.00 · bu oy $0.00" in text
    # The remedies for what is silent follow the status itself.
    assert "docker compose restart worker" in text
    assert "docker compose restart userbot" in text


async def test_holat_goes_green_once_the_processes_beat(bound, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    for name in health.COMPONENTS:
        await health.beat(bound, name, detail={"username": "miya_bot"})
    await health.beat(bound, "userbot", detail={"enabled": True, "connected": True})
    await bound.flush()
    message = _Message()

    await handlers.cmd_status(message)

    [(text, _)] = message.sent
    assert "✅ Bot — ishlayapti · @miya_bot" in text
    assert "✅ Telegram o'quvchi — ulangan · oxirgi xabar: hali yo'q" in text
    assert "✅ Rejalashtiruvchi — oxirgi urish: hozirgina" in text
    assert "❌" not in text


async def test_holat_shows_a_userbot_switched_off_as_paused(bound, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    await health.beat(
        bound, "userbot", detail={"enabled": False}, now=NOW - timedelta(days=3)
    )
    await bound.flush()
    message = _Message()

    await handlers.cmd_status(message)

    [(text, _)] = message.sent
    assert "⏸ Telegram o'quvchi — o'chirilgan" in text
    assert "docker compose restart userbot" not in text


# --- the bot's watchdog ---------------------------------------------------------


class _Bot:
    """Records what reached the owner; can be told Telegram is down."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        assert chat_id == settings.owner_telegram_id
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append(text)


@pytest.fixture
async def watchdog(session, monkeypatch):
    """watchdog_tick runs inside the test session; the memo starts empty."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(bot_main, "session_scope", _scope)
    monkeypatch.setattr(settings, "owner_telegram_id", 4242)
    bot_main._memo.clear()
    yield session
    bot_main._memo.clear()


async def _alert_rows(session, key: str) -> tuple[int, int]:
    rows = await session.scalars(
        sa.select(m.ReminderLog.kind).where(m.ReminderLog.ref == key)
    )
    kinds = list(rows)
    return (
        kinds.count(health.ALERT_KIND + key),
        kinds.count(health.RECOVERED_KIND + key),
    )


async def test_a_fresh_worker_means_a_quiet_tick_that_still_beats(watchdog):
    await health.beat(watchdog, "worker", now=NOW - timedelta(minutes=1))
    bot = _Bot()

    await bot_main.watchdog_tick(bot, now=NOW)

    assert bot.sent == []
    rows = await health.beats(watchdog)
    assert rows["bot"].last_seen_at == NOW
    assert await _alert_rows(watchdog, "worker_silent") == (0, 0)


async def test_a_silent_worker_is_reported_once_then_recovered_once(watchdog):
    await health.beat(watchdog, "worker", now=NOW - timedelta(minutes=30))
    bot = _Bot()

    await bot_main.watchdog_tick(bot, now=NOW)

    assert len(bot.sent) == 1
    assert bot.sent[0].startswith("❌ Rejalashtiruvchi (worker) 30 daqiqadan beri jim")
    assert "docker compose restart worker" in bot.sent[0]
    assert await _alert_rows(watchdog, "worker_silent") == (1, 0)

    # Inside ALERT_REPEAT_HOURS: nothing, however many ticks.
    for minutes in (5, 10, 60):
        await bot_main.watchdog_tick(bot, now=NOW + timedelta(minutes=minutes))
    assert len(bot.sent) == 1

    # Past it (and past the night's quiet hours) and still silent: said again.
    later = NOW + timedelta(hours=10)
    assert timedelta(hours=10) > timedelta(hours=settings.alert_repeat_hours)
    await bot_main.watchdog_tick(bot, now=later)
    assert len(bot.sent) == 2
    assert "soatdan beri jim" in bot.sent[1]

    # Back: the bot says nothing more — the worker's own health job, which
    # runs again now that it is alive, announces the recovery from the
    # shared ledger. Nothing repeats.
    back = later + timedelta(minutes=30)
    await health.beat(watchdog, "worker", now=back)
    await bot_main.watchdog_tick(bot, now=back)
    await bot_main.watchdog_tick(bot, now=back + timedelta(minutes=5))
    assert len(bot.sent) == 2
    assert await _alert_rows(watchdog, "worker_silent") == (2, 0)


async def test_a_silent_worker_waits_out_the_quiet_hours(watchdog):
    """A warning, so it holds until morning — and is not marked meanwhile."""
    night = datetime(2026, 9, 16, 2, 0, tzinfo=TZ)
    await health.beat(watchdog, "worker", now=night - timedelta(minutes=30))
    bot = _Bot()

    await bot_main.watchdog_tick(bot, now=night)
    assert bot.sent == []
    assert await _alert_rows(watchdog, "worker_silent") == (0, 0)

    morning = datetime(2026, 9, 16, 7, 31, tzinfo=TZ)
    await bot_main.watchdog_tick(bot, now=morning)
    assert len(bot.sent) == 1 and "6 soatdan beri jim" in bot.sent[0]

    # The recovery is the worker's to announce; the bot stays quiet.
    await health.beat(watchdog, "worker", now=datetime(2026, 9, 17, 1, 0, tzinfo=TZ))
    await bot_main.watchdog_tick(bot, now=datetime(2026, 9, 17, 1, 0, tzinfo=TZ))
    await health.beat(watchdog, "worker", now=datetime(2026, 9, 17, 8, 0, tzinfo=TZ))
    await bot_main.watchdog_tick(bot, now=datetime(2026, 9, 17, 8, 0, tzinfo=TZ))
    assert len(bot.sent) == 1


async def test_a_database_down_is_said_even_at_night(watchdog, monkeypatch):
    async def _raise(session):
        raise OSError("connection refused")

    monkeypatch.setattr(health, "beats", _raise)
    bot = _Bot()

    await bot_main.watchdog_tick(bot, now=datetime(2026, 9, 16, 2, 0, tzinfo=TZ))

    assert bot.sent == [replies.DB_DOWN_ALERT]


async def test_a_worker_that_never_beat_is_reported_too(watchdog):
    bot = _Bot()
    await bot_main.watchdog_tick(bot, now=NOW)
    assert len(bot.sent) == 1
    assert "hali bir marta ham xabar bermagan" in bot.sent[0]


async def test_an_alert_telegram_refused_is_not_recorded_as_sent(watchdog):
    await health.beat(watchdog, "worker", now=NOW - timedelta(minutes=30))
    bot = _Bot(fail=True)

    await bot_main.watchdog_tick(bot, now=NOW)
    assert bot.sent == []
    assert await _alert_rows(watchdog, "worker_silent") == (0, 0)

    bot.fail = False
    await bot_main.watchdog_tick(bot, now=NOW + timedelta(minutes=5))
    assert len(bot.sent) == 1
    assert await _alert_rows(watchdog, "worker_silent") == (1, 0)


async def test_the_bot_speaks_only_for_the_worker_key(watchdog):
    """A backup alert the worker's health job wrote is the worker's to
    recover — the bot must not announce it just because it is not in its
    own (worker-only) problem list."""
    watchdog.add(
        m.ReminderLog(
            kind=health.ALERT_KIND + "backup_stale",
            ref="backup_stale",
            sent_at=NOW - timedelta(hours=1),
        )
    )
    await health.beat(watchdog, "worker", now=NOW - timedelta(minutes=1))
    await watchdog.flush()
    bot = _Bot()

    await bot_main.watchdog_tick(bot, now=NOW)

    assert bot.sent == []
    assert await _alert_rows(watchdog, "backup_stale") == (1, 0)


async def test_an_unreachable_database_is_said_once_per_window(watchdog, monkeypatch):
    real_beats = health.beats

    async def _raise(session):
        raise OSError("connection refused")

    monkeypatch.setattr(health, "beats", _raise)
    bot = _Bot()

    await bot_main.watchdog_tick(bot, now=NOW)
    assert bot.sent == [replies.DB_DOWN_ALERT]
    assert "Baza javob bermayapti" in bot.sent[0]

    await bot_main.watchdog_tick(bot, now=NOW + timedelta(minutes=5))
    await bot_main.watchdog_tick(bot, now=NOW + timedelta(hours=1))
    assert len(bot.sent) == 1

    later = NOW + timedelta(hours=settings.alert_repeat_hours, minutes=1)
    await bot_main.watchdog_tick(bot, now=later)
    assert len(bot.sent) == 2

    # The database answers again: one recovery line from the memo.
    monkeypatch.setattr(health, "beats", real_beats)
    await health.beat(watchdog, "worker", now=later + timedelta(minutes=5))
    await bot_main.watchdog_tick(bot, now=later + timedelta(minutes=5))
    assert bot.sent[2] == health.recovery_text("db_down")
    await bot_main.watchdog_tick(bot, now=later + timedelta(minutes=10))
    assert len(bot.sent) == 3


async def test_a_db_down_message_telegram_refused_is_retried_next_tick(
    watchdog, monkeypatch
):
    async def _raise(session):
        raise OSError("connection refused")

    monkeypatch.setattr(health, "beats", _raise)
    bot = _Bot(fail=True)

    await bot_main.watchdog_tick(bot, now=NOW)
    assert bot.sent == [] and bot_main._memo == {}

    bot.fail = False
    await bot_main.watchdog_tick(bot, now=NOW + timedelta(minutes=5))
    assert bot.sent == [replies.DB_DOWN_ALERT]


async def test_send_retries_as_plain_text_and_reports_delivery(monkeypatch):
    monkeypatch.setattr(settings, "owner_telegram_id", 4242)
    calls: list[dict] = []

    class _Picky:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(kwargs)
            if "parse_mode" not in kwargs:
                raise RuntimeError("can't parse entities")

    assert await bot_main._send(_Picky(), "<b>x") is True
    assert calls == [{}, {"parse_mode": None}]

    class _Dead:
        async def send_message(self, *a, **kw):
            raise RuntimeError("down")

    assert await bot_main._send(_Dead(), "x") is False


async def test_startup_beats_bot_and_the_watchdog_task_is_cancelled(session, monkeypatch):
    seen: dict = {}

    class _Me:
        username = "miya_test_bot"

    class _Session:
        async def close(self):
            seen["closed"] = True

    class _FakeBot:
        session = _Session()

        def __init__(self, *a, **kw):
            pass

        async def get_me(self):
            return _Me()

    class _Dispatcher:
        async def start_polling(self, bot, **kwargs):
            seen["tasks"] = {t.get_name() for t in asyncio.all_tasks()}

    class _Engine:
        async def dispose(self):
            pass

    monkeypatch.setattr(bot_main, "Bot", _FakeBot)
    monkeypatch.setattr(bot_main, "build_dispatcher", lambda: _Dispatcher())
    monkeypatch.setattr(bot_main, "engine", _Engine())
    monkeypatch.setattr(settings, "assistant_bot_token", "test-token")
    monkeypatch.setattr(settings, "owner_telegram_id", 4242)

    await bot_main.run()

    assert "watchdog" in seen["tasks"] and seen["closed"] is True
    rows = await health.beats(session)
    assert rows["bot"].detail == {"username": "miya_test_bot"}
    assert bot_main._identity["username"] == "miya_test_bot"
    assert not any(
        t.get_name() == "watchdog" and not t.done() for t in asyncio.all_tasks()
    )


async def test_startup_survives_a_database_that_is_down(monkeypatch):
    """The startup heartbeat is a courtesy; polling must begin regardless."""

    class _Me:
        username = "miya_test_bot"

    class _Session:
        async def close(self):
            pass

    class _FakeBot:
        session = _Session()

        def __init__(self, *a, **kw):
            pass

        async def get_me(self):
            return _Me()

    polled = {}

    class _Dispatcher:
        async def start_polling(self, bot, **kwargs):
            polled["yes"] = True

    class _Engine:
        async def dispose(self):
            pass

    async def _boom(*a, **kw):
        raise RuntimeError("database down")

    monkeypatch.setattr(bot_main, "Bot", _FakeBot)
    monkeypatch.setattr(bot_main, "build_dispatcher", lambda: _Dispatcher())
    monkeypatch.setattr(bot_main, "engine", _Engine())
    monkeypatch.setattr(bot_main, "_beat", _boom)
    monkeypatch.setattr(settings, "assistant_bot_token", "test-token")
    monkeypatch.setattr(settings, "owner_telegram_id", 4242)

    await bot_main.run()

    assert polled == {"yes": True}


# --- GET /health: components, best effort -------------------------------------


@pytest.fixture
def client():
    api_main._api_beat.clear()
    with TestClient(app) as c:
        yield c
    api_main._api_beat.clear()


async def test_health_lists_every_component_and_stays_200_when_stale(client, session):
    await health.beat(session, "worker", now=datetime.now(TZ) - timedelta(hours=2))
    await health.beat(session, "userbot", detail={"enabled": False})
    await session.commit()

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    components = body["components"]
    assert set(components) == set(health.COMPONENTS)
    assert components["worker"]["stale"] is True
    assert components["worker"]["age_seconds"] >= 2 * 3600
    assert components["worker"]["last_seen_at"] is not None
    assert components["bot"] == {"last_seen_at": None, "age_seconds": None, "stale": True}
    assert components["userbot"]["stale"] is False
    # The api beat itself on the way in.
    assert components["api"]["stale"] is False
    assert components["api"]["age_seconds"] < 60


async def test_the_api_beat_is_throttled_to_once_a_minute(client, session):
    client.get("/health")
    first = (await health.beats(session))["api"].last_seen_at

    client.get("/health")
    client.get("/health")
    assert (await health.beats(session))["api"].last_seen_at == first

    api_main._api_beat["api"] = datetime.now(TZ) - api_main.API_BEAT_EVERY
    client.get("/health")
    assert (await health.beats(session))["api"].last_seen_at > first


def test_health_is_still_ok_when_the_heartbeats_cannot_be_read(client, monkeypatch):
    async def _boom(now):
        raise RuntimeError("heartbeats table missing")

    monkeypatch.setattr(api_main, "_components", _boom)

    resp = client.get("/health")

    if resp.json()["database"] != "ok":
        pytest.skip("no database reachable")
    assert resp.status_code == 200
    assert resp.json()["components"] == {}
