"""Daily report (spec §8): cron at REPORT_TIME and the `/hisobot` command.

The day's numbers are gathered by SQL (services/queries.py) and rendered
deterministically — no model ever touches the report. It used to be the
reasoning model's rewrite of this block, which let one mis-copied figure reach
the owner's main evening money surface; money comes from SQL only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from miya.bot.formatting import (
    escape,
    full_date,
    missed_line,
    question_line,
    queue_line,
    quiet_line,
    ref,
)
from miya.bot.formatting import money as format_money
from miya.config import settings
from miya.db.models import DailyReport
from miya.services import loops, nudges, planner, queries, questions
from miya.services.loops import MissedCall, QuietCounterparty, UnansweredQuestion

log = logging.getLogger(__name__)

# Section headings, in order. Bold HTML: the report goes out with
# parse_mode=HTML, and everything else in it is escaped.
H_MONEY = "💰 <b>Pul</b>"
# Money texts (WP-14). replies.py reuses the first for the brief.
MONEY_REVIEW_LINE = "🔎 {n} ta pul xabari tekshiruv kutmoqda — /tekshir"
REPORT_IGNORED_LINE = (
    "🙈 Bugun {n} ta pul xabari e'tiborsiz qoldirildi (kod, reklama) — /tekshir hammasi"
)
AUTO_RESOLVED_LINE = "🤖 Bugun {n} ta savolni o'zim hal qildim — /savollar hal"
H_PEOPLE = "👥 <b>Muloqotlar</b>"
H_NEW = "🧾 <b>Yangi qarz va va'dalar</b>"
H_DUE = "⏰ <b>Ochiq va muddati o'tganlar</b>"
H_DONE = "✅ <b>Bajarilganlar</b>"
H_CHATS = "💬 <b>Chatlarda</b>"
H_TO_ME = "📨 <b>Sizga murojaatlar</b>"
H_QUESTIONS = "❓ <b>Javobsiz qolganlar</b>"
H_MISSED = "📵 <b>Javobsiz qo'ng'iroqlar</b>"
H_QUIET = "🤫 <b>Jim bo'lib qolganlar</b>"
H_TOMORROW = "📅 <b>Ertaga</b>"
HEADINGS = (
    H_MONEY,
    H_PEOPLE,
    H_NEW,
    H_DUE,
    H_DONE,
    H_CHATS,
    H_TO_ME,
    H_QUESTIONS,
    H_MISSED,
    H_QUIET,
    H_TOMORROW,
)


@dataclass(slots=True)
class ReportData:
    day: date
    summary: queries.DaySummary
    completed: queries.CompletedToday
    due: dict
    plan: str
    chats: list = field(default_factory=list)
    to_me: list = field(default_factory=list)
    # Open loops (build step 2): questions nobody answered, people who went
    # quiet with something open. Both from the loops engine — SQL, no model.
    questions: list[UnansweredQuestion] = field(default_factory=list)
    quiet: list[QuietCounterparty] = field(default_factory=list)
    # Missed calls nobody returned (build step 6) — the same engine.
    missed: list[MissedCall] = field(default_factory=list)
    # What a counterparty asserted and the owner has not answered (build
    # step 3). A count only: the questions themselves have their buttons on
    # the receipt, the brief and /davolar, and the report is not a place to
    # answer from.
    # Everything still waiting for the owner's tap (WP-19): one count line.
    queue: questions.QueueSummary | None = None
    # Money texts (WP-14): waiting in /tekshir, and ignored today (codes,
    # adverts) — counts only, so the owner knows both exist.
    money_review: int = 0
    money_ignored: int = 0
    # Questions MIYA settled itself today (WP-45): said, never silent.
    auto_resolved: int = 0


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
        "chats": [
            {"title": d.title, "messages": d.messages, "to_me": len(d.to_me)}
            for d in data.chats
        ],
        "to_me": len(data.to_me),
        "unanswered": len(data.questions),
        "quiet": len(data.quiet),
        "missed": len(data.missed),
        "questions_waiting": data.queue.waiting if data.queue is not None else 0,
        "money_review": data.money_review,
        "money_ignored": data.money_ignored,
        "settled_debts": len(data.completed.settled_debts),
        "done_promises": len(data.completed.done_promises),
        "done_tasks": len(data.completed.done_tasks),
        "model": False,
    }


def report_header(day: date) -> str:
    """ "📊 Kunlik hisobot · 25-sentabr 2026" — the message's first line."""
    return f"📊 <b>Kunlik hisobot</b> · {full_date(day)}"


