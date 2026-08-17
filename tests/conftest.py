"""Shared fixtures.

Database-backed tests skip automatically when nothing is listening on
``DATABASE_URL``, so `make test` works on a laptop with no containers running.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from miya.db import models as m
from miya.db.session import SessionLocal, engine


async def database_available() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1 FROM reminder_log LIMIT 0"))
        return True
    except Exception:
        return False


async def _truncate(session) -> None:
    """Wipe owner data between tests. Order follows foreign keys."""
    for model in (
        m.ReminderLog,
        m.UsageLog,
        m.DailyReport,
        m.Memory,
        m.Task,
        m.Event,
        m.Transaction,
        m.Promise,
        m.DebtPayment,
        m.Debt,
        m.Interaction,
        m.ChatMonitor,
        m.Person,
    ):
        await session.execute(sa.delete(model))
    await session.commit()


@pytest.fixture
async def session():
    if not await database_available():
        pytest.skip("no migrated database reachable at DATABASE_URL")
    async with SessionLocal() as s:
        await _truncate(s)
        yield s
        await s.rollback()
        await _truncate(s)
