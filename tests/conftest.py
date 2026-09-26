"""Shared fixtures.

Database-backed tests skip automatically when nothing is listening on
``DATABASE_URL``, so `make test` works on a laptop with no containers running.
Set ``MIYA_REQUIRE_DB=1`` (CI does) to turn that skip into a failure, so a
suite that silently ran without its database cannot pass.

``MIYA_TIME_TRAVEL=newyear`` or ``=+Nd`` runs the whole suite at another
moment (see tests/timetravel.py); it is for runs without a database.

Dates in tests: hard-code a calendar date only when the test passes it to the
code under test (``now=`` / ``today=``). Anything compared against code that
reads the real clock must be derived from the real clock, or the test starts
failing on a date nobody chose.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from miya.db import models as m
from miya.db.session import SessionLocal, engine


def pytest_configure(config) -> None:
    # Runs before any test module is imported, so module-level stamps such as
    # test_health_worker.STAMP are taken in the travelled time.
    if os.environ.get("MIYA_TIME_TRAVEL"):
        from tests import timetravel

        timetravel.start()


def pytest_unconfigure(config) -> None:
    if os.environ.get("MIYA_TIME_TRAVEL"):
        from tests import timetravel

        timetravel.stop()


@pytest.fixture(autouse=True)
def _strong_api_token(monkeypatch):
    """The bot and worker refuse to start with a short API token; tests that
    drive their run() must not trip on the laptop's blank .env."""
    from miya.config import API_TOKEN_MIN_LENGTH, settings

    if len(settings.api_bearer_token.strip()) < API_TOKEN_MIN_LENGTH:
        monkeypatch.setattr(settings, "api_bearer_token", "a" * 64)


async def database_available() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1 FROM reminder_log LIMIT 0"))
        return True
    except Exception:
        return False


async def _truncate(session) -> None:
    """Wipe owner data between tests. Order follows foreign keys."""
    # Liveness rows are the processes' own, not owner data — but a heartbeat
    # left behind by one test must not read as "alive" in the next. Nothing
    # references the table, so it goes first, ahead of the ledger below.
    await session.execute(sa.delete(m.Heartbeat))
    for model in (
        # Claims reference interactions and people, so they go before both.
        m.QuestionLog,
        m.Claim,
        m.ReminderLog,
        m.UsageLog,
        m.DailyReport,
        m.Memory,
        m.Task,
        m.Event,
        m.TransactionEvidence,
        m.Transaction,
        m.Promise,
        m.DebtPayment,
        m.Debt,
        # Interactions reference windows, so they go first.
        m.Interaction,
        m.ConversationWindow,
        m.ChatMonitor,
        m.Person,
    ):
        await session.execute(sa.delete(model))
    await session.commit()


@pytest.fixture
async def session():
    if not await database_available():
        if os.environ.get("MIYA_REQUIRE_DB") == "1":
            pytest.fail("MIYA_REQUIRE_DB=1 but no migrated database at DATABASE_URL")
        pytest.skip("no migrated database reachable at DATABASE_URL")
    async with SessionLocal() as s:
        await _truncate(s)
        yield s
        await s.rollback()
        await _truncate(s)
