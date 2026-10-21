"""Jarvis's own model spend against JARVIS_DAILY_COST_CAP_USD (§5.7).

The spend is summed in SQL from ``usage_log`` since ``reminders.day_start``,
over the Jarvis operations only; the prices behind ``usage_log.cost_usd``
come from config (EXTRACT_MODEL_PRICE, REASON_MODEL_PRICE), never from a
guess. A Jarvis call that could not be priced leaves the spend unknown, and
an unknown spend counts as over the cap: above it Jarvis only escalates with
fixed templates and makes no model call, so failing that way is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import UsageLog
from miya.services.reminders import day_start

# usage_log.operation of every Jarvis model call (§5.9). receipt_vision is
# core MIYA's and never counts here.
JARVIS_OPERATIONS: Final = (
    "jarvis_intent",
    "jarvis_draft",
    "jarvis_guard",
    "jarvis_command",
    "jarvis_style",
    "jarvis_kb",
)
COST_CAP_CODE: Final = "cost_cap"


@dataclass(frozen=True, slots=True)
class Spend:
    usd: Decimal
    # Jarvis calls recorded without a price.
    unpriced: int


async def spend_today(session: AsyncSession, now: datetime) -> Spend:
    row = (
        await session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(UsageLog.cost_usd), 0),
                sa.func.count().filter(UsageLog.cost_usd.is_(None)),
            ).where(
                UsageLog.operation.in_(JARVIS_OPERATIONS),
                UsageLog.created_at >= day_start(now),
            )
        )
    ).one()
    return Spend(usd=Decimal(row[0]), unpriced=int(row[1] or 0))


def cap_reached(spend: Spend, cap: Decimal) -> bool:
    """At or over the cap, or not measurable."""
    return spend.unpriced > 0 or spend.usd >= cap


async def over_cap(session: AsyncSession, now: datetime) -> bool:
    return cap_reached(
        await spend_today(session, now), settings.jarvis_daily_cost_cap_usd
    )


__all__ = [
    "COST_CAP_CODE",
    "JARVIS_OPERATIONS",
    "Spend",
    "cap_reached",
    "over_cap",
    "spend_today",
]
