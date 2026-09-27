"""Tomorrow planner (spec §8): `/reja`, and the SQL listing in the daily report.

All inputs are SQL. For `/reja` the reasoning model arranges them into a
realistic time-blocked Uzbek schedule around the fixed event times; it never
sees an amount, so it cannot mis-copy one. The daily report's "Ertaga"
section is the deterministic listing itself (no model). If the API call
fails, `/reja` still returns the listing — a plan must never silently vanish.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from miya.bot.formatting import escape, money, ref, tags
from miya.config import settings
from miya.services import queries
from miya.services.extraction import API_FAILURES, get_client
from miya.services.usage import record_anthropic_usage

log = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """\
You are MIYA, the planner of ONE owner — a freight-forwarding business owner
working between China and Uzbekistan. Given tomorrow's fixed events, due and
overdue tasks, promises and debts, produce a realistic time-blocked plan for
his day in Uzbek (Latin script).

Rules:
- Fixed event times are immovable; schedule everything else around them.
- Overdue items come first in the morning; group errands sensibly.
- Every date and name must be copied verbatim from the input data — never
  invent or alter them.
- Never write any amount or currency; refer to debts by name and ref.
- Keep it short: a time-blocked list plus at most two sentences of advice.
- Format for Telegram: plain text, <b>bold</b> for times, no markdown.
"""


@dataclass(slots=True)
class PlanInputs:
    day: date
    events: list = field(default_factory=list)
    due: dict = field(default_factory=dict)  # debts / promises / tasks


async def plan_inputs(session: AsyncSession, day: date) -> PlanInputs:
    start, end = queries.day_bounds(day)
    horizon = (day - datetime.now(settings.tz).date()).days + 3
    return PlanInputs(
        day=day,
        events=await queries.events_between(session, start, end),
        due=await queries.due_items(session, horizon_days=max(horizon, 1)),
    )


def render_inputs(
    inputs: PlanInputs, *, header: bool = True, amounts: bool = True
) -> str:
    """Deterministic listing: the report's "Ertaga", /reja's fallback, and
    (with ``amounts=False``) the model's input for /reja.

    Titles, names and descriptions are escaped for the same reason as in
    reports.py — they are attacker-influenced and end up in an HTML message.
    Without amounts a debt line names the person, the side, the due date and
    its refs only: figures reach the owner from SQL, never through a model.
    """
    lines: list[str] = [f"REJA KUNI: {inputs.day.isoformat()}"] if header else []

    if header:
        lines.append("")
    lines.append("BELGILANGAN UCHRASHUVLAR:")
    if inputs.events:
        for ev in inputs.events:
            when = ev.start_at.astimezone(settings.tz).strftime("%H:%M")
            where = f" ({escape(ev.location)})" if ev.location else ""
            lines.append(f"- {when} — {escape(ev.title)}{where}")
    else:
        lines.append("- yo'q")

    lines.append("\nMUDDATI KELGAN/O'TGAN VAZIFALAR:")
    tasks = inputs.due.get("tasks", [])
    if tasks:
        for t in tasks:
            lines.append(f"- {escape(t.description)} (muddat: {t.due_date})")
    else:
        lines.append("- yo'q")

    lines.append("\nVA'DALAR:")
    promises = inputs.due.get("promises", [])
    if promises:
        for p, person in promises:
            lines.append(
                f"- {escape(person.display_name)}: {escape(p.description)} "
                f"(muddat: {p.due_date})"
            )
    else:
        lines.append("- yo'q")

    lines.append("\nQARZLAR (muddati yaqin):")
    debts = inputs.due.get("debts", [])
    if debts:
        for b in debts:
            side = "sizdan qarzi" if b.direction.value == "they_owe_me" else "qarzingiz"
            name = escape(b.person.display_name)
            if amounts:
                lines.append(
                    f"- {name}: {money(b.outstanding, b.currency)} "
                    f"({side}, muddat: {b.earliest_due}){tags('debt', b.ids)}"
                )
            else:
                handles = ", ".join(ref("debt", i) for i in b.ids if i is not None)
                lines.append(
                    f"- {name}: qarz ({side}, muddat: {b.earliest_due}) [{handles}]"
                )
    else:
        lines.append("- yo'q")

    return "\n".join(lines)


async def plan_for(session: AsyncSession, day: date) -> str:
    """A plan for one day. Falls back to the raw listing when the model fails."""
    inputs = await plan_inputs(session, day)
    data_block = render_inputs(inputs, amounts=False)
    listing = render_inputs(inputs)

    try:
        response = await get_client().messages.create(
            model=settings.reason_model,
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": PLANNER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": data_block}],
        )
    except API_FAILURES as exc:
        log.warning("planner call failed, falling back to raw listing: %s", exc)
        return listing

    await record_anthropic_usage(
        session,
        model=settings.reason_model,
        operation="planner",
        usage=response.usage,
    )
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return text or listing


async def plan_tomorrow(session: AsyncSession) -> str:
    tomorrow = datetime.now(settings.tz).date() + timedelta(days=1)
    return await plan_for(session, tomorrow)
