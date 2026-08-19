"""Owner-facing message bodies, in Uzbek."""

from __future__ import annotations

from miya.bot.formatting import (
    PRIORITY_LABEL,
    TELEGRAM_LIMIT,
    bullet_list,
    clip,
    clock,
    debt_line,
    escape,
    full_date,
    money,
    relative_day,
    short_date,
    usd,
)
from miya.db.enums import DebtDirection, PromiseMadeBy
from miya.services.persistence import Applied
from miya.services.queries import DaySummary, DebtBalance, PersonSummary

FAILED_EXTRACTION_HINT = (
    "⚠️ Yozib oldim, lekin ma'lumot ajratib bo'lmadi — /tekshir ro'yxatida turadi."
)

TRANSCRIPTION_FAILED_HINT = (
    "⚠️ Ovozni matnga o'girib bo'lmadi. Fayl saqlandi — /tekshir ro'yxatida turadi."
)

PHOTO_FAILED_HINT = "⚠️ Rasmni o'qib bo'lmadi. Saqlandi — /tekshir ro'yxatida turadi."

VISION_PARTIAL_HINT = (
    "⚠️ Rasmning o'zini o'qib bo'lmadi — faqat izoh bo'yicha yozdim. "
    "/tekshir ro'yxatida turadi."
)


HELP = """\
<b>MIYA</b> — sizning ikkinchi miyangiz.

Menga shunchaki yozing, ovozli xabar yuboring yoki chek rasmini tashlang —
qarz, va'da, xarajat va vazifalarni o'zim ajratib olib yozib qo'yaman.

Savol bersangiz (masalan, «Akmal menga qancha qarz?») — bazadagi aniq
raqamlar bilan javob beraman.

<b>Buyruqlar</b>
/qarz — ochiq qarzlar
/vada — ochiq va'dalar
/bugun — bugungi holat
/kim &lt;ism&gt; — odam bo'yicha xulosa
/qidir &lt;so'z&gt; — xotiradan qidirish
/hisobot — kunlik hisobot
/reja — ertangi reja
/chats — qaysi Telegram chatlar o'qilishi
/process — javob yozilgan media'ni qayta ishlash
/xarajat — MIYA'ning API xarajati
/unut — ma'lumotni butunlay o'chirish
/tekshir — qayta ishlanmagan yozuvlar
/yordam — shu ro'yxat
"""

CHATS_HEADER = (
    "💬 <b>Kuzatilayotgan chatlar</b>\n"
    "<i>Tugmalar: chat nomi — o'qishni yoqadi/o'chiradi, "
    "👁 — rasmlarni tahlil qilish, 📄 — hujjatlarni o'qish.</i>"
)

CHATS_EMPTY = (
    "💬 Hozircha chat ro'yxati bo'sh — userbot ishga tushganda "
    "chatlar avtomatik qo'shiladi."
)

PROCESS_NO_TARGET = (
    "Qayta ishlash uchun kerakli media xabarga <b>javob</b> qilib "
    "<code>/process</code> deb yozing."
)

PROCESS_NO_MEDIA = "Bu xabarda qayta ishlanadigan media yo'q."

VIDEO_STORED_HINT = (
    "🎬 Video saqlandi. Ichidagi gaplarni yozib olishim uchun shu xabarga "
    "javob qilib <code>/process</code> deb yozing."
)

FILE_TOO_BIG_HINT = (
    "⚠️ Fayl 20 MB dan katta — Telegram bot orqali yuklab olib bo'lmaydi. "
    "Kichikroq qilib yuboring yoki matnini yozib yuboring."
)

UNSUPPORTED_HINT = (
    "🤷 Bu turdagi xabarni hali tushunmayman — u yozib olinmadi. "
    "Matn, ovoz, rasm, hujjat yoki video yuboring."
)

DOCUMENT_FAILED_HINT = (
    "⚠️ Hujjatni o'qib bo'lmadi (formati qo'llab-quvvatlanmaydi yoki himoyalangan). "
    "Fayl saqlandi — /tekshir ro'yxatida turadi."
)


