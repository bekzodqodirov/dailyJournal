"""Jarvis's own model spend against JARVIS_DAILY_COST_CAP_USD (§5.7).

The spend is summed in SQL from ``usage_log`` since ``reminders.day_start``,
over the Jarvis operations only; the prices behind ``usage_log.cost_usd``
come from config (EXTRACT_MODEL_PRICE, REASON_MODEL_PRICE), never from a
guess. A Jarvis call that could not be priced leaves the spend unknown, and
an unknown spend counts as over the cap: above it Jarvis only escalates with
fixed templates and makes no model call, so failing that way is safe.

``ask`` is how the responder's calls (JV-16: ``jarvis_intent``,
``jarvis_draft``, ``jarvis_guard``) reach a model: it refuses over the cap
before anything is sent, sends no tools, and records the usage in a
transaction of its own, so a spend is counted even when the run that made it
fails later (the cap never undercounts).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import UsageLog
from miya.db.session import session_scope
from miya.services.extraction import API_FAILURES, get_client
from miya.services.reminders import day_start
from miya.services.usage import record_anthropic_usage

log = logging.getLogger(__name__)

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
# The responder's three calls (JV-16).
INTENT_OPERATION: Final = "jarvis_intent"
DRAFT_OPERATION: Final = "jarvis_draft"
GUARD_OPERATION: Final = "jarvis_guard"
# A model that declines to answer says so in the stop reason.
REFUSAL_STOP: Final = "refusal"


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


class ModelUnavailable(Exception):
    """No answer from the model: the call was not made, failed or was
    refused. The message is a code, never text (rule 23)."""


class CostCapReached(ModelUnavailable):
    """JARVIS_DAILY_COST_CAP_USD is reached (or the spend is unknown)."""


async def _record(
    session: AsyncSession,
    *,
    model: str,
    operation: str,
    usage: object,
    source_interaction_id: int | None,
) -> None:
    """The call's usage, committed in a transaction of its own so a later
    failure of the caller's run cannot roll the spend back. When that
    transaction cannot be had (no connection, the source message not
    committed yet), the caller's session records it instead."""
    try:
        async with session_scope() as own:
            await record_anthropic_usage(
                own,
                model=model,
                operation=operation,
                usage=usage,
                source_interaction_id=source_interaction_id,
            )
        return
    except Exception as exc:
        log.warning(
            "jarvis usage recorded with the caller's transaction",
            extra={
                "op": operation,
                "interaction_id": source_interaction_id,
                "error": type(exc).__name__,
            },
        )
    try:
        async with session.begin_nested():
            await record_anthropic_usage(
                session,
                model=model,
                operation=operation,
                usage=usage,
                source_interaction_id=source_interaction_id,
            )
            await session.flush()
    except Exception as exc:
        log.error(
            "jarvis usage not recorded",
            extra={
                "op": operation,
                "interaction_id": source_interaction_id,
                "error": type(exc).__name__,
            },
        )


async def ask(
    session: AsyncSession,
    *,
    model: str,
    operation: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout_seconds: float,
    now: datetime,
    source_interaction_id: int | None = None,
) -> str:
    """One Jarvis model call with no tools: the reply's text.

    Raises ``CostCapReached`` before any call over the cap, and
    ``ModelUnavailable`` for a failed call, a timeout or a refusal. The usage
    of a call that was answered is recorded under ``operation`` (priced from
    config) in its own transaction."""
    if operation not in JARVIS_OPERATIONS:
        raise ValueError(operation)
    if await over_cap(session, now):
        raise CostCapReached(COST_CAP_CODE)
    try:
        client = get_client().with_options(timeout=timeout_seconds, max_retries=1)
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
            timeout=timeout_seconds + 5,
        )
    except (TimeoutError, *API_FAILURES) as exc:
        raise ModelUnavailable(type(exc).__name__) from exc
    await _record(
        session,
        model=model,
        operation=operation,
        usage=getattr(response, "usage", None),
        source_interaction_id=source_interaction_id,
    )
    if getattr(response, "stop_reason", None) == REFUSAL_STOP:
        raise ModelUnavailable(REFUSAL_STOP)
    return "".join(
        str(getattr(part, "text", ""))
        for part in getattr(response, "content", None) or []
        if getattr(part, "type", None) == "text"
    )


__all__ = [
    "COST_CAP_CODE",
    "DRAFT_OPERATION",
    "GUARD_OPERATION",
    "INTENT_OPERATION",
    "JARVIS_OPERATIONS",
    "REFUSAL_STOP",
    "CostCapReached",
    "ModelUnavailable",
    "Spend",
    "ask",
    "cap_reached",
    "over_cap",
    "spend_today",
]