def render_data_block(data: ReportData) -> str:
    """The daily report, rendered from SQL. No model.

    Names and descriptions are HTML-escaped here. They come from Telegram
    contacts and message text, which a counterparty controls: a supplier who
    sets a first name to "<b" would otherwise produce a report Telegram
    refuses to render, silently costing the owner the evening summary. The
    only markup is the section headings.
    """
    s = data.summary
    lines: list[str] = [H_MONEY]
    if s.income or s.expense:
        for cur, total in s.income.items():
            lines.append(f"- kirim: {format_money(total, cur)}")
        for cur, total in s.expense.items():
            lines.append(f"- chiqim: {format_money(total, cur)}")
        for category, cur, total in s.by_category:
            lines.append(f"- {escape(category)}: {format_money(total, cur)}")
        if s.biggest:
            lines.append("- eng katta xarajatlar:")
            for txn in s.biggest:
                what = escape(txn.description or txn.category or "?")
                lines.append(f"  • {format_money(txn.amount, txn.currency)} — {what}")
    else:
        lines.append("- bugun pul harakati yozilmadi")
    if data.money_review:
        lines.append(MONEY_REVIEW_LINE.format(n=data.money_review))
    if data.money_ignored:
        lines.append(REPORT_IGNORED_LINE.format(n=data.money_ignored))
    if data.auto_resolved:
        lines.append(AUTO_RESOLVED_LINE.format(n=data.auto_resolved))

    lines.append("\n" + H_PEOPLE)
    lines.append(f"- jami {s.interactions} ta yozuv")
    for person, n in s.people_seen[:10]:
        lines.append(f"- {escape(person.display_name)}: {n} ta")

    lines.append("\n" + H_NEW)
    if s.new_debts or s.new_promises:
        lines.append(f"- yangi qarzlar: {len(s.new_debts)} ta")
        lines.append(f"- yangi va'dalar: {len(s.new_promises)} ta")
    else:
        lines.append("- yo'q")

    lines.append("\n" + H_DUE)
    due_lines = 0
    # Every open line carries its ref (d12 / p7 / t3), so the owner can
    # /bajarildi it straight from the report.
    for b in data.due.get("debts", []):
        side = "sizdan qarzi" if b.direction.value == "they_owe_me" else "qarzingiz"
        handles = ", ".join(ref("debt", i) for i in b.ids)
        lines.append(
            f"- {escape(b.person.display_name)}: "
            f"{format_money(b.outstanding, b.currency)} ({side}, "
            f"muddat: {b.earliest_due}) [{handles}]"
        )
        due_lines += 1
    for p, person in data.due.get("promises", []):
        lines.append(
            f"- va'da: {escape(person.display_name)} — {escape(p.description)} "
            f"[{ref('promise', p.id)}]"
        )
        due_lines += 1
    for t in data.due.get("tasks", []):
        lines.append(
            f"- vazifa: {escape(t.description)} (muddat: {t.due_date}) "
            f"[{ref('task', t.id)}]"
        )
        due_lines += 1
    if not due_lines:
        lines.append("- yo'q")

    lines.append("\n" + H_DONE)
    c = data.completed
    if c.settled_debts or c.done_promises or c.done_tasks:
        if c.settled_debts:
            lines.append(f"- yopilgan qarzlar: {len(c.settled_debts)} ta")
        for p in c.done_promises:
            who = escape(p.person.display_name) if p.person is not None else "?"
            lines.append(f"- va'da bajarildi: {who} — {escape(p.description)}")
        for t in c.done_tasks:
            lines.append(f"- vazifa bajarildi: {escape(t.description)}")
    else:
        lines.append("- yo'q")

    lines.append("\n" + H_CHATS)
    if data.chats:
        for digest in data.chats[:8]:
            head = f"- {escape(digest.title)} ({digest.messages} ta xabar)"
            if digest.to_me:
                head += f", {len(digest.to_me)} tasi sizga"
            lines.append(head)
            for summary in digest.summaries[:2]:
                lines.append(f"  • {escape(summary)}")
    else:
        lines.append("- yo'q")

    lines.append("\n" + H_TO_ME)
    if data.to_me:
        for interaction in data.to_me[:10]:
            body = (interaction.raw_text or interaction.transcript or "").strip()
            when = interaction.occurred_at.astimezone(settings.tz).strftime("%H:%M")
            lines.append(f"- {when}: {escape(body[:120] or '[media]')}")
    else:
        lines.append("- yo'q")

    # The two open-loop sections are the brief's own lines rendered plain
    # (formatting.question_line / quiet_line with markup=False): no tags, so
    # escaping the finished line is the same as escaping each name in it.
    lines.append("\n" + H_QUESTIONS)
    if data.questions:
        for q in data.questions[:10]:
            lines.append(f"- {escape(question_line(q, markup=False))}")
    else:
        lines.append("- yo'q")

    # Missed calls (build step 6): the brief's own lines rendered plain, the
    # same way as the two sections around it. Shown only when there are any —
    # a phone-less install should not read an empty section every evening.
    if data.missed:
        lines.append("\n" + H_MISSED)
        for miss in data.missed[:10]:
            lines.append(f"- {escape(missed_line(miss, markup=False))}")

    lines.append("\n" + H_QUIET)
    if data.quiet:
        for q in data.quiet[:10]:
            lines.append(f"- {escape(quiet_line(q, markup=False))}")
    else:
        lines.append("- yo'q")

    line = queue_line(data.queue, markup=False)
    if line:
        lines.append("\n" + escape(line))

    lines.append("\n" + H_TOMORROW)
    lines.append(data.plan)

    return "\n".join(lines)


