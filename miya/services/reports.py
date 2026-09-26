"""The evening report (spec §8): cron at REPORT_TIME and `/hisobot`.

Since WP-53 the report *is* the evening recap "🌆 Bugun nima bo'ldi"
(services/recaps.py, rendered by bot/recap_text.py): every figure from SQL,
at most a labelled sentence or two per person from the model. This module
keeps the old entry point for the worker, the bot and the API.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.services import recaps

EVENING = recaps.EVENING
# Money texts (WP-14). replies.py reuses it for the brief.
MONEY_REVIEW_LINE = "🔎 {n} ta pul xabari tekshiruv kutmoqda — /tekshir"


async def generate_report(
    session: AsyncSession,
    day: date | None = None,
    *,
    now: datetime | None = None,
    store: bool = True,
) -> str:
    """The day's recap as one text (its parts joined). With ``store`` it is
    kept as the day's evening row for the worker to deliver (WP-49)."""
    now = now or datetime.now(settings.tz)
    day = day or now.astimezone(settings.tz).date()
    result = await recaps.build_evening(session, day, now=now, store=store)
    return "\n\n".join(result.parts)
