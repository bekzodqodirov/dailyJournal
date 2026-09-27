"""Recompute ``usage_log.cost_usd`` for Anthropic calls from stored tokens.

    make reprice SINCE=2026-09-01 [DRY=1]

Costs are MIYA's own estimate from the price table (or EXTRACT_MODEL_PRICE /
REASON_MODEL_PRICE); when a price was wrong or missing, this rewrites the
recorded figures from the token counts, which are kept verbatim.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.models import UsageLog
from miya.db.session import engine, session_scope
from miya.services import queries
from miya.services.usage import anthropic_cost_usd

SUMMARY = "{n} ta yozuv qayta hisoblandi: {old} → {new}"


async def reprice(
    session: AsyncSession, since: date, *, dry: bool = False
) -> tuple[int, Decimal, Decimal]:
    """(rows changed, old total, new total) over Anthropic rows since ``since``."""
    start, _ = queries.day_bounds(since)
    rows = list(
        await session.scalars(
            sa.select(UsageLog)
            .where(UsageLog.provider == "anthropic")
            .where(UsageLog.created_at >= start)
            .order_by(UsageLog.id)
        )
    )
    changed = 0
    old_total = new_total = Decimal("0")
    for row in rows:
        new = anthropic_cost_usd(
            row.model or "",
            input_tokens=row.input_tokens or 0,
            output_tokens=row.output_tokens or 0,
            cache_read_tokens=row.cache_read_tokens or 0,
            cache_write_tokens=row.cache_write_tokens or 0,
            batch=bool(row.batch),
        )
        old_total += row.cost_usd or Decimal("0")
        new_total += new or Decimal("0")
        if new != row.cost_usd:
            changed += 1
            if not dry:
                row.cost_usd = new
    if not dry:
        await session.flush()
    return changed, old_total, new_total


async def _main(since: date, dry: bool) -> str:
    try:
        async with session_scope() as session:
            n, old, new = await reprice(session, since, dry=dry)
            if dry:
                await session.rollback()
    finally:
        await engine.dispose()
    line = SUMMARY.format(n=n, old=f"${old:.4f}", new=f"${new:.4f}")
    return line + (" (sinov: hech narsa yozilmadi)" if dry else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("since", help="YYYY-MM-DD, in the owner's timezone")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args(argv)
    try:
        since = date.fromisoformat(args.since)
    except ValueError:
        print("SINCE=YYYY-MM-DD", file=sys.stderr)
        return 1
    print(asyncio.run(_main(since, args.dry)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
