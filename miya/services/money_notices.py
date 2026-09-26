"""Telegram receipts for money events (WP-15).

A booked payment is visible the minute it lands, with 🗑 O'chir one tap
away, so a phantom or wrong row never waits to be found as a wrong total.
The booking service calls ``note`` for every reading; only a fresh, newly
booked payment queues a receipt, and anything older than
MONEY_RECEIPT_MAX_AGE_HOURS (a first import, a backlog) is only counted into
one summary. The queue lives on the interaction's metadata, like the chat
receipts, and the partial index ix_interactions_money_notice_pending keeps
the minute-by-minute poll cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from miya.config import settings
from miya.db.models import Interaction, Transaction

MONEY_NOTICE_KEY = "money_notice"
MONEY_NOTIFIED_KEY = "money_notified"


def note(
    interaction: Interaction,
    verdict: str,
    *,
    txn: Transaction | None,
    merged: bool,
    now: datetime,
) -> None:
    """Queue the owner's receipt for one money event, when it deserves one."""
    if settings.money_receipts == "off":
        return
    age = timedelta(hours=settings.money_receipt_max_age_hours)
    if interaction.occurred_at < now - age:
        notice = {
            "txn_id": txn.id if txn is not None and not merged else None,
            "verdict": verdict,
            "backfill": True,
        }
    elif verdict == "book" and txn is not None and not merged:
        notice = {"txn_id": txn.id, "verdict": "book"}
    else:
        # A fresh review row surfaces through /tekshir and the count lines,
        # a fresh ignored one through the daily count; a merged event is a
        # payment the owner already got a receipt for.
        return
    interaction.meta = {**(interaction.meta or {}), MONEY_NOTICE_KEY: notice}


@dataclass(slots=True)
class Pending:
    fresh: list[tuple[Interaction, Transaction]]
    backfill: list[tuple[Interaction, str, Transaction | None]]


async def pending(session: AsyncSession) -> Pending:
    """Every queued notice not yet delivered, oldest first."""
    meta = Interaction.meta
    rows = list(
        await session.scalars(
            sa.select(Interaction)
            .where(meta.has_key(MONEY_NOTICE_KEY))
            .where(sa.not_(meta.has_key(MONEY_NOTIFIED_KEY)))
            .order_by(Interaction.occurred_at, Interaction.id)
        )
    )
    ids = {
        (row.meta or {})[MONEY_NOTICE_KEY].get("txn_id")
        for row in rows
        if (row.meta or {})[MONEY_NOTICE_KEY].get("txn_id") is not None
    }
    txns = {}
    if ids:
        txns = {
            t.id: t
            for t in await session.scalars(
                sa.select(Transaction)
                .where(Transaction.id.in_(ids))
                .options(selectinload(Transaction.counterparty))
            )
        }
    result = Pending(fresh=[], backfill=[])
    for row in rows:
        notice = row.meta[MONEY_NOTICE_KEY]
        txn = txns.get(notice.get("txn_id"))
        if notice.get("backfill"):
            result.backfill.append((row, notice.get("verdict") or "", txn))
        elif txn is not None:
            result.fresh.append((row, txn))
        else:
            # The transaction is gone (purged with its interaction's day):
            # nothing to tell; stamp it so the poll stops seeing it.
            mark_notified(row, now=datetime.now(settings.tz))
    return result


def mark_notified(interaction: Interaction, *, now: datetime) -> None:
    interaction.meta = {**(interaction.meta or {}), MONEY_NOTIFIED_KEY: now.isoformat()}
