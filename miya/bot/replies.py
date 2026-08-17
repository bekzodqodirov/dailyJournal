"""Owner-facing message bodies, in Uzbek."""

from __future__ import annotations

from miya.bot.formatting import (
    PRIORITY_LABEL,
    bullet_list,
    clock,
    debt_line,
    escape,
    full_date,
    money,
    relative_day,
    short_date,
)
from miya.db.enums import DebtDirection, PromiseMadeBy
from miya.services.persistence import Applied
from miya.services.queries import DaySummary, DebtBalance, PersonSummary

FAILED_EXTRACTION_HINT = (
    "⚠️ Yozib oldim, lekin ma'lumot ajratib bo'lmadi — keyinroq qayta ko'raman."
)

HELP = """\
<b>MIYA</b> — sizning ikkinchi miyangiz.

Menga shunchaki yozing, ovozli xabar yuboring yoki chek rasmini tashlang —
qarz, va'da, xarajat va vazifalarni o'zim ajratib olib yozib qo'yaman.

<b>Buyruqlar</b>
/qarz — ochiq qarzlar
/vada — ochiq va'dalar
/bugun — bugungi holat
/kim &lt;ism&gt; — odam bo'yicha xulosa
/yordam — shu ro'yxat
"""


def confirmation(applied: Applied) -> str:
    """Short receipt of what was recorded, so nothing lands silently."""
    if applied.is_empty():
        if applied.facts:
            return "📝 Yozib oldim."
        return "📝 Yozib oldim — alohida qarz, xarajat yoki va'da topilmadi."

    lines: list[str] = []

    for debt in applied.debts:
        who = "senga" if debt.direction is DebtDirection.they_owe_me else "sen"
        arrow = "→" if debt.direction is DebtDirection.they_owe_me else "←"
        tail = f", muddat: {short_date(debt.due_date)}" if debt.due_date else ""
        lines.append(f"💰 Qarz: {arrow} {who} {money(debt.amount, debt.currency)}{tail}")

    for person, payment in applied.settlements:
        lines.append(
            f"✅ To'lov: {escape(person.display_name)} — "
            f"{money(payment.amount, payment.currency)}"
        )

    for name, amount, currency in applied.unmatched_settlements:
        lines.append(
            f"⚠️ {escape(name)} {money(amount, currency)} to'ladi, "
            f"lekin unga mos ochiq qarz topilmadi"
        )

    for promise in applied.promises:
        who = "Men" if promise.made_by is PromiseMadeBy.me else "U"
        tail = f" ({short_date(promise.due_date)})" if promise.due_date else ""
        lines.append(f"🤝 Va'da: {who} — {escape(promise.description)}{tail}")

    for txn in applied.transactions:
        icon = "📈" if txn.type.value == "income" else "📉"
        label = "Kirim" if txn.type.value == "income" else "Chiqim"
        detail = f" ({escape(txn.description)})" if txn.description else ""
        lines.append(
            f"{icon} {label}: {money(txn.amount, txn.currency)} · "
            f"{escape(txn.category or 'boshqa')}{detail}"
        )

    for event in applied.events:
        lines.append(
            f"📅 Uchrashuv: {escape(event.title)} — "
            f"{short_date(event.start_at.date())} {clock(event.start_at)}"
        )

    for task in applied.tasks:
        tail = f" ({short_date(task.due_date)})" if task.due_date else ""
        priority = (
            f" [{PRIORITY_LABEL[task.priority]}]" if task.priority.value == "high" else ""
        )
        lines.append(f"✔️ Vazifa: {escape(task.description)}{tail}{priority}")

    if applied.facts:
        lines.append(f"🧠 {applied.facts} ta yangi ma'lumot eslab qolindi")

    return "\n".join(lines)


def debts_report(balances: list[DebtBalance]) -> str:
    if not balances:
        return "✅ Ochiq qarz yo'q."

    they_owe = [b for b in balances if b.direction is DebtDirection.they_owe_me]
    i_owe = [b for b in balances if b.direction is DebtDirection.i_owe_them]
    blocks: list[str] = []

    if they_owe:
        lines = [
            f"{escape(b.person.display_name)}: {money(b.outstanding, b.currency)}"
            + (f" · {relative_day(b.earliest_due)}" if b.earliest_due else "")
            for b in they_owe
        ]
        blocks.append("<b>Senga qarzdorlar</b>\n" + bullet_list(lines, empty="—"))

    if i_owe:
        lines = [
            f"{escape(b.person.display_name)}: {money(b.outstanding, b.currency)}"
            + (f" · {relative_day(b.earliest_due)}" if b.earliest_due else "")
            for b in i_owe
        ]
        blocks.append("<b>Sen qarzdorsan</b>\n" + bullet_list(lines, empty="—"))

    return "\n\n".join(blocks)


