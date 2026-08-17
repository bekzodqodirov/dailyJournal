"""Daily report and planner (Phase 3). Sonnet is stubbed; the SQL is real."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, EventSource, EventStatus, TransactionType
from miya.services import planner, reports
from tests.test_pipeline import _interaction, _open_debt

TZ = settings.tz


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


class _StubClient:
    """Returns the same canned text for every call (planner + report)."""

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.messages = self
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            stop_reason="end_turn",
            usage=_usage(),
        )


def _api_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")
    )


async def _seed_day(session) -> date:
    """One day of activity: an expense, a debt and tomorrow's meeting."""
    interaction = await _interaction(session, text="kunlik yozuv")
    now = datetime.now(TZ)
    session.add(
        m.Transaction(
            type=TransactionType.expense,
            amount=1_200_000,
            currency=Currency.UZS,
            category="transport",
            occurred_at=now,
            source_interaction_id=interaction.id,
        )
    )
    await _open_debt(session, due=now.date())
    session.add(
        m.Event(
            title="Bojxona uchrashuvi",
            start_at=now.replace(hour=15, minute=0) + timedelta(days=1),
            location="Toshkent",
            source=EventSource.extracted,
            status=EventStatus.planned,
        )
    )
    await session.flush()
    return now.date()


# --- planner -----------------------------------------------------------------


async def test_planner_falls_back_to_the_raw_listing_without_api(session, monkeypatch):
    day = await _seed_day(session)
    monkeypatch.setattr(planner, "get_client", lambda: _StubClient(error=_api_error()))

    plan = await planner.plan_for(session, day + timedelta(days=1))

    # The deterministic listing still carries the fixed event with its time.
    assert "Bojxona uchrashuvi" in plan
    assert "15:00" in plan
    assert "Akmal" in plan  # the due debt is in the inputs


async def test_planner_uses_the_model_when_available(session, monkeypatch):
    day = await _seed_day(session)
    stub = _StubClient(text="<b>09:00</b> — bojxona hujjatlari")
    monkeypatch.setattr(planner, "get_client", lambda: stub)

    plan = await planner.plan_for(session, day + timedelta(days=1))

    assert plan == "<b>09:00</b> — bojxona hujjatlari"
    # The prompt's data block was deterministic SQL output.
    assert "Bojxona uchrashuvi" in stub.calls[0]["messages"][0]["content"]


# --- daily report ------------------------------------------------------------


async def test_generate_report_stores_content_and_stats(session, monkeypatch):
    day = await _seed_day(session)
    monkeypatch.setattr(reports, "get_client", lambda: _StubClient(text="HISOBOT"))
    monkeypatch.setattr(planner, "get_client", lambda: _StubClient(text="REJA"))

    content = await reports.generate_report(session, day)
    await session.commit()

    assert content == "HISOBOT"
    row = await session.scalar(sa.select(m.DailyReport))
    assert row.report_date == day
    assert row.content == "HISOBOT"
    assert row.stats["expense"] == {"UZS": "1200000.00"}
    assert row.stats["interactions"] >= 1


async def test_generate_report_upserts_on_the_same_day(session, monkeypatch):
    day = await _seed_day(session)
    monkeypatch.setattr(planner, "get_client", lambda: _StubClient(text="REJA"))

    monkeypatch.setattr(reports, "get_client", lambda: _StubClient(text="ERTALABKI"))
    await reports.generate_report(session, day)
    monkeypatch.setattr(reports, "get_client", lambda: _StubClient(text="KECHKI"))
    await reports.generate_report(session, day)
    await session.commit()

    rows = list(await session.scalars(sa.select(m.DailyReport)))
    assert len(rows) == 1
    assert rows[0].content == "KECHKI"


async def test_generate_report_survives_api_failure(session, monkeypatch):
    day = await _seed_day(session)
    failing = _StubClient(error=_api_error())
    monkeypatch.setattr(reports, "get_client", lambda: failing)
    monkeypatch.setattr(planner, "get_client", lambda: failing)

    content = await reports.generate_report(session, day)

    # The deterministic data block is stored and sent instead of nothing.
    assert content.startswith("HISOBOT KUNI:")
    assert "transport" in content and "1.2 mln" in content
    row = await session.scalar(sa.select(m.DailyReport))
    assert row is not None and row.content == content
