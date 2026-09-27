"""WP-49: the recap delivery ledger, a quiet-hours-safe catch-up, and the
REPORT_TIME validators."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from miya.config import Settings, settings
from miya.db import models as m
from miya.services import reports
from miya.worker import main as worker
from tests.test_open_loops_surface import _Bot

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _row(session, day) -> m.DailyReport | None:
    session.expire_all()
    return await session.scalar(
        sa.select(m.DailyReport).where(m.DailyReport.report_date == day)
    )


async def test_lunchtime_hisobot_does_not_cause_a_second_evening_send(session):
    day = _at(12).date()
    await reports.generate_report(session, day, store=False)
    await session.commit()
    assert await _row(session, day) is None

    bot = _Bot()
    await worker.report_job(bot, now=_at(19))
    assert sum(1 for t in bot.texts if "Bugun nima bo'ldi" in t) == 1
    row = await _row(session, day)
    assert row.delivered_at is not None and row.parts_sent == 1
    assert await worker._evening_to_resume(_at(20)) is None


class _FailingOn(_Bot):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.fail_on = text

    async def send_message(self, chat_id, text, **kwargs) -> None:
        if text == self.fail_on:
            raise RuntimeError("telegram is down")
        await super().send_message(chat_id, text, **kwargs)


async def test_a_partly_delivered_report_resumes_without_repeating(session):
    day = _at(19).date()
    session.add(
        m.DailyReport(report_date=day, content="A\nB", stats={}, parts=["A", "B"])
    )
    await session.commit()

    first = _FailingOn("B")
    assert await worker.deliver_report(first, day) is False
    assert first.texts == ["A"]
    assert (await _row(session, day)).parts_sent == 1

    # A resume at 19:30 keeps the stored parts and sends only B.
    assert await reports.generate_report(session, day, now=_at(19, 30)) == "A\n\nB"
    await session.commit()
    second = _Bot()
    assert await worker.deliver_report(second, day) is True
    assert second.texts == ["B"]
    row = await _row(session, day)
    assert row.parts_sent == 2 and row.delivered_at is not None


async def test_catch_up_is_silent_inside_quiet_hours(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: True)
    monkeypatch.setattr(settings, "backup_age_recipient", "")

    async def _no_brief(now):
        return False

    monkeypatch.setattr(worker, "_brief_is_missing", _no_brief)
    jobs: list[tuple] = []

    class _Scheduler:
        def add_job(self, func, trigger, *, id, **kwargs):
            jobs.append((func, trigger, id))

    bot = _Bot()
    await worker.catch_up(bot, _Scheduler())
    assert bot.sent == []
    [(func, trigger, job_id)] = jobs
    assert func is worker.catch_up_evening and job_id == "catch_up_evening"
    assert trigger.run_date.astimezone(TZ).time() == time(7, 30)


async def test_report_job_inside_quiet_hours_stores_but_does_not_send(session):
    night = _at(23, 45)
    bot = _Bot()
    await worker.report_job(bot, now=night)
    assert bot.sent == []
    row = await _row(session, night.date())
    assert row is not None and row.delivered_at is None and row.parts_sent == 0


@pytest.mark.parametrize("report_time", ["23:45", "08:00"])
def test_report_time_is_validated(report_time):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_time=report_time)


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


def test_the_migration_counts_history_as_delivered():
    url = os.environ.get("MIYA_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("MIYA_MIGRATION_DATABASE_URL is not set")
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "0018_group_windows_unowned")
    engine = sa.create_engine(url)
    day = datetime(2020, 1, 2).date()
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM daily_reports WHERE report_date = :d"), {"d": day}
            )
            conn.execute(
                sa.text(
                    "INSERT INTO daily_reports (report_date, content, created_at) "
                    "VALUES (:d, 'old', '2020-01-02 19:00+05')"
                ),
                {"d": day},
            )
        _alembic(url, "upgrade", "head")
        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT kind, delivered_at = created_at, parts_sent, parts "
                    "FROM daily_reports WHERE report_date = :d"
                ),
                {"d": day},
            ).one()
        assert row[0] == "evening" and row[1] is True and row[2] == 1
        assert row[3] == ["old"]
    finally:
        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM daily_reports WHERE report_date = :d"), {"d": day}
            )
        engine.dispose()
        _alembic(url, "upgrade", "head")


# --- WP-53: the evening recap end to end ----------------------------------------


class _ProseClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages = self

    def with_options(self, **kwargs):
        return self

    async def create(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


async def _seed_chat(session, day_at: datetime) -> m.Person:
    from miya.db.enums import ChatType, Direction, InteractionSource

    akmal = m.Person(display_name="Akmal", aliases=[])
    session.add(akmal)
    session.add(m.ChatMonitor(tg_chat_id=4401, chat_type=ChatType.private, title="Akmal"))
    await session.flush()
    session.add(
        m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.in_,
            person_id=akmal.id,
            tg_chat_id=4401,
            occurred_at=day_at,
            raw_text="Yuk qachon keladi?",
            meta={"tg_message_id": 1},
        )
    )
    await session.commit()
    return akmal


async def test_evening_recap_end_to_end_with_stub_model(session, monkeypatch):
    from miya.services import recaps

    akmal = await _seed_chat(session, _at(11))
    monkeypatch.setattr(
        recaps,
        "get_client",
        lambda: _ProseClient(f'{{"p:{akmal.id}": "Yuk haqida so\'radi"}}'),
    )
    bot = _Bot()
    await worker.report_job(bot, now=_at(19))
    recap, markup = bot.sent[0]
    assert recap.startswith("🌆 <b>Bugun nima bo'ldi</b>") and markup is None
    assert "🤖 <i>Yuk haqida so'radi</i>" in recap
    row = await _row(session, _at(19).date())
    assert row.delivered_at is not None and row.prose_status == "model"


async def test_hisobot_on_demand_writes_no_ledger_row_and_says_until_when(session):
    from miya.services import recaps

    await _seed_chat(session, _at(9))
    result = await recaps.build_evening(session, _at(12).date(), now=_at(12), store=False)
    assert "12:00 gacha" in result.parts[0]
    assert await _row(session, _at(12).date()) is None


async def test_api_report_today_returns_the_joined_recap(session, monkeypatch):
    from fastapi.testclient import TestClient

    from miya.api.main import app

    token = "test-token-" + "x" * 53
    monkeypatch.setattr(settings, "api_bearer_token", token)
    with TestClient(app) as client:
        response = client.post(
            "/v1/report/today", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert response.json()["content"].startswith("🌆 <b>Bugun nima bo'ldi</b>")


async def test_evening_batch_follows_the_recap(session, monkeypatch):
    from miya.bot import replies

    sent_batches = []

    async def batch(bot, *, slot, via, header, now):
        sent_batches.append(header)
        return 0

    monkeypatch.setattr(worker, "_slot_questions", batch)
    bot = _Bot()
    await worker.report_job(bot, now=_at(19))
    assert bot.texts[0].startswith("🌆 <b>Bugun nima bo'ldi</b>")
    assert sent_batches == [replies.QUESTIONS_EVENING_HEADER]