def promises_report(items) -> str:
    if not items:
        return "✅ Ochiq va'da yo'q."

    mine = [
        f"{escape(p.description)} — {escape(person.display_name)}"
        + (f" · {relative_day(p.due_date)}" if p.due_date else "")
        for p, person in items
        if p.made_by is PromiseMadeBy.me
    ]
    theirs = [
        f"{escape(person.display_name)}: {escape(p.description)}"
        + (f" · {relative_day(p.due_date)}" if p.due_date else "")
        for p, person in items
        if p.made_by is PromiseMadeBy.them
    ]

    blocks = []
    if mine:
        blocks.append("<b>Sen va'da bergansan</b>\n" + bullet_list(mine, empty="—"))
    if theirs:
        blocks.append("<b>Senga va'da berishgan</b>\n" + bullet_list(theirs, empty="—"))
    return "\n\n".join(blocks)


def day_report(summary: DaySummary) -> str:
    parts = [f"<b>{full_date(summary.day)}</b>"]

    if summary.income or summary.expense:
        money_lines = []
        for currency, total in summary.income.items():
            money_lines.append(f"Kirim: {money(total, currency)}")
        for currency, total in summary.expense.items():
            money_lines.append(f"Chiqim: {money(total, currency)}")
        parts.append("💰 <b>Pul</b>\n" + bullet_list(money_lines, empty="—"))

        if summary.by_category:
            cats = [
                f"{escape(category)}: {money(total, currency)}"
                for category, currency, total in summary.by_category[:5]
            ]
            parts.append("📉 <b>Kategoriya bo'yicha</b>\n" + bullet_list(cats, empty="—"))
    else:
        parts.append("💰 <b>Pul</b>\n• Bugun pul harakati yo'q")

    if summary.people_seen:
        people = [
            f"{escape(person.display_name)} ({count})"
            for person, count in summary.people_seen[:10]
        ]
        parts.append("👥 <b>Muloqotlar</b>\n" + bullet_list(people, empty="—"))

    if summary.new_debts or summary.new_promises:
        counts = []
        if summary.new_debts:
            counts.append(f"{len(summary.new_debts)} ta yangi qarz")
        if summary.new_promises:
            counts.append(f"{len(summary.new_promises)} ta yangi va'da")
        parts.append("🧾 " + ", ".join(counts))

    parts.append(f"\n<i>{summary.interactions} ta yozuv</i>")
    return "\n\n".join(parts)


def person_report(summary: PersonSummary) -> str:
    person = summary.person
    parts = [f"<b>{escape(person.display_name)}</b>"]

    if person.aliases:
        parts.append(f"<i>{escape(', '.join(person.aliases))}</i>")

    if summary.balances:
        lines = [
            debt_line(
                escape(b.person.display_name),
                b.direction,
                b.outstanding,
                b.currency,
                b.earliest_due,
            )
            for b in summary.balances
        ]
        parts.append("💰 <b>Qarzlar</b>\n" + bullet_list(lines, empty="—"))
    else:
        parts.append("💰 Ochiq qarz yo'q")

    if summary.open_promises:
        lines = [
            ("Men: " if p.made_by is PromiseMadeBy.me else "U: ")
            + escape(p.description)
            + (f" · {relative_day(p.due_date)}" if p.due_date else "")
            for p in summary.open_promises
        ]
        parts.append("🤝 <b>Va'dalar</b>\n" + bullet_list(lines, empty="—"))

    if summary.last_interactions:
        last = summary.last_interactions[0]
        parts.append(
            f"🕐 Oxirgi aloqa: {short_date(last.occurred_at.date())} "
            f"{clock(last.occurred_at)} · jami {summary.total_interactions} ta"
        )

    return "\n\n".join(parts)


def reminder(debts, promises, tasks, events) -> str:
    """Body of an hourly reminder ping. Empty string means nothing to send."""
    blocks: list[str] = []

    if debts:
        lines = [
            f"{escape(b.person.display_name)}: {money(b.outstanding, b.currency)} · "
            f"{relative_day(b.earliest_due)}"
            for b in debts
        ]
        blocks.append("💰 <b>Qarz muddati</b>\n" + bullet_list(lines, empty="—"))

    if promises:
        lines = [
            f"{escape(person.display_name)}: {escape(p.description)} · "
            f"{relative_day(p.due_date)}"
            for p, person in promises
        ]
        blocks.append("🤝 <b>Va'da muddati</b>\n" + bullet_list(lines, empty="—"))

    if tasks:
        lines = [f"{escape(t.description)} · {relative_day(t.due_date)}" for t in tasks]
        blocks.append("✔️ <b>Vazifalar</b>\n" + bullet_list(lines, empty="—"))

    if events:
        lines = [f"{escape(e.title)} · {clock(e.start_at)}" for e in events]
        blocks.append("📅 <b>Yaqin uchrashuvlar</b>\n" + bullet_list(lines, empty="—"))

    return "\n\n".join(blocks)