async def gather(session: AsyncSession, day: date) -> ReportData:
    return ReportData(
        day=day,
        summary=await queries.day_summary(session, day),
        completed=await queries.completed_on(session, day),
        due=await queries.due_items(session, horizon_days=1),
        # The SQL listing, not /reja's model-written plan: no figure in the
        # report passes through a model.
        plan=planner.render_inputs(
            await planner.plan_inputs(session, day + timedelta(days=1)), header=False
        ),
        chats=await queries.chat_digests(session, day),
        to_me=await queries.messages_to_me(session, day),
        questions=await nudges.unanswered_questions(session),
        quiet=await loops.quiet_counterparties(session),
        missed=await loops.missed_calls(session),
        queue=questions.summarise(await questions.collect(session, for_push=False), []),
        money_review=await queries.money_review_count(session),
        money_ignored=await queries.ignored_money_count(
            session, *queries.day_bounds(day)
        ),
        auto_resolved=(
            await questions.auto_resolved_since(session, queries.day_bounds(day)[0])
        ).total,
    )


async def generate_report(session: AsyncSession, day: date | None = None) -> str:
    """Compose, store (upsert by date) and return the day's report."""
    day = day or datetime.now(settings.tz).date()
    data = await gather(session, day)
    content = render_data_block(data)

    # /hisobot can be called repeatedly, and the worker cron can race a manual
    # /hisobot on the same date — an upsert makes last-writer-wins instead of
    # a unique-violation that would cost one of them its report.
    await session.execute(
        insert(DailyReport)
        .values(report_date=day, content=content, stats=_stats_json(data))
        .on_conflict_do_update(
            index_elements=[DailyReport.report_date],
            set_={"content": content, "stats": _stats_json(data)},
        )
    )
    await session.flush()
    return content
