"""Shared builders for tests of the evening recap (WP-53)."""

from __future__ import annotations

from datetime import datetime

from miya.bot import recap_text
from miya.config import settings
from miya.services import recaps
from miya.services.queries import MoneyDay, day_bounds


def activity(day=None, **fields) -> recaps.DayActivity:
    day = day or datetime.now(settings.tz).date()
    start, end = day_bounds(day)
    base = {"start": start, "end": end, "now": end, "money": MoneyDay()}
    return recaps.DayActivity(**{**base, **fields})


def render(act=None, *, prose=None, tomorrow=None, queue=None, day=None, **kw) -> str:
    act = act or activity(day)
    parts = recap_text.evening_parts(
        act,
        prose or {},
        tomorrow,
        queue,
        day=day or act.start.date(),
        tz=settings.tz,
        **kw,
    )
    return "\n\n".join(parts)


async def recap_of(session, day=None, *, now=None) -> str:
    now = now or datetime.now(settings.tz)
    day = day or now.date()
    result = await recaps.build_evening(session, day, now=now, store=False)
    return "\n\n".join(result.parts)
