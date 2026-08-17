"""Daily report (spec §8): cron at REPORT_TIME and the `/hisobot` command.

The day's numbers are gathered by SQL (services/queries.py) and rendered into
a deterministic data block; Sonnet only turns that block into a clean Uzbek
report. If the API call fails the deterministic block itself is stored and
sent — a report day is never lost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import anthropic
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.bot.formatting import money as format_money
from miya.config import settings
from miya.db.models import DailyReport
from miya.services import planner, queries
from miya.services.extraction import get_client
from miya.services.usage import record_anthropic_usage

log = logging.getLogger(__name__)

REPORT_SYSTEM_PROMPT = """\
You are MIYA, writing the end-of-day report for ONE owner — a freight-
forwarding business owner working between China and Uzbekistan. You get a
deterministic data block (SQL results) and must compose a compact, readable
daily report in Uzbek (Latin script).

Rules:
- Every figure, name and date must be copied verbatim from the data block —
  never invent, estimate or alter numbers. Amounts may be reformatted for
  readability ("5000000 UZS" → "5 mln UZS") but never changed.
- Follow this section order, skipping sections with no data:
  💰 Pul (kirim/chiqim, kategoriyalar, eng katta xarajatlar)
  👥 Muloqotlar (kim bilan, nechta)
  🧾 Yangi qarz va va'dalar
  ⏰ Ochiq va muddati o'tganlar
  ✅ Bajarilganlar
  📅 Ertaga (given plan text — include as-is, lightly trimmed if long)
- Keep the whole report short and scannable. Telegram formatting: plain text
  with <b>bold</b> section titles, no markdown, no # headers.
"""


@dataclass(slots=True)
class ReportData:
    day: date
    summary: queries.DaySummary
    completed: queries.CompletedToday
    due: dict
    plan: str


def _stats_json(data: ReportData) -> dict[str, Any]:
    """JSON-safe snapshot for daily_reports.stats (audit / future dashboard)."""
    return {
        "income": {c.value: str(v) for c, v in data.summary.income.items()},
        "expense": {c.value: str(v) for c, v in data.summary.expense.items()},
        "by_category": [
            {"category": c, "currency": cur.value, "total": str(total)}
            for c, cur, total in data.summary.by_category
        ],
        "people_seen": [
            {"person": p.display_name, "interactions": n}
            for p, n in data.summary.people_seen
        ],
        "new_debts": len(data.summary.new_debts),
        "new_promises": len(data.summary.new_promises),
        "interactions": data.summary.interactions,
        "settled_debts": len(data.completed.settled_debts),
        "done_promises": len(data.completed.done_promises),
        "done_tasks": len(data.completed.done_tasks),
    }


def render_data_block(data: ReportData) -> str:
    """Deterministic report input — also the no-API fallback report."""
    s = data.summary
    lines: list[str] = [f"HISOBOT KUNI: {data.day.isoformat()}"]

    lines.append("\n💰 PUL:")
    if s.income or s.expense:
        for cur, total in s.income.items():
            lines.append(f"- kirim: {format_money(total, cur)}")
        for cur, total in s.expense.items():
            lines.append(f"- chiqim: {format_money(total, cur)}")
        for category, cur, total in s.by_category:
            lines.append(f"- {category}: {format_money(total, cur)}")
    else:
        lines.append("- bugun pul harakati yozilmadi")

    lines.append("\n👥 MULOQOTLAR:")
    lines.append(f"- jami {s.interactions} ta yozuv")
    for person, n in s.people_seen[:10]:
        lines.append(f"- {person.display_name}: {n} ta")

    lines.append("\n🧾 YANGI QARZ/VA'DALAR:")
    if s.new_debts or s.new_promises:
        lines.append(f"- yangi qarzlar: {len(s.new_debts)} ta")
        lines.append(f"- yangi va'dalar: {len(s.new_promises)} ta")
    else:
        lines.append("- yo'q")

    lines.append("\n⏰ OCHIQ VA MUDDATI O'TGANLAR:")
    due_lines = 0
    for b in data.due.get("debts", []):
        side = "sizdan qarzi" if b.direction.value == "they_owe_me" else "qarzingiz"
        lines.append(
            f"- {b.person.display_name}: "
            f"{format_money(b.outstanding, b.currency)} ({side}, "
            f"muddat: {b.earliest_due})"
        )
        due_lines += 1
    for p, person in data.due.get("promises", []):
        lines.append(f"- va'da: {person.display_name} — {p.description}")
        due_lines += 1
    for t in data.due.get("tasks", []):
        lines.append(f"- vazifa: {t.description} (muddat: {t.due_date})")
        due_lines += 1
    if not due_lines:
        lines.append("- yo'q")

    lines.append("\n✅ BAJARILGANLAR:")
    c = data.completed
    if c.settled_debts or c.done_promises or c.done_tasks:
        if c.settled_debts:
            lines.append(f"- yopilgan qarzlar: {len(c.settled_debts)} ta")
        for p in c.done_promises:
            lines.append(f"- va'da bajarildi: {p.description}")
        for t in c.done_tasks:
            lines.append(f"- vazifa bajarildi: {t.description}")
    else:
        lines.append("- yo'q")

    lines.append("\n📅 ERTAGA:")
    lines.append(data.plan)

    return "\n".join(lines)


async def gather(session: AsyncSession, day: date) -> ReportData:
    return ReportData(
        day=day,
        summary=await queries.day_summary(session, day),
        completed=await queries.completed_on(session, day),
        due=await queries.due_items(session, horizon_days=1),
        plan=await planner.plan_for(session, day + timedelta(days=1)),
    )


async def generate_report(session: AsyncSession, day: date | None = None) -> str:
    """Compose, store (upsert by date) and return the day's report."""
    day = day or datetime.now(settings.tz).date()
    data = await gather(session, day)
    data_block = render_data_block(data)

    content = ""
    try:
        response = await get_client().messages.create(
            model=settings.reason_model,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": REPORT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": data_block}],
        )
        await record_anthropic_usage(
            session,
            model=settings.reason_model,
            operation="report",
            usage=response.usage,
        )
        content = "\n".join(b.text for b in response.content if b.type == "text").strip()
    except anthropic.APIError as exc:
        log.warning("report call failed, storing the raw data block: %s", exc)

    content = content or data_block

    existing = await session.scalar(
        sa.select(DailyReport).where(DailyReport.report_date == day)
    )
    if existing is None:
        session.add(
            DailyReport(report_date=day, content=content, stats=_stats_json(data))
        )
    else:
        # /hisobot can be called repeatedly; the evening cron also overwrites
        # an earlier manual run with the full day's picture.
        existing.content = content
        existing.stats = _stats_json(data)
    await session.flush()
    return content