def person_not_found(name: str) -> str:
    return f"❓ <b>{escape(name)}</b> topilmadi."


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

    for name, amount, currency in applied.ambiguous_settlements:
        # Both sides are open with this person, so guessing would silently
        # invert the books. Ask instead.
        lines.append(
            f"❓ {escape(name)} bilan {money(amount, currency)} to'lov: "
            f"ikkalangizning ham ochiq qarzingiz bor — kim to'laganini yozing "
            f"(masalan: «{escape(name)} menga {money(amount, currency)} qaytardi»)"
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

    return clip("\n".join(lines))


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

    return clip("\n\n".join(blocks))


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
    return clip("\n\n".join(blocks))


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
    return clip("\n\n".join(parts))


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

    return clip("\n\n".join(parts))


def reminder(debts, promises, tasks, events) -> str:
    """Body of an hourly reminder ping. Empty string means nothing to send."""
    body, _ = reminder_with_counts(debts, promises, tasks, events)
    return body


def reminder_with_counts(debts, promises, tasks, events) -> tuple[str, dict[str, int]]:
    """The ping body, plus how many of each kind actually fit in it.

    Two things this must get right, both learned the hard way:

    * **Only what is shown may be marked as sent.** The caller logs each item
      to suppress it for 24 hours; if a clipped item were logged, the next
      sweep would rebuild the identical head and the tail would starve
      forever, not merely wait.
    * **Events come first.** A debt reminder repeats tomorrow, but a meeting
      only qualifies while it is within the hour — a clipped one is gone.
    """
    sections = [
        (
            "event",
            "📅 <b>Yaqin uchrashuvlar</b>",
            [f"{escape(e.title)} · {clock(e.start_at)}" for e in events],
        ),
        (
            "debt",
            "💰 <b>Qarz muddati</b>",
            [
                f"{escape(b.person.display_name)}: {money(b.outstanding, b.currency)} · "
                f"{relative_day(b.earliest_due)}"
                for b in debts
            ],
        ),
        (
            "promise",
            "🤝 <b>Va'da muddati</b>",
            [
                f"{escape(person.display_name)}: {escape(p.description)} · "
                f"{relative_day(p.due_date)}"
                for p, person in promises
            ],
        ),
        (
            "task",
            "✔️ <b>Vazifalar</b>",
            [f"{escape(t.description)} · {relative_day(t.due_date)}" for t in tasks],
        ),
    ]

    counts = {kind: 0 for kind, _, _ in sections}
    total = {kind: len(lines) for kind, _, lines in sections}
    blocks: list[str] = []

    for kind, header, lines in sections:
        kept: list[str] = []
        for line in lines:
            trial = [*blocks, header + "\n" + bullet_list([*kept, line], empty="—")]
            # Measured, not estimated: the budget has to hold for the joined
            # message, and a marker line may still be appended below.
            if len("\n\n".join(trial)) > TELEGRAM_LIMIT - 60:
                break
            kept.append(line)
        if kept:
            blocks.append(header + "\n" + bullet_list(kept, empty="—"))
            counts[kind] = len(kept)

    dropped = sum(total[k] - counts[k] for k in counts)
    if dropped:
        blocks.append(f"<i>… va yana {dropped} ta — keyingi eslatmada.</i>")

    return "\n\n".join(blocks), counts


def search_results(hits, query: str) -> str:
    """`/qidir`: raw semantic hits — no LLM, just what memory holds."""
    if not hits:
        return f"🔍 <b>{escape(query)}</b> bo'yicha xotirada hech narsa topilmadi."

    lines = [
        f"{short_date(h.memory.occurred_at.date())} · {escape(h.memory.content)}"
        for h in hits
    ]
    return clip(f"🔍 <b>{escape(query)}</b>\n" + bullet_list(lines, empty="—"))


SEARCH_UNAVAILABLE = (
    "⚠️ Qidiruv hozircha ishlamayapti (embedding xizmati tayyor emas) — "
    "birozdan keyin qayta urinib ko'ring."
)


OPERATION_LABEL = {
    "extract": "xabarlardan ajratish",
    "extract_window": "telegram suhbatlari (batch)",
    "extract_window_fallback": "telegram suhbatlari (qayta)",
    "transcribe": "ovozni matnga o'girish",
    "vision": "rasmlarni o'qish",
    "report": "kunlik hisobot",
    "planner": "reja tuzish",
    "rag": "savollarga javob",
}


def usage_report(summary) -> str:
    """`/xarajat`: what MIYA itself cost, from usage_log (spec §9)."""
    if not summary.rows:
        return "💳 Bu davrda API xarajati yozilmagan."

    lines = []
    for row in summary.rows[:12]:
        label = OPERATION_LABEL.get(row.operation, row.operation or row.provider)
        detail = f"{row.calls} marta"
        if row.audio_seconds:
            detail += f" · {int(row.audio_seconds) // 60} daqiqa"
        lines.append(f"{escape(label)}: {usd(row.cost_usd)} ({detail})")

    parts = [
        f"💳 <b>MIYA xarajati</b> · {full_date(summary.date_from)} — "
        f"{full_date(summary.date_to)}",
        bullet_list(lines, empty="—"),
        f"<b>Jami: {usd(summary.total_usd)}</b>\nBugun: {usd(summary.today_usd)}",
    ]
    if summary.cached_share:
        parts.append(f"<i>Keshdan o'qilgan: {summary.cached_share * 100:.0f}%</i>")
    return clip("\n\n".join(parts))


PURGE_USAGE = (
    "🗑 <b>/unut</b> — ma'lumotni butunlay o'chiradi.\n\n"
    "<code>/unut Akmal</code> — odam va u bilan bog'liq hamma narsa\n"
    "<code>/unut chat GZ logistika</code> — bitta chat tarixi\n"
    "<code>/unut 2026-08-01..2026-08-15</code> — sana oralig'i\n\n"
    "<i>O'chirishdan oldin nima yo'qolishini ko'rsataman.</i>"
)

PURGE_NOTHING = "✅ Bu bo'yicha o'chiradigan narsa topilmadi."
PURGE_CANCELLED = "Bekor qilindi — hech narsa o'chirilmadi."
PURGE_EXPIRED = "So'rov eskirdi. <code>/unut</code> ni qaytadan yozing."

_PURGE_KIND = {
    "person": "Odam",
    "chat": "Chat",
    "range": "Sana oralig'i",
}

_COUNT_LABEL = {
    "interactions": "yozuv",
    "debts": "qarz",
    "promises": "va'da",
    "transactions": "pul harakati",
    "events": "uchrashuv",
    "tasks": "vazifa",
    "memories": "xotira",
}


def purge_preview(plan) -> str:
    """What `/unut` is about to delete — shown before anything is touched."""
    lines = [
        f"{_COUNT_LABEL[key]}: {count} ta"
        for key, count in plan.counts.items()
        if count and key in _COUNT_LABEL
    ]
    if plan.files:
        lines.append(f"media fayl: {len(plan.files)} ta")

    return clip(
        f"⚠️ <b>O'chirishni tasdiqlang</b>\n"
        f"{_PURGE_KIND.get(plan.kind, plan.kind)}: <b>{escape(plan.label)}</b>\n\n"
        + bullet_list(lines, empty="—")
        + "\n\n<i>Bu amalni ortga qaytarib bo'lmaydi.</i>"
    )


def purge_done(plan, result) -> str:
    tail = f", {result.files_deleted} ta fayl" if result.files_deleted else ""
    return (
        f"🗑 <b>{escape(plan.label)}</b> o'chirildi: "
        f"{result.interactions} ta yozuv{tail}."
    )


SOURCE_LABEL = {
    "assistant_bot": "xabar",
    "telegram_userbot": "telegram",
    "phone_call": "qo'ng'iroq",
    "manual": "qo'lda",
    "receipt_photo": "rasm",
    "calendar": "kalendar",
}


def review_report(interactions, total: int) -> str:
    """`/tekshir`: what failed processing and still needs the owner's eye."""
    if not interactions:
        return "✅ Qayta ishlanmagan yozuv yo'q."

    lines = []
    for it in interactions:
        label = SOURCE_LABEL.get(it.source.value, it.source.value)
        preview = (it.raw_text or it.transcript or "").strip().replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        detail = f" — {escape(preview)}" if preview else ""
        filename = (it.meta or {}).get("filename") or (it.media or {}).get("filename")
        if not preview and filename:
            detail = f" — {escape(filename)}"
        lines.append(
            f"{short_date(it.occurred_at.date())} {clock(it.occurred_at)} · "
            f"{label}{detail}"
        )

    header = f"⚠️ <b>{total} ta yozuv qayta ishlanmagan</b>"
    if total > len(interactions):
        header += f" (oxirgi {len(interactions)} tasi)"
    return clip(header + "\n" + bullet_list(lines, empty="—"))
