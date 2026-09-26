"""Owner-facing message bodies, in Uzbek."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from miya.bot.formatting import (
    PRIORITY_LABEL,
    TELEGRAM_LIMIT,
    age_label,
    bullet_list,
    claim_line,
    claim_ref,
    clip,
    clock,
    day_label,
    debt_line,
    escape,
    full_date,
    missed_line,
    money,
    question_line,
    queue_line,
    quiet_line,
    quote,
    record_line,
    relative_day,
    short_date,
    split_message,
    stale_line,
    tag,
    tags,
    timeline_line,
    usd,
)
from miya.bot.formatting import ref as _ref_handle
from miya.config import settings
from miya.db.enums import ChatType, Currency, DebtDirection, PromiseMadeBy
from miya.services import claims, health, reports
from miya.services.brief import MorningBrief
from miya.services.loops import MissedCall, UnansweredQuestion
from miya.services.people import Match
from miya.services.persistence import Applied
from miya.services.queries import (
    DaySummary,
    DebtBalance,
    PersonSummary,
    TimelineEntry,
)

# age_label, record_line and the three open-loop lines (question_line,
# stale_line, quiet_line) live in formatting.py: the report's data block
# renders the same rows in plain text and reports.py must not import this
# module. They stay importable from here for existing callers.

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

Har bir qarz, va'da va vazifaning qisqa raqami bor: <code>d12</code>, <code>p7</code>,
<code>t3</code>. Shu raqam bilan yopasiz yoki tuzatasiz.

<b>Buyruqlar</b>
/qarz — ochiq qarzlar
/vada — ochiq va'dalar
/bugun — bugungi holat
/kim &lt;ism yoki GS kod&gt; — odam haqida hamma narsa: profil, qarz, va'da, tarix
/kod &lt;ism&gt; &lt;GS kod&gt; — mijoz kodini biriktirish
/kodlar — xabarlardan topilgan kod takliflari
/tarix &lt;ism&gt; [N] — odam bilan to'liq aloqa tarixi (oxirgi N ta)
/eslab &lt;ism&gt;: &lt;matn&gt; — odam haqida biror narsani eslab qolish
/yuk &lt;YW26-004715 yoki GS367&gt; — yuk xati yoki kod qayerda tilga olingan
/qidir &lt;so'z&gt; — xotiradan qidirish
/hisobot — kunlik hisobot
/ertalab — ertalabki xulosa: bugungi ishlar va ochiq qolganlar
/reja — ertangi reja
/chats — qaysi Telegram chatlar o'qilishi
/process — javob yozilgan media'ni qayta ishlash
/xarajat — MIYA'ning API xarajati
/holat — MIYA'ning ahvoli
/bajarildi &lt;id&gt; — va'da/vazifa bajarildi, qarz to'liq yopildi
/yop &lt;id&gt; — va'da/vazifani bajarilmagan holda yopish
/qaytar &lt;id&gt; — yopilgan yozuvni qayta ochish (noto'g'ri bosilgan bo'lsa)
/tuzat &lt;id&gt; &lt;nima&gt; — yozuvni tuzatish (summa, valyuta, teskari, ism, muddat)
/tuzat x12 … — summa, valyuta, kirim/chiqim, odam, izoh
/pul — bugungi to'lovlar (x12 raqamlari bilan)
/ochir x12 — noto'g'ri pul yozuvini o'chirish (↩️ bilan qaytadi)
/davolar — tasdiqlanmagan da'volar
/savollar — javob kutayotgan savollar (da'volar, guruhlar, fayllar)
/unut — ma'lumotni butunlay o'chirish
/menga — guruhlarda menga yozilganlar
/guruhlar — guruhlarda nima gaplashildi
/tekshir — qayta ishlanmagan yozuvlar
/qayta — ularni qaytadan ajratishga urinish
/yordam — shu ro'yxat
"""

# The bot's command menu (Telegram's "/" list), one short Uzbek phrase per
# command the owner types. Every package that adds a command appends one
# entry; tests/test_command_menu.py fails when a command has none.
COMMAND_MENU: tuple[tuple[str, str], ...] = (
    ("yordam", "Buyruqlar ro'yxati"),
    ("qarz", "Ochiq qarzlar"),
    ("vada", "Ochiq va'dalar"),
    ("bugun", "Bugungi holat"),
    ("menga", "Guruhlarda menga yozilganlar"),
    ("guruhlar", "Guruhlarda nima gaplashildi"),
    ("tekshir", "Qayta ishlanmagan yozuvlar"),
    ("qayta", "Ularni qaytadan ajratish"),
    ("qidir", "Xotiradan qidirish"),
    ("hisobot", "Kunlik hisobot"),
    ("ertalab", "Ertalabki xulosa"),
    ("reja", "Ertangi reja"),
    ("chats", "Qaysi chatlar o'qilishi"),
    ("xarajat", "MIYA'ning API xarajati"),
    ("holat", "MIYA'ning ahvoli"),
    ("unut", "Ma'lumotni butunlay o'chirish"),
    ("bajarildi", "Va'da yoki vazifa bajarildi"),
    ("yop", "Bajarilmagan holda yopish"),
    ("qaytar", "Yopilgan yozuvni qayta ochish"),
    ("tuzat", "Yozuvni tuzatish"),
    ("davolar", "Tasdiqlanmagan da'volar"),
    ("savollar", "Javob kutayotgan savollar"),
    ("kim", "Odam haqida hamma narsa"),
    ("tarix", "Odam bilan aloqa tarixi"),
    ("eslab", "Odam haqida eslab qolish"),
    ("kod", "Mijoz kodini biriktirish"),
    ("kodlar", "Kod takliflari"),
    ("yuk", "Yuk xati yoki kod qayerda tilga olingan"),
    ("pul", "bugungi to'lovlar ro'yxati"),
    ("ochir", "noto'g'ri pul yozuvini o'chirish"),
)

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

UNKNOWN_COMMAND_HINT = (
    "🤷 Bunday buyruq yo'q. Mavjudlarini ko'rish uchun /yordam deb yozing."
)

UNSUPPORTED_HINT = (
    "🤷 Bu turdagi xabarni hali tushunmayman — u yozib olinmadi. "
    "Matn, ovoz, rasm, hujjat yoki video yuboring."
)

DOCUMENT_FAILED_HINT = (
    "⚠️ Hujjatni o'qib bo'lmadi (formati qo'llab-quvvatlanmaydi yoki himoyalangan). "
    "Fayl saqlandi — /tekshir ro'yxatida turadi."
)


RETRY_NOTHING_TO_DO = "✅ Qayta ishlanadigan yozuv yo'q."


def retry_report(rescued: int, still_failing: int) -> str:
    """`/qayta`: what a second extraction pass managed to rescue."""
    if not rescued and not still_failing:
        return RETRY_NOTHING_TO_DO
    lines = [f"🔁 <b>{rescued + still_failing} ta yozuv qayta ishlandi</b>"]
    if rescued:
        lines.append(f"✅ {rescued} tasi ajratildi")
    if still_failing:
        lines.append(f"⚠️ {still_failing} tasi yana bo'lmadi — /tekshir ro'yxatida qoladi")
    return "\n".join(lines)


MEDIA_ASK_LABELS = {
    "video": "video",
    "too_large": "katta fayl",
}

MEDIA_APPROVED = "✅ Yuklab olaman — tayyor bo'lganda yozaman."
MEDIA_DECLINED = "👌 Tegmadim."
MEDIA_GONE = "⚠️ Bu so'rov eskirgan yoki yozuv o'chirilgan."


def _size(nbytes: int | None) -> str:
    if not nbytes:
        return ""
    mb = nbytes / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 1 else f"{nbytes / 1024:.0f} KB"


def media_question(*, who: str | None, media: dict, reason: str) -> str:
    """Ask the owner whether a large attachment is worth fetching.

    Everything the answer depends on is in the question — who sent it, what it
    is, how big, and its caption — because the file itself is what MIYA is
    asking permission to look at. It cannot describe what it has not fetched.
    """
    kind = MEDIA_ASK_LABELS.get(reason, str(media.get("type") or "fayl"))
    parts = [f"📎 <b>{escape(who or 'Nomaʼlum')}</b> {escape(kind)} yubordi"]

    detail = " · ".join(
        p for p in (escape(media.get("filename") or ""), _size(media.get("size"))) if p
    )
    if detail:
        parts.append(detail)
    caption = (media.get("caption") or "").strip()
    if caption:
        parts.append(f"<i>{escape(caption[:200])}</i>")
    parts.append("O'qiyminmi?")
    return "\n".join(parts)


TO_ME_EMPTY = "📭 Bugun guruhlarda sizga to'g'ridan-to'g'ri yozilmadi."
CHATS_QUIET = "🤫 Bugun kuzatilayotgan chatlarda harakat bo'lmadi."


def _line_of(interaction, limit: int = 160) -> str:
    body = (interaction.raw_text or interaction.transcript or "").strip()
    if not body:
        body = f"[{(interaction.media or {}).get('type') or 'media'}]"
    return escape(body[:limit])


def to_me_report(
    interactions: list, titles: dict[int, str], maybe: list | None = None
) -> str:
    """`/menga`: what was aimed at the owner in a group today; lines that
    name only a first name a namesake in the group shares come apart."""
    maybe = maybe or []
    if not interactions and not maybe:
        return TO_ME_EMPTY

    def line(interaction) -> str:
        where = escape(titles.get(interaction.tg_chat_id or 0, "guruh"))
        when = interaction.occurred_at.astimezone(settings.tz).strftime("%H:%M")
        return f"• {when} · <b>{where}</b> — {_line_of(interaction)}"

    lines = [TO_ME_EMPTY]
    if interactions:
        lines = [f"📨 <b>Sizga {len(interactions)} ta murojaat</b>"]
        lines += [line(i) for i in interactions]
    if maybe:
        stem = (maybe[0].meta or {}).get("namesake") or ""
        lines.append(
            f"\n❔ <b>Balki sizga</b> <i>(guruhda boshqa «{escape(stem.capitalize())}» "
            f"ham bor)</i>"
        )
        lines += [line(i) for i in maybe]
    return "\n".join(lines)


def chat_digest_report(digests: list) -> str:
    """`/guruhlar`: one line of subject matter per chat, busiest first."""
    if not digests:
        return CHATS_QUIET
    lines = ["💬 <b>Bugun chatlarda</b>"]
    for digest in digests:
        head = f"\n<b>{escape(digest.title)}</b> · {digest.messages} ta xabar"
        if digest.to_me:
            head += f" · 📨 {len(digest.to_me)} ta sizga"
        lines.append(head)
        for summary in digest.summaries[:3]:
            lines.append(f"• {escape(summary)}")
        if not digest.summaries:
            # A window only closes after the chat goes quiet, so an active
            # conversation legitimately has nothing summarised yet.
            lines.append("<i>• hali umumlashtirilmadi</i>")
    return "\n".join(lines)


def person_not_found(name: str) -> str:
    return f"❓ <b>{escape(name)}</b> topilmadi."


KIM_USAGE = "Ism yozing: <code>/kim Akmal</code>"
TARIX_USAGE = "Ism yozing: <code>/tarix Akmal</code> yoki <code>/tarix Akmal 50</code>"
ESLAB_USAGE = (
    "Kim haqida nimani eslab qolay?\n" "<code>/eslab Akmal: mashinasi oq Malibu</code>"
)

# How each command's hint reads when the owner has to pick a candidate.
_AMBIGUOUS_HINT = {
    "kim": "/kim {name}",
    "tarix": "/tarix {name}",
    "eslab": "/eslab {name}: …",
    "unut": "/unut {name}",
    "kod": "/kod {name} …",
}


def person_ambiguous(
    match: Match, *, command: str = "kim", codes: dict[int, list[str]] | None = None
) -> str:
    """Two people score alike — name both and ask, never guess.

    Kimni nazarda tutding: Akmal (GS367) yoki Akmal (GS412)? (/kim GS367)
    A person's client codes tell two namesakes apart; the hint uses the
    first person's code when they hold exactly one.
    """
    codes = codes or {}

    def label(person) -> tuple[str, list[str]]:
        if person is None:
            return "", []
        held = codes.get(person.id, [])
        text = f"<b>{escape(person.display_name)}</b>"
        if held:
            text += f" ({escape(', '.join(held))})"
        return text, held

    first, first_codes = label(match.person)
    second, _ = label(match.runner_up)
    name = match.person.display_name if match.person else ""
    if len(first_codes) == 1:
        name = first_codes[0]
    hint = _AMBIGUOUS_HINT.get(command, _AMBIGUOUS_HINT["kim"]).format(name=name)
    return (
        f"❓ Kimni nazarda tutding: {first} yoki {second}? "
        f"(<code>{escape(hint)}</code>)"
    )


def code_unknown(code: str) -> str:
    """A client code nobody holds (WP-31)."""
    return (
        f"❓ <b>{escape(code)}</b> hech kimga biriktirilmagan. "
        f"Biriktirish: <code>/kod Ism {escape(code)}</code>"
    )


def identity_conflict(code: str, holder: str, named: str) -> str:
    """A code held by someone other than the person named with it."""
    return (
        f"⚠️ <b>{escape(code)}</b> bazada <b>{escape(holder)}</b> nomida, "
        f"xabarda esa «{escape(named)}». Kod noto'g'ri bo'lsa: "
        f"<code>/kod {escape(named)} {escape(code)}</code>"
    )


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
        lines.append(
            f"💰 Qarz: {arrow} {who} {money(debt.amount, debt.currency)}{tail}"
            + tag("debt", debt.id)
        )

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

    for code, name in applied.codes_learned:
        lines.append(f"🏷 {escape(code)} → {escape(name)} biriktirildi.")

    for name in applied.owner_named:
        lines.append(
            f"⚠️ «{escape(name)}» — bu sizning ismingiz. Kim nazarda tutilganini "
            f"aniqlay olmadim — yozmadim, /tekshir ro'yxatida."
        )

    for code in applied.unknown_codes:
        lines.append(
            f"⚠️ <b>{escape(code)}</b> kodi hech kimga biriktirilmagan — yozmadim "
            f"(/tekshir ro'yxatida). Avval <code>/kod Ism {escape(code)}</code>, "
            f"keyin xabarni qayta yuboring."
        )

    for code, holder, named in applied.identity_conflicts:
        lines.append(
            f"⚠️ <b>{escape(code)}</b> bazada <b>{escape(holder)}</b> nomida, xabarda "
            f"esa «{escape(named)}». Yozmadim — /tekshir ro'yxatida. Kod noto'g'ri "
            f"bo'lsa: <code>/kod {escape(named)} {escape(code)}</code>"
        )

    for promise in applied.promises:
        who = "Men" if promise.made_by is PromiseMadeBy.me else "U"
        tail = f" ({short_date(promise.due_date)})" if promise.due_date else ""
        lines.append(
            f"🤝 Va'da: {who} — {escape(promise.description)}{tail}"
            + tag("promise", promise.id)
        )

    for promise, person in applied.fulfilled:
        lines.append(
            f"✅ Va'da bajarildi: {escape(person.display_name)} — "
            f"{escape(promise.description)}" + tag("promise", promise.id)
        )

    for name, description in applied.unmatched_fulfilments:
        # Said to have happened, but no open promise matched clearly enough
        # to close on its own — the owner closes the right one by its ref.
        lines.append(
            f"❓ {escape(name)}: «{escape(description)}» — bajarilgan ko'rinadi, "
            f"lekin qaysi va'da ekani aniq emas (/vada, keyin /bajarildi p…)"
        )

    for txn in applied.transactions:
        icon = "📈" if txn.type.value == "income" else "📉"
        label = "Kirim" if txn.type.value == "income" else "Chiqim"
        detail = f" ({escape(txn.description)})" if txn.description else ""
        lines.append(
            f"{icon} {label}: {money(txn.amount, txn.currency)} · "
            f"{escape(txn.category or 'boshqa')}{detail}" + tag("transaction", txn.id)
        )

    for bank, _, _ in applied.matched_transactions:
        icon = "📈" if bank.type.value == "income" else "📉"
        label = "Kirim" if bank.type.value == "income" else "Chiqim"
        lines.append(
            f"{icon} {label}: {money(bank.amount, bank.currency)} — SMS'dagi"
            f"{tag('transaction', bank.id)} bilan bir xil to'lov, ikkinchi marta "
            f"yozilmadi."
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
        lines.append(
            f"✔️ Vazifa: {escape(task.description)}{tail}{priority}" + tag("task", task.id)
        )

    if applied.facts:
        lines.append(f"🧠 {applied.facts} ta yangi ma'lumot eslab qolindi")

    # What a counterparty asserted is not on the ledger yet: each line is a
    # question, and the receipt's Ha / Yo'q / Tuzat rows answer it.
    lines += [claim_line(claims.view(c)) for c in applied.claims]

    return clip("\n".join(lines))


def confirmation_refs(applied: Applied) -> list[tuple[str, int]]:
    """The rows a confirmation's buttons act on, in the order the lines show."""
    refs: list[tuple[str, int]] = []
    refs += [("debt", d.id) for d in applied.debts if d.id is not None]
    refs += [("promise", p.id) for p in applied.promises if p.id is not None]
    refs += [("transaction", t.id) for t in applied.transactions if t.id is not None]
    refs += [("task", t.id) for t in applied.tasks if t.id is not None]
    return refs


def confirmation_claim_ids(applied: Applied) -> list[int]:
    """The claims a confirmation asks about, in the order the lines show."""
    return [c.id for c in applied.claims if c.id is not None and c.state == "pending"]


def debts_report(balances: list[DebtBalance]) -> str:
    if not balances:
        return "✅ Ochiq qarz yo'q."

    they_owe = [b for b in balances if b.direction is DebtDirection.they_owe_me]
    i_owe = [b for b in balances if b.direction is DebtDirection.i_owe_them]
    blocks: list[str] = []

    if they_owe:
        lines = [_balance_line(b) for b in they_owe]
        blocks.append("<b>Senga qarzdorlar</b>\n" + bullet_list(lines, empty="—"))

    if i_owe:
        lines = [_balance_line(b) for b in i_owe]
        blocks.append("<b>Sen qarzdorsan</b>\n" + bullet_list(lines, empty="—"))

    return clip("\n\n".join(blocks))


def _balance_line(b: DebtBalance) -> str:
    return (
        f"{escape(b.person.display_name)}: {money(b.outstanding, b.currency)}"
        + (f" · {relative_day(b.earliest_due)}" if b.earliest_due else "")
        + tags("debt", b.ids)
    )


def promises_report(items) -> str:
    if not items:
        return "✅ Ochiq va'da yo'q."

    mine = [
        f"{escape(p.description)} — {escape(person.display_name)}"
        + (f" · {relative_day(p.due_date)}" if p.due_date else "")
        + tag("promise", p.id)
        for p, person in items
        if p.made_by is PromiseMadeBy.me
    ]
    theirs = [
        f"{escape(person.display_name)}: {escape(p.description)}"
        + (f" · {relative_day(p.due_date)}" if p.due_date else "")
        + tag("promise", p.id)
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
        # Listed by ref, not counted: the day's fresh rows are the ones most
        # likely to need /tuzat, and the owner needs a handle to name them.
        new_lines = [
            debt_line(
                escape(debt.person.display_name),
                debt.direction,
                debt.amount,
                debt.currency,
                debt.due_date,
            )
            + tag("debt", debt.id)
            for debt in summary.new_debts
        ]
        new_lines += [
            ("Men: " if p.made_by is PromiseMadeBy.me else "U: ")
            + f"{escape(p.person.display_name)} — {escape(p.description)}"
            + (f" · {relative_day(p.due_date)}" if p.due_date else "")
            + tag("promise", p.id)
            for p in summary.new_promises
        ]
        parts.append(
            "🧾 <b>Yangi qarz va va'dalar</b>\n" + bullet_list(new_lines, empty="—")
        )

    parts.append(f"\n<i>{summary.interactions} ta yozuv</i>\n{DAY_REPORT_MONEY_HINT}")
    return clip("\n\n".join(parts))


DAY_REPORT_MONEY_HINT = "Har bir to'lov: /pul"


PROFILE_MISSING = "📝 Profil hali yozilmagan."
KIM_FACTS = 5
KIM_TIMELINE = 10


def _identity_line(person, codes=()) -> str:
    """Name in bold, then everything that pins down who this is."""
    bits = [f"<b>{escape(person.display_name)}</b>"]
    if codes:
        bits.append(f"🏷 {escape(', '.join(codes))}")
    if person.aliases:
        bits.append(f"<i>{escape(', '.join(person.aliases))}</i>")
    if person.telegram_username:
        bits.append(f"@{escape(person.telegram_username)}")
    if person.phone:
        bits.append(escape(person.phone))
    if person.relationship_:
        bits.append(escape(person.relationship_))
    return " · ".join(bits)


def _profile_block(summary: PersonSummary) -> str:
    """MIYA's own paragraph about the person, dated; never a source of figures."""
    if not summary.profile:
        return PROFILE_MISSING
    stamp = ""
    if summary.profile_updated_at is not None:
        stamp = f" <i>({day_label(summary.profile_updated_at)})</i>"
    return f"📝 <b>Profil</b>{stamp}\n{escape(summary.profile)}"


def _fact_lines(facts, *, limit: int = KIM_FACTS) -> list[str]:
    return [
        f"{day_label(fact.occurred_at)} · {escape(fact.content)}"
        for fact in facts[:limit]
    ]


def person_report(summary: PersonSummary) -> str:
    """`/kim`: everything held about one person, on one screen.

    Identity, the profile, the SQL figures (balances, promises), the facts
    remembered, the last contacts, and where the full history lives.
    """
    person = summary.person
    codes = getattr(summary, "codes", None) or []
    parts = [_identity_line(person, codes), _profile_block(summary)]

    if summary.balances:
        lines = [
            debt_line(
                escape(b.person.display_name),
                b.direction,
                b.outstanding,
                b.currency,
                b.earliest_due,
            )
            + tags("debt", b.ids)
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
            + tag("promise", p.id)
            for p in summary.open_promises
        ]
        parts.append("🤝 <b>Va'dalar</b>\n" + bullet_list(lines, empty="—"))

    if summary.facts:
        parts.append(
            "🧠 <b>Eslab qolganlarim</b>\n"
            + bullet_list(_fact_lines(summary.facts), empty="—")
        )

    if summary.timeline:
        lines = [timeline_line(e) for e in summary.timeline[:KIM_TIMELINE]]
        parts.append("🕐 <b>Oxirgi aloqalar</b>\n" + bullet_list(lines, empty="—"))

    mentioned = getattr(summary, "code_mentions", None) or []
    if mentioned:
        lines = [mention_line(line) for line in mentioned]
        parts.append(f"{CODE_MENTIONS_HEADER}\n" + bullet_list(lines, empty="—"))

    tail = []
    last = summary.last_contact_at
    if last is None and summary.last_interactions:
        last = summary.last_interactions[0].occurred_at
    if last is not None:
        tail.append(f"Oxirgi aloqa: {day_label(last)} {clock(last)}")
    tail.append(f"jami {summary.total_interactions} ta aloqa")
    parts.append("🕐 " + " · ".join(tail))
    handle = codes[0] if len(codes) == 1 else person.display_name
    parts.append(f"<i>/tarix {escape(handle)} — to'liq tarix</i>")

    return clip("\n\n".join(parts))


def _fit_oldest_first(lines_newest_first: list[str], *, budget: int) -> list[str]:
    """Keep the newest lines that fit, returned oldest→newest.

    ``clip`` cuts the tail of a message; a history reads oldest→newest, so
    a tail cut would drop the most recent contact — the one the owner asked
    for. Cut the old end instead.
    """
    kept: list[str] = []
    used = 0
    for line in lines_newest_first:
        used += len(line) + 3  # bullet and newline
        if used > budget:
            break
        kept.append(line)
    kept.reverse()
    return kept


def history_report(
    person, entries: list[TimelineEntry], *, requested: int, codes=()
) -> str:
    """`/tarix`: a person's contacts, oldest at the top, newest at the bottom."""
    name = escape(person.display_name)
    if not entries:
        return f"🕐 <b>{name}</b> bilan hali aloqa yozilmagan."
    shown_count = min(requested, len(entries))
    label = f"<b>{name}</b>" + (f" ({escape(', '.join(codes))})" if codes else "")
    header = f"🕐 {label} — tarix (oxirgi {shown_count} ta)"
    # Offer more only when there may be more: a short history is complete,
    # and the cap is the cap.
    hint = ""
    handle = escape(codes[0]) if len(codes) == 1 else name
    if len(entries) >= requested and requested < TARIX_MAX:
        hint = f"\n\n<i>Ko'proq: /tarix {handle} {min(requested * 2, TARIX_MAX)}</i>"
    lines = [timeline_line(e) for e in entries]  # newest first, as queried
    budget = TELEGRAM_LIMIT - len(header) - len(hint) - 40
    shown = _fit_oldest_first(lines, budget=budget)
    body = bullet_list(shown, empty="—")
    if len(shown) < len(lines):
        body = f"<i>…(eskilari sig'madi)</i>\n{body}"
    return clip(f"{header}\n\n{body}{hint}")


TARIX_DEFAULT = 30
TARIX_MAX = 100


def remembered(person, text: str) -> str:
    """`/eslab`: the owner's own words, stored against the person."""
    return f"🧠 Eslab qoldim: <b>{escape(person.display_name)}</b> — {quote(text, 300)}"


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
                f"{relative_day(b.earliest_due)}" + tags("debt", b.ids)
                for b in debts
            ],
        ),
        (
            "promise",
            "🤝 <b>Va'da muddati</b>",
            [
                f"{escape(person.display_name)}: {escape(p.description)} · "
                f"{relative_day(p.due_date)}" + tag("promise", p.id)
                for p, person in promises
            ],
        ),
        (
            "task",
            "✔️ <b>Vazifalar</b>",
            [
                f"{escape(t.description)} · {relative_day(t.due_date)}"
                + tag("task", t.id)
                for t in tasks
            ],
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


def reminder_refs(
    debts, promises, tasks, rendered: dict[str, int]
) -> list[tuple[str, int]]:
    """The rows behind the lines that made it into the reminder body."""
    refs: list[tuple[str, int]] = []
    for b in debts[: rendered.get("debt", 0)]:
        refs += [("debt", i) for i in b.ids]
    refs += [("promise", p.id) for p, _ in promises[: rendered.get("promise", 0)]]
    refs += [("task", t.id) for t in tasks[: rendered.get("task", 0)]]
    return refs


STILL_OPEN_HEADER = "📌 <b>Hali ochiqmi?</b>"
STILL_OPEN_HINT = "<i>Ha — yana bir haftadan keyin so'rayman.</i>"


def still_open_question(questions) -> str:
    """The escalation's last word and the weekly nudge for undated items.

    One message for all of them, a row of Ha / Bajarildi / Yop per line, so
    ten forgotten tasks are one message, not ten. A debt question is about a
    balance (several rows), the others about one row.
    """
    body, _ = still_open_question_with_count(questions)
    return body


def still_open_question_with_count(questions) -> tuple[str, int]:
    """The question body, plus how many of its lines actually fit.

    The same rule as `reminder_with_counts`: only what is shown may be
    logged as asked. A question clipped off the end and still logged would
    go quiet for a week without the owner ever having seen it; unlogged, it
    simply qualifies again next sweep.
    """
    lines = [
        _balance_line(q.balance)
        if q.balance is not None
        else record_line(q.kind, q.record, q.person)
        for q in questions
    ]
    kept: list[str] = []
    for line in lines:
        trial = (
            f"{STILL_OPEN_HEADER}\n"
            + bullet_list([*kept, line], empty="—")
            + f"\n{STILL_OPEN_HINT}"
        )
        if len(trial) > TELEGRAM_LIMIT - 60:
            break
        kept.append(line)
    body = f"{STILL_OPEN_HEADER}\n" + bullet_list(kept, empty="—")
    dropped = len(lines) - len(kept)
    if dropped:
        body += f"\n<i>… va yana {dropped} ta — keyingi eslatmada.</i>"
    return body + f"\n{STILL_OPEN_HINT}", len(kept)


# --- /bajarildi, /yop, /tuzat ------------------------------------------------

REF_USAGE = (
    "Yozuv raqamini yozing — ro'yxatlarda ko'rinadi: <code>d12</code> (qarz), "
    "<code>p7</code> (va'da), <code>t3</code> (vazifa).\n"
    "<code>/bajarildi p7</code> · <code>/yop t3</code> · <code>/tuzat d12 6 mln</code>"
)

RECORD_NOT_FOUND = "❓ Bunday yozuv topilmadi: <code>{ref}</code>"
RECORD_ALREADY_CLOSED = "Bu yozuv allaqachon yopilgan."
RECORD_ALREADY_OPEN = "Bu yozuv ochiq — qaytaradigan narsa yo'q."
DEBT_NOT_REOPENABLE = (
    "Bu qarz yozib olingan to'lovlar bilan yopilgan — ularni o'chirmayman. "
    "Noto'g'ri bo'lsa: <code>/tuzat {ref} …</code>."
)
DEBT_NOT_CLOSABLE = (
    "Qarzni /yop bilan yopib bo'lmaydi. To'liq to'langan bo'lsa — "
    "<code>/bajarildi {ref}</code>; noto'g'ri yozilgan bo'lsa — "
    "<code>/tuzat {ref} …</code>."
)

TUZAT_USAGE = (
    "✏️ <b>/tuzat</b> — bitta yozuvni tuzatish.\n\n"
    "<code>/tuzat d12 6 mln</code> — summa\n"
    "<code>/tuzat d12 300 $</code> — summa va valyuta\n"
    "<code>/tuzat d12 teskari</code> — kim kimga qarz (aksincha)\n"
    "<code>/tuzat d12 Sardor</code> — boshqa odam\n"
    "<code>/tuzat p7 ertaga</code> — muddat (sana, ertaga, juma, 3 kun, muddatsiz)\n"
    "<code>/tuzat c12 summa 4 mln</code> — tasdiqlanmagan da'voni, javobdan oldin\n\n"
    "<i>Aniq bo'lmasa: «kim Sardor», «summa 5 mln», «muddat juma».</i>"
)


# What the ✏️ button offers, per kind: only what `set_field` accepts for
# that row (records.EDITABLE), so the hint never teaches a refused edit.
_TUZAT_EXAMPLES = {
    "debt": ("6 mln", "300 $", "teskari", "Sardor", "ertaga"),
    "promise": ("Sardor", "ertaga"),
    "task": ("ertaga",),
    "transaction": (
        "250 ming",
        "valyuta $",
        "teskari",
        "kim Akmal",
        "izoh yuk uchun",
        "sana kecha",
    ),
}

TXN_TUZAT_HINT = (
    "✏️ <code>{ref}</code> ni tuzatish uchun yozing:\n"
    "<code>/tuzat {ref} 250 ming</code> — summa · "
    "<code>/tuzat {ref} valyuta $</code> — valyuta · "
    "<code>/tuzat {ref} teskari</code> — kirim ↔ chiqim · "
    "<code>/tuzat {ref} kim Akmal</code> — kim bilan · "
    "<code>/tuzat {ref} izoh yuk uchun</code> — izoh · "
    "<code>/tuzat {ref} sana kecha</code> — sana · "
    "<code>/ochir {ref}</code> — noto'g'ri yozilgan bo'lsa"
)
TXN_NOT_DOABLE = (
    "<code>{ref}</code> — pul harakati, uni «bajarildi» qilib bo'lmaydi. "
    "Noto'g'ri bo'lsa: <code>/ochir {ref}</code>, xato joyi bo'lsa: "
    "<code>/tuzat {ref} …</code>"
)
TXN_VOIDED_LOCKED = (
    "<code>{ref}</code> o'chirilgan. Tuzatish uchun avval <code>/qaytar {ref}</code>."
)
OCHIR_USAGE = (
    "Qaysi yozuvni? Masalan: <code>/ochir x12</code> — raqamlar /pul ro'yxatida."
)
OCHIR_ONLY_MONEY = (
    "<code>/ochir</code> faqat pul harakatlari uchun (<code>x12</code>). "
    "Va'da yoki vazifani yopish: <code>/yop p7</code>."
)


def tuzat_hint(kind: str, handle: str) -> str:
    """What the ✏️ button says: the syntax, with this row's ref filled in."""
    if kind == "transaction":
        return TXN_TUZAT_HINT.format(ref=handle)
    examples = " · ".join(
        f"<code>/tuzat {handle} {example}</code>" for example in _TUZAT_EXAMPLES[kind]
    )
    return f"✏️ <code>{handle}</code> ni tuzatish uchun yozing:\n{examples}"


_FIELD_LABEL = {
    "amount": "summa",
    "currency": "valyuta",
    "direction": "yo'nalish",
    "person": "odam",
    "due": "muddat",
    "note": "izoh",
    "category": "turkum",
}

FIELD_NOT_EDITABLE = {
    "debt": "Qarzda bunday maydon yo'q.",
    "promise": "Va'dada faqat odam va muddatni tuzatish mumkin.",
    "task": "Vazifada faqat muddatni tuzatish mumkin.",
    "transaction": (
        "Pul harakatida faqat summa, valyuta, tomon (kirim/chiqim), odam, sana, "
        "izoh va turkumni tuzatish mumkin."
    ),
}

DEBT_CURRENCY_LOCKED = (
    "Bu qarzda to'lovlar yozilgan — valyutasini o'zgartirmayman, to'lovlar "
    "eski valyutada qoladi. Avval <code>/qaytar {ref}</code> bilan qayta "
    "oching yoki to'lovlarni tuzating, keyin valyutani."
)

NEW_PERSON_EXPIRED = "Bu savol eskirgan — <code>/tuzat</code> ni qaytadan yozing."
NEW_PERSON_DECLINED = "Yaratilmadi: «{name}». Yozuv o'zgarmadi."


def debt_payments_exceed(handle: str, exc) -> str:
    """`/tuzat d12 3 mln` below what was really repaid."""
    return (
        f"Bu qarzga {money(exc.paid, exc.currency)} to'lov yozilgan — yangi summa "
        f"{money(exc.amount, exc.currency)} undan kam, to'lovlarni kesmayman. "
        f"Avval <code>/qaytar {handle}</code>, keyin to'lovlarni tuzating."
    )


def new_person_question(name: str) -> str:
    return f"❓ Yangi odam «<b>{escape(name)}</b>» yaratilsinmi?"


def record_done(change) -> str:
    verb = "Yopildi" if change.kind == "debt" else "Bajarildi"
    line = record_line(change.kind, change.record, change.person)
    return f"✅ <b>{verb}</b>\n{line}"


def record_closed(change) -> str:
    return (
        "✖️ <b>Yopildi</b> (bajarilgan hisoblanmaydi)\n"
        f"{record_line(change.kind, change.record, change.person)}"
    )


def balance_settled(changes) -> str:
    """Every row of a balance settled from one "Hali ochiqmi?" ✅."""
    lines = [record_line(c.kind, c.record, c.person) for c in changes]
    return "✅ <b>Yopildi</b>\n" + "\n".join(lines)


def record_reopened(change) -> str:
    return (
        "↩️ <b>Qayta ochildi</b>\n"
        f"{record_line(change.kind, change.record, change.person)}"
    )


def record_edited(change) -> str:
    label = _FIELD_LABEL.get(change.field, change.field)
    line = record_line(change.kind, change.record, change.person)
    return f"✏️ <b>Tuzatildi</b> ({label})\n{line}"


def txn_short(txn) -> str:
    income = txn.type.value == "income"
    return f"{'📈' if income else '📉'} {'Kirim' if income else 'Chiqim'} " + money(
        txn.amount, txn.currency
    )


def record_voided(change) -> str:
    txn = change.record
    what = txn.description or txn.category
    tail = f" · {escape(what)}" if what else ""
    return (
        f"🗑 <code>x{txn.id}</code> o'chirildi: {txn_short(txn)}{tail}. "
        "Endi hisobotlarga kirmaydi."
    )


def record_unvoided(change) -> str:
    txn = change.record
    return f"↩️ <code>x{txn.id}</code> qaytarildi — yana hisobda: {txn_short(txn)}."


PUL_USAGE = (
    "Qaysi kun? <code>/pul</code> — bugun · <code>/pul kecha</code> · "
    "<code>/pul 2026-09-20</code>"
)
# --- money receipts (WP-15) ---------------------------------------------------

MONEY_FOLDED_HEADER = "💳 <b>{n} ta yangi to'lov</b>"
MONEY_FOLDED_MAX_LINES = 15
MONEY_FOLDED_TAIL = "… yana {n} ta — /pul"
MONEY_IMPORT_SUMMARY = (
    "📥 Telefondan eski xabarlar yuklandi: {booked} ta to'lov yozildi, "
    "{review} tasi /tekshir'da, {ignored} tasi e'tiborsiz qoldirildi "
    "(kod, reklama). Ro'yxat: /pul"
)


def _receipt_parts(txn, interaction) -> tuple[str, str | None, str | None]:
    """(what, app label, sms?) — the merchant as read, else the description."""
    media = interaction.media or {}
    merchant = (media.get("money") or {}).get("merchant") or txn.description
    if interaction.source.value == "phone_notification":
        return merchant, media.get("app_label") or "ilova", None
    return merchant, None, "sms"


def money_receipt(txn, interaction, person=None) -> str:
    """'📉 Chiqim: <b>250 ming so'm</b> · KORZINKA.UZ · karta *1234 · 14:30 · ✉️ x12'"""
    income = txn.type.value == "income"
    what, app, sms = _receipt_parts(txn, interaction)
    parts = [
        f"{'📈' if income else '📉'} {'Kirim' if income else 'Chiqim'}: "
        f"<b>{money(txn.amount, txn.currency)}</b>"
    ]
    if what:
        parts.append(escape(what))
    if txn.card_last4:
        parts.append(f"karta *{txn.card_last4}")
    if person is not None:
        parts.append(f"👤 {escape(person.display_name)}")
    if app:
        parts.append(f"🔔 {escape(app)}")
    parts.append(clock(txn.occurred_at))
    if sms:
        parts.append("✉️")
    return " · ".join(parts) + f" <code>x{txn.id}</code>"


def money_receipts_folded(items) -> str:
    """Several receipts at once: one line each, at most MONEY_FOLDED_MAX_LINES."""
    lines = []
    for interaction, txn in items[:MONEY_FOLDED_MAX_LINES]:
        what, _, _ = _receipt_parts(txn, interaction)
        icon = "📈" if txn.type.value == "income" else "📉"
        lines.append(
            f"• {icon} x{txn.id} {clock(txn.occurred_at)} · "
            f"{money(txn.amount, txn.currency)}" + (f" · {escape(what)}" if what else "")
        )
    hidden = len(items) - MONEY_FOLDED_MAX_LINES
    if hidden > 0:
        lines.append(MONEY_FOLDED_TAIL.format(n=hidden))
    return clip(
        MONEY_FOLDED_HEADER.format(n=len(items))
        + "\n"
        + "\n".join(lines)
        + "\n"
        + TXN_LIST_FOOTER
    )


def money_import_summary(booked: int, review: int, ignored: int) -> str:
    return MONEY_IMPORT_SUMMARY.format(booked=booked, review=review, ignored=ignored)


TXN_LIST_EMPTY = "Bu kunda pul harakati yo'q."
TXN_LIST_FOOTER = (
    "Tuzatish: <code>/tuzat x12 …</code> · O'chirish: <code>/ochir x12</code>"
)


def _channel_icon(channel: str | None) -> str:
    if channel is None:
        return "✍️"
    return "🔔" if channel.startswith("app:") else "✉️"


def transactions_list(day, rows) -> str:
    """`/pul`: one day's money rows by x-ref, voided ones marked."""
    header = f"💳 <b>{short_date(day)} — pul harakatlari</b>"
    if not rows:
        return f"{header}\n{TXN_LIST_EMPTY}"
    lines = []
    for txn in rows:
        icon = "📈" if txn.type.value == "income" else "📉"
        what = txn.description or txn.category
        void = " · 🗑 o'chirilgan" if txn.voided_at is not None else ""
        lines.append(
            f"{icon} <code>x{txn.id}</code> {clock(txn.occurred_at)} · "
            f"{money(txn.amount, txn.currency)}"
            + (f" · {escape(what)}" if what else "")
            + f" · {_channel_icon(txn.channel)}{void}"
        )
    return clip(f"{header}\n" + "\n".join(lines) + f"\n\n{TXN_LIST_FOOTER}")


def record_still_open(kind: str, records, person) -> str:
    """ "Ha": the row — or every open row of the balance — stays open."""
    lines = "\n".join(record_line(kind, record, person) for record in records)
    return f"👌 Ochiq qoladi — bir haftadan keyin yana so'rayman.\n{lines}"


# --- a counterparty's claim: ask first (build step 3) ---------------------------
#
# "You owe me", "I paid you back", "you promised" — said by the other side.
# The owner decided such a thing is asked and never written silently
# (docs/owner-decisions.md, "Counterparty claims"). The question line itself
# is formatting.claim_line; here are the message around it, the list, and
# what each answer says back.

CLAIM_QUESTION_HEADER = "❓ <b>Tasdiqlash kerak</b>"
CLAIMS_HEADER = "❓ <b>Tasdiqlanmagan da'volar</b>"
CLAIMS_NONE = "✅ Tasdiqlanmagan da'vo yo'q."
CLAIMS_HINT = "<i>✅ Ha — yozaman · ✖️ Yo'q — yozmayman · ✏️ Tuzat — avval tuzatasan</i>"

CLAIM_ACCEPTED_PREFIX = "✅ <b>Yozib oldim:</b>"
# "Ha" on a repayment or a hint that has nothing to land on: the claim stays
# open, the owner confirms the debt or promise first and taps Ha again.
CLAIM_ACCEPTED_UNMATCHED = (
    "⚠️ <b>Hozircha yozilmadi</b> — avval tegishli qarz yoki va'dani tasdiqla, "
    "keyin yana Ha bos:"
)
CLAIM_ACCEPTED_NOTHING = (
    "⚠️ Tasdiqlading, lekin yozib bo'lmadi — da'voda ism yoki summa yetishmaydi. "
    "Kerak bo'lsa o'zing yozib qo'y."
)
CLAIM_DECLINED = "✖️ Yozilmadi. Kerak bo'lsa o'zing yozib qo'y."
CLAIM_GONE = "⚠️ Bu da'vo topilmadi — yozuvi o'chirilgan bo'lsa kerak."
CLAIM_ALREADY = "Bu da'voga allaqachon javob berilgan."
CLAIM_EDITED = "✏️ <b>Tuzatildi</b> — endi javob ber:"

# How many claims the brief lists with buttons; the rest wait in /davolar.


def claim_question(view: claims.ClaimView) -> str:
    """One message for one claim, when no receipt carried the question."""
    return f"{CLAIM_QUESTION_HEADER}\n{claim_line(view)}"


def claims_list(views: list[claims.ClaimView], *, hidden: int = 0) -> str:
    """`/davolar`: every unanswered claim, oldest first, with the buttons' key.

    ``hidden`` is how many more are waiting beyond the ones listed — the
    keyboard has a ceiling, and a claim without its buttons is not asked.
    """
    if not views:
        return CLAIMS_NONE
    body = f"{CLAIMS_HEADER}\n" + "\n".join(claim_line(v) for v in views)
    if hidden:
        body += f"\n<i>… va yana {hidden} ta — javob bergach yana /davolar.</i>"
    return clip(f"{body}\n{CLAIMS_HINT}")


def claim_accepted(accepted: claims.Accepted) -> str:
    """ "Ha": what accepting wrote, in the receipt's own words.

    A settlement that matched no open debt, or a hint that closed no
    promise, lands in the receipt as the same question it would have been
    on the day. An item the writer refused outright says so, not "yozib
    oldim" over nothing.
    """
    if accepted.applied.is_empty():
        return CLAIM_ACCEPTED_NOTHING
    if not accepted.written:
        return f"{CLAIM_ACCEPTED_UNMATCHED}\n{confirmation(accepted.applied)}"
    return f"{CLAIM_ACCEPTED_PREFIX}\n{confirmation(accepted.applied)}"


def claim_edited(view: claims.ClaimView) -> str:
    return f"{CLAIM_EDITED}\n{claim_line(view)}"


# One example per field the kind can take (claims.EDITABLE), with the
# prefix spelled out: a claim is corrected before it is a row, and the
# owner should not have to guess whether "4 mln" is an amount or a name.
_CLAIM_TUZAT_EXAMPLES = {
    "amount": "summa 4 mln",
    "person": "kim Akmal",
    "currency": "valyuta $",
    "due": "muddat 2026-10-01",
    "direction": "teskari",
}


def claim_tuzat_hint(view: claims.ClaimView) -> str:
    """What the ✏️ button says: the syntax, with this claim's ref filled in."""
    handle = claim_ref(view.id)
    examples = " · ".join(
        f"<code>/tuzat {handle} {_CLAIM_TUZAT_EXAMPLES[field]}</code>"
        for field in claims.EDITABLE.get(view.kind, ())
        if field in _CLAIM_TUZAT_EXAMPLES
    )
    return (
        f"✏️ <code>{handle}</code> ni tuzatish uchun yozing:\n{examples}\n"
        f"<i>Keyin ✅ Ha yoki ✖️ Yo'q.</i>"
    )


def claim_field_refused(view: claims.ClaimView, field: str) -> str:
    """`/tuzat c12 muddat …` on a claim that has no such field."""
    label = _FIELD_LABEL.get(field, field)
    return f"Bu da'voda {label}ni tuzatib bo'lmaydi.\n{claim_tuzat_hint(view)}"


def claim_value_refused(view: claims.ClaimView) -> str:
    """A field the claim has, but a value it cannot take (nothing to flip,
    an empty name)."""
    return f"Bu qiymat to'g'ri kelmadi.\n{claim_tuzat_hint(view)}"


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
    "extract_window_instant": "telegram suhbatlari (tezkor)",
    "extract_window_fallback": "telegram suhbatlari (qayta)",
    "transcribe": "ovozni matnga o'girish",
    "vision": "rasmlarni o'qish",
    "report": "kunlik hisobot",
    "planner": "reja tuzish",
    "rag": "savollarga javob",
    "profile": "odam haqida profil",
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
    unpriced = getattr(summary, "unpriced_calls", 0)
    if unpriced:
        parts.append(UNPRICED_LINE.format(n=unpriced))
    return clip("\n\n".join(parts))


UNPRICED_LINE = (
    "⚠️ {n} ta chaqiruvning narxi noma'lum — bu model narx jadvalida yo'q "
    "(.env: EXTRACT_MODEL_PRICE / REASON_MODEL_PRICE). Jami summa haqiqatdan kam "
    "ko'rinadi."
)


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
    # A low-confidence money SMS waits in /tekshir; it must name itself.
    "phone_sms": "sms",
    "manual": "qo'lda",
    "receipt_photo": "rasm",
    "calendar": "kalendar",
    "phone_notification": "ilova",
}


# --- /tekshir: the money block (WP-14) ---------------------------------------

MONEY_REASON_LABEL = {
    "declined": "rad etilgan ko'rinadi",
    "reversal": "qaytarish yoki bekor qilish",
    "pending": "hali o'tmagan (kutilmoqda)",
    "reminder": "eslatma — kelajakdagi to'lov",
    "future_date": "sana kelajakda",
    "advert": "reklamaga o'xshaydi",
    "advert_with_evidence": "reklamaga o'xshaydi",
    "conflict": "kirim yoki chiqim — aniq emas",
    "no_direction": "kirim yoki chiqim — aniq emas",
    "no_amount": "summa o'qilmadi",
    "no_evidence": "to'lov o'tgani aniq emas",
    "otp": "SMS-kod",
    "otp_conflict": "kod so'zi bor — to'lovmi, tekshiring",
    "info": "ma'lumot xabari",
    "autobook_off": "avtomatik yozish o'chirilgan",
}
MONEY_NO_AMOUNT = "summa o'qilmadi"
REVIEW_MONEY_HEADER = "💳 <b>Pul xabarlari — {n} ta tekshiruvda</b>"
REVIEW_IGNORED_HEADER = "🙈 <b>E'tiborsiz qoldirilganlar — oxirgi 7 kun, {n} ta</b>"
REVIEW_NOTHING = "✅ Qayta ishlanmagan yozuv yo'q."
REVIEW_BOOKED = "✅ Yozildi: {line} <code>x{id}</code>"
REVIEW_MERGED = (
    "🔗 Bu to'lov allaqachon bor: <code>x{id}</code> — ikkinchi marta yozilmadi."
)
REVIEW_NOT_MONEY = "✖️ Pul harakati emas deb belgilandi."
REVIEW_NOT_MONEY_BULK = "✖️ {n} ta eski xabar «pul emas» deb belgilandi."
REVIEW_SEEN = "✔️ Ko'rib chiqildi — ro'yxatdan olindi."
REVIEW_GONE = "Bu yozuv allaqachon ko'rib chiqilgan."
MONEY_REVIEW_LINE = reports.MONEY_REVIEW_LINE
PREVIEW_CHARS = 40


def money_review_line(interaction, number: int) -> str:
    """'#1 · 12-sen 14:30 · ✉️ Payme · 250 ming so'm · rad etilgan ko'rinadi'
    and the text's start on the next line."""
    media = interaction.media or {}
    money_info = media.get("money") or {}
    if interaction.source.value == "phone_notification":
        who = f"🔔 {escape(media.get('app_label') or 'ilova')}"
    else:
        who = f"✉️ {escape(media.get('sender') or 'sms')}"
    if money_info.get("amount"):
        amount = money(
            Decimal(money_info["amount"]),
            Currency(money_info.get("currency") or Currency.UZS.value),
        )
    else:
        amount = MONEY_NO_AMOUNT
    reason = MONEY_REASON_LABEL.get(money_info.get("reason") or "", "")
    text = " ".join((interaction.raw_text or "").split())
    preview = text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
    when = interaction.occurred_at
    head = f"#{number} · {day_label(when)} {clock(when)} · {who} · {amount}" + (
        f" · {reason}" if reason else ""
    )
    return f"{head}\n   «{escape(preview)}»"


def _money_block(header: str, rows, total: int, *, start: int = 1) -> str:
    lines = [money_review_line(row, start + i) for i, row in enumerate(rows)]
    return header.format(n=total) + "\n" + "\n".join(lines)


def review_report(
    interactions,
    total: int,
    money_rows=(),
    money_total: int = 0,
    ignored_rows=(),
    ignored_total: int = 0,
) -> str:
    """`/tekshir`: the money texts first, each with its one-tap answer, then
    what failed processing; `/tekshir hammasi` adds what the reader ignored."""
    blocks = []
    if money_rows:
        blocks.append(_money_block(REVIEW_MONEY_HEADER, money_rows, money_total))
    if interactions:
        blocks.append(_failed_block(interactions, total, start=len(money_rows) + 1))
    if ignored_rows:
        blocks.append(
            _money_block(
                REVIEW_IGNORED_HEADER,
                ignored_rows,
                ignored_total,
                start=len(money_rows) + len(interactions) + 1,
            )
        )
    if not blocks:
        return REVIEW_NOTHING
    return clip("\n\n".join(blocks))


def _failed_block(interactions, total: int, *, start: int) -> str:
    lines = []
    for number, it in enumerate(interactions, start):
        label = SOURCE_LABEL.get(it.source.value, it.source.value)
        preview = (it.raw_text or it.transcript or "").strip().replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        detail = f" — {escape(preview)}" if preview else ""
        filename = (it.meta or {}).get("filename") or (it.media or {}).get("filename")
        if not preview and filename:
            detail = f" — {escape(filename)}"
        # Numbered only where a ✔️ Ko'rdim button refers to the line.
        tag_ = f"#{number} · " if getattr(it, "processed", False) else ""
        lines.append(
            f"{tag_}{short_date(it.occurred_at.date())} {clock(it.occurred_at)} · "
            f"{label}{detail}"
        )

    header = f"⚠️ <b>{total} ta yozuv qayta ishlanmagan</b>"
    if total > len(interactions):
        header += f" (oxirgi {len(interactions)} tasi)"
    return header + "\n" + bullet_list(lines, empty="—")


# --- open loops: the morning brief, the nudge, a new group -------------------
#
# question_line / stale_line / quiet_line live in formatting.py: the report's
# data block renders the same rows in plain text (``markup=False``), and
# reports.py must not import this module. They are re-exported above.


BRIEF_HEADER = "🌅 <b>Ertalabki xulosa</b>"
BRIEF_ALL_CLEAR = "✅ Hammasi joyida — bugun uchrashuv ham, ochiq qolgan narsa ham yo'q."

BRIEF_EVENTS = "📅 <b>Bugungi uchrashuvlar</b>"
BRIEF_DUE = "⏰ <b>Muddati bugun va kechikkanlar</b>"
BRIEF_QUESTIONS = "❓ <b>Javobsiz qolganlar</b>"
# After the questions and before the claims: an unanswered ring outranks a
# decision the owner still has time to make.
BRIEF_MISSED = "📵 <b>Javobsiz qo'ng'iroqlar</b>"
BRIEF_STALE = "📌 <b>Muddatsiz, turib qolganlar</b>"
BRIEF_QUIET = "🤫 <b>Jim bo'lib qolganlar</b>"


BRIEF_CONTINUED = "🌅 <b>Ertalabki xulosa</b> (davomi {i}/{n})"
BRIEF_OVERFLOW = "<i>… qolgani sig'madi — to'liq ro'yxat: /ertalab</i>"
BRIEF_MAX_PARTS = 3


def morning_brief(brief: MorningBrief) -> str:
    """The morning message as one text (the parts joined)."""
    return "\n\n".join(morning_brief_parts(brief))


def morning_brief_parts(
    brief: MorningBrief, *, max_parts: int = BRIEF_MAX_PARTS
) -> list[str]:
    """The morning brief as Telegram messages: split between sections,
    never clipped (WP-50)."""
    return split_message(
        _morning_brief_text(brief),
        max_parts=max_parts,
        continued=BRIEF_CONTINUED,
        overflow=BRIEF_OVERFLOW,
    )


def visible_refs(parts: list[str], refs: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Only the refs whose handle the owner can actually see."""
    shown = "\n\n".join(parts)
    return [(kind, i) for kind, i in refs if _ref_handle(kind, i) in shown]


def _morning_brief_text(brief: MorningBrief) -> str:
    """The one morning message. Deterministic — SQL and the loops engine."""
    parts = [f"{BRIEF_HEADER} · {full_date(brief.day)}"]
    if brief.is_empty():
        return "\n\n".join([*parts, BRIEF_ALL_CLEAR])

    if brief.events:
        lines = [
            f"{clock(e.start_at)} — {escape(e.title)}"
            + (f" ({escape(e.location)})" if e.location else "")
            for e in brief.events
        ]
        parts.append(f"{BRIEF_EVENTS}\n" + bullet_list(lines, empty="—"))

    due_lines = [
        f"{escape(b.person.display_name)}: {money(b.outstanding, b.currency)} · "
        f"{relative_day(b.earliest_due)}" + tags("debt", b.ids)
        for b in brief.due.get("debts", [])
    ]
    due_lines += [
        f"{escape(person.display_name)}: {escape(p.description)} · "
        f"{relative_day(p.due_date)}" + tag("promise", p.id)
        for p, person in brief.due.get("promises", [])
    ]
    due_lines += [
        f"{escape(t.description)} · {relative_day(t.due_date)}" + tag("task", t.id)
        for t in brief.due.get("tasks", [])
    ]
    if due_lines:
        parts.append(f"{BRIEF_DUE}\n" + bullet_list(due_lines, empty="—"))

    loops = brief.loops
    if loops is not None and loops.questions:
        lines = [question_line(q) for q in loops.questions]
        parts.append(f"{BRIEF_QUESTIONS}\n" + bullet_list(lines, empty="—"))
    missed = _brief_missed(brief)
    if missed:
        lines = [missed_line(m) for m in missed]
        parts.append(f"{BRIEF_MISSED}\n" + bullet_list(lines, empty="—"))

    if loops is not None and loops.stale:
        lines = [stale_line(s) for s in loops.stale]
        parts.append(f"{BRIEF_STALE}\n" + bullet_list(lines, empty="—"))
    if loops is not None and loops.quiet:
        lines = [quiet_line(q) for q in loops.quiet]
        parts.append(f"{BRIEF_QUIET}\n" + bullet_list(lines, empty="—"))
    if getattr(brief, "money_review", 0):
        parts.append(MONEY_REVIEW_LINE.format(n=brief.money_review))
    if getattr(brief, "media_expired", 0):
        parts.append(MEDIA_EXPIRED_LINE.format(n=brief.media_expired))
    if getattr(brief, "code_suggestions", 0):
        # Pulled, never pushed (WP-33): a count, no buttons, not a question.
        parts.append(CODE_SUGGESTIONS_LINE.format(n=brief.code_suggestions))
    line = queue_line(getattr(brief, "queue", None))
    if line:
        parts.append(line)

    return "\n\n".join(parts)


def morning_brief_refs(
    brief: MorningBrief, parts: list[str] | None = None
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """``(due, stale)`` — the rows the brief's buttons act on, in line order."""
    due: list[tuple[str, int]] = []
    for b in brief.due.get("debts", []):
        due += [("debt", i) for i in b.ids]
    due += [("promise", p.id) for p, _ in brief.due.get("promises", [])]
    due += [("task", t.id) for t in brief.due.get("tasks", [])]
    stale = (
        [(s.record_kind, s.record.id) for s in brief.loops.stale]
        if brief.loops is not None
        else []
    )
    if parts is not None:
        due, stale = visible_refs(parts, due), visible_refs(parts, stale)
    return due, stale


def _brief_missed(brief: MorningBrief) -> list[MissedCall]:
    """The missed-call loops the brief shows (build step 6).

    Read defensively, like the claims above: a brief built before the loops
    engine learned about missed calls simply has none.
    """
    loops = brief.loops
    if loops is None:
        return []
    return list(getattr(loops, "missed", None) or [])


NUDGE_HEADER = "❓ <b>Javobsiz savol</b>"
NUDGE_ANSWERED = "✅ Javob berilgan deb yozib qo'ydim — boshqa eslatmayman."


def nudge_snoozed(until: datetime) -> str:
    """After ⏰ Ertalab eslat: name the moment, since "the next brief" is today's
    09:00 for a tap before it (00:30 after an evening nudge, or 08:00) and
    tomorrow's for a tap after it."""
    local = until.astimezone(settings.tz)
    day = "Bugun" if local.date() == datetime.now(settings.tz).date() else "Ertaga"
    return f"⏰ {day} {local.strftime('%H:%M')} dagi ertalabki xulosada yana eslataman."


NUDGE_GONE = "⚠️ Bu savol eskirgan yoki yozuv o'chirilgan."


def nudge(q: UnansweredQuestion) -> str:
    """One short message per unanswered question: who asked, what, how long."""
    where = f" · {escape(q.chat_title)}" if q.is_group and q.chat_title else ""
    lines = [
        NUDGE_HEADER,
        f"<b>{escape(q.person_name)}</b>{where} · {age_label(q.age)} oldin",
        quote(q.text, limit=300),
    ]
    if q.follow_ups:
        lines.append(
            f"<i>Keyin yana {q.follow_ups} ta xabar keldi — hali javob yo'q.</i>"
        )
    return clip("\n".join(lines))


def nudge_overflow(count: int) -> str:
    return (
        f"❓ <i>… va yana {count} ta javobsiz savol — keyingi safar eslataman "
        f"(to'liq ro'yxat: /ertalab).</i>"
    )


# --- missed calls (build step 6) ---------------------------------------------
#
# The companion app uploads the call log; a missed or rejected ring nobody
# dealt with is an open loop (loops.missed_calls). The nudge mirrors the
# question nudge: once, plus once more after an "⏰ Ertalab eslat" snooze.

MISSED_NUDGE_HEADER = "📵 <b>Javobsiz qo'ng'iroq</b>"
MISSED_ANSWERED = "✅ Yozib qo'ydim — bog'landing."
MISSED_GONE = "⚠️ Bu qo'ng'iroq eskirgan yoki yozuvi o'chirilgan."


def missed_nudge(m: MissedCall) -> str:
    """One short message per missed call: who rang, how long ago, how often."""
    who = m.person_name if m.person is not None else (m.phone or "Noma'lum raqam")
    lines = [
        MISSED_NUDGE_HEADER,
        f"<b>{escape(who)}</b> · {age_label(m.age)} oldin qo'ng'iroq qildi — "
        "javob berilmadi.",
    ]
    if m.attempts > 1:
        lines.append(f"<i>Jami {m.attempts} marta urindi.</i>")
    return clip("\n".join(lines))


NEW_GROUP_GONE = "⚠️ Bu chat endi ro'yxatda yo'q."

# What a switched-off chat is called in the one-tap question. A channel is
# not a group to the owner, and chats.awaiting_join_question asks about both.
_NEW_CHAT_LABEL = {
    ChatType.channel: "📢 <b>Yangi kanal:</b>",
    ChatType.group: "👥 <b>Yangi guruh:</b>",
}


def new_group_question(
    title: str | None, tg_chat_id: int, chat_type: ChatType | None = None
) -> str:
    name = escape(title or f"chat {tg_chat_id}")
    label = _NEW_CHAT_LABEL.get(chat_type, _NEW_CHAT_LABEL[ChatType.group])
    return f"{label} {name} — o'qiymi?"


GROUP_DIGEST_HEADER = "👥 <b>Yangi guruhlar</b> — {n} ta. Qaysilarini o'qiyin?"
GROUP_DIGEST_HINT = (
    "<i>✅ bosilgani darhol yoqiladi, oxirgi {days} kuni ham o'qiladi. "
    "Bosilmaganlari o'chiq qoladi — keyin /chats dan yoqsa bo'ladi.</i>"
)
GROUP_DIGEST_LEGEND = "<i>📣 — senga murojaat qilishgan · ✍️ — o'zing yozgansan</i>"
GROUP_DIGEST_MORE = "<i>Yana {k} tasi keyingi safar.</i>"
GROUP_REST_DONE = "👌 {n} ta guruh o'chiq qoldi. Kerak bo'lsa /chats dan yoqasan."


def group_digest(monitors, *, more: int = 0, days: int | None = None) -> str:
    """One message for several undecided groups (WP-20); the rows are the
    buttons, so the text only frames them."""
    from miya.services.chats import BACKFILL_DAYS

    lines = [
        GROUP_DIGEST_HEADER.format(n=len(monitors)),
        GROUP_DIGEST_HINT.format(days=days or BACKFILL_DAYS),
    ]
    if any(m.addressed_at or m.owner_active_at for m in monitors):
        lines.append(GROUP_DIGEST_LEGEND)
    if more > 0:
        lines.append(GROUP_DIGEST_MORE.format(k=more))
    return "\n".join(lines)


def new_group_accepted(title: str | None, tg_chat_id: int, days: int) -> str:
    name = escape(title or f"chat {tg_chat_id}")
    return f"✅ <b>{name}</b> — endi o'qiyman. Oxirgi {days} kunini ham o'qib chiqaman."


def new_group_declined(title: str | None, tg_chat_id: int) -> str:
    name = escape(title or f"chat {tg_chat_id}")
    return f"👌 <b>{name}</b> — o'qimayman. Kerak bo'lsa /chats dan yoqasiz."


# --- /holat: is MIYA alive, and what to type if not (build step 5) -----------
#
# One screen, one line per part, ✅ / ⚠️ / ❌ / ⏸ at the front so the owner
# reads the colour before the words. The judgements come from
# services/health.py; this only says them in Uzbek. Every dynamic string
# (a job id, a bot username) is escaped, the whole thing is clipped.

STATUS_HEADER = "🩺 <b>MIYA holati</b>"
STATUS_NEVER = "hali yo'q"

DB_DOWN_ALERT = (
    "❌ Baza javob bermayapti — hech narsa yozilmayapti va o'qilmayapti. "
    "Serverda: <code>docker compose ps</code>, <code>docker compose logs db</code>, "
    "keyin <code>make up</code>."
)


def _ago(age: timedelta | None) -> str:
    """'6 daqiqa oldin', or the honest gap when it never happened."""
    if age is None:
        return STATUS_NEVER
    if age < timedelta(minutes=1):
        return "hozirgina"
    return f"{age_label(age)} oldin"


def _since_when(when: datetime | None, now: datetime) -> str:
    return _ago(now - when) if when is not None else STATUS_NEVER


def _day_clock(when: datetime, now: datetime) -> str:
    """'bugun 03:30' / 'kecha 03:30' / '12-sen 03:30'."""
    local = when.astimezone(settings.tz)
    days = (now.astimezone(settings.tz).date() - local.date()).days
    if days == 0:
        day = "bugun"
    elif days == 1:
        day = "kecha"
    else:
        day = short_date(local.date())
    return f"{day} {clock(local)}"


def worker_silent_alert(worker: health.Component) -> str:
    """The bot's own alarm: the scheduler has stopped beating.

    The same key as services/health.py's ``worker_silent`` and the same
    remedy, so the shared ledger dedupes whichever process speaks first.
    """
    since = (
        f"{age_label(worker.age)}dan beri jim"
        if worker.age is not None
        else "hali bir marta ham xabar bermagan"
    )
    return (
        f"❌ Rejalashtiruvchi (worker) {since} — eslatmalar, hisobot va zaxira "
        "nusxa to'xtab turibdi. Serverda: <code>docker compose restart worker</code>, "
        "keyin <code>make worker</code> bilan logni ko'r."
    )


def _bot_line(status: health.Status) -> str:
    # This very message is the proof: the bot rendering it is alive.
    username = status.components["bot"].detail.get("username")
    tail = f" · @{escape(str(username))}" if username else ""
    return f"✅ Bot — ishlayapti{tail}"


def _userbot_line(status: health.Status) -> str:
    userbot = status.components["userbot"]
    if userbot.disabled:
        return "⏸ Telegram o'quvchi — o'chirilgan (USERBOT_ENABLED=false)"
    last = f"oxirgi xabar: {_since_when(status.userbot_last_message_at, status.now)}"
    if userbot.stale:
        if userbot.age is None:
            return f"❌ Telegram o'quvchi — hali ulanmagan · {last}"
        return f"❌ Telegram o'quvchi — {age_label(userbot.age)}dan beri jim · {last}"
    if userbot.detail.get("connected") is False:
        return f"⚠️ Telegram o'quvchi — Telegramdan uzilgan · {last}"
    return f"✅ Telegram o'quvchi — ulangan · {last}"


CATCH_UP_LINE = "🔄 Telegram: uzilishdan keyin {n} ta xabar qayta o'qildi ({soat})."


def _catch_up_line(status: health.Status) -> str | None:
    """For a day after a catch-up that stored anything (WP-21)."""
    userbot = status.components.get("userbot")
    detail = (userbot.detail if userbot is not None else None) or {}
    count, at = detail.get("caught_up"), detail.get("caught_up_at")
    if not count or not at:
        return None
    try:
        when = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None
    if status.now - when > timedelta(hours=24):
        return None
    return CATCH_UP_LINE.format(n=count, soat=clock(when))


def _worker_line(status: health.Status) -> str:
    worker = status.components["worker"]
    if not worker.stale:
        return f"✅ Rejalashtiruvchi — oxirgi urish: {_ago(worker.age)}"
    if worker.age is None:
        return "❌ Rejalashtiruvchi — hali bir marta ham urmagan"
    line = f"❌ Rejalashtiruvchi — {age_label(worker.age)}dan beri jim"
    # The job that has waited longest says what the owner is missing.
    known = [job for job in status.jobs.values() if job.age is not None]
    if known:
        oldest = max(known, key=lambda job: job.age)
        line += f" · eng eski ish: {escape(oldest.name)} ({_ago(oldest.age)})"
    return line


def _db_line(status: health.Status) -> str:
    if not status.db_ok:
        return "❌ Baza — javob bermayapti"
    return f"✅ Baza — javob beryapti · {health.size_label(status.db_size_bytes)}"


def _disk_line(status: health.Status) -> str:
    if status.disk_free_bytes is None:
        return "⚠️ Disk — o'lchab bo'lmadi"
    free = health.size_label(status.disk_free_bytes)
    if status.disk_low:
        return f"❌ Disk — {free} bo'sh (chegara {settings.disk_min_free_gb:g} GB)"
    return f"✅ Disk — {free} bo'sh"


def _backup_line(status: health.Status) -> str:
    info = status.backup
    if not info.configured:
        return "⚠️ Zaxira nusxa — sozlanmagan (BACKUP_AGE_RECIPIENT bo'sh)"
    if info.path is None or info.created_at is None:
        return f"⚠️ Zaxira nusxa — {STATUS_NEVER}"
    when = _day_clock(info.created_at, status.now)
    size = health.size_label(info.size)
    if info.stale:
        return f"⚠️ Zaxira nusxa — {when} · {size} · eskirgan"
    if info.sent_to_telegram:
        return f"✅ Zaxira nusxa — {when} · {size} · Telegramga yuborildi"
    if info.send_failed:
        return f"⚠️ Zaxira nusxa — {when} · {size} · Telegramga yuborilmadi"
    if not settings.backup_to_telegram:
        return f"✅ Zaxira nusxa — {when} · {size} · faqat diskda"
    return f"⚠️ Zaxira nusxa — {when} · {size} · Telegramga hali yuborilmagan"


def _anthropic_line(status: health.Status) -> str:
    if status.anthropic_failing:
        return (
            "⚠️ Anthropic — oxirgi muvaffaqiyat: "
            f"{_since_when(status.anthropic_last_ok_at, status.now)} · "
            f"{status.windows_pending} ta kutmoqda, {status.windows_failed} ta xato"
        )
    if status.anthropic_last_ok_at is None:
        return "✅ Anthropic — hali ishlatilmagan"
    last = _since_when(status.anthropic_last_ok_at, status.now)
    return f"✅ Anthropic — oxirgi muvaffaqiyat: {last}"


def _search_line(status: health.Status) -> str:
    backlog = getattr(status, "embed_backlog", 0)
    oldest = getattr(status, "embed_oldest_at", None)
    if not backlog or oldest is None:
        return "✅ Qidiruv — tayyor"
    mark = "⚠️" if health.search_stale(status) else "⏳"
    return (
        f"{mark} Qidiruv — {backlog} ta yozuv kutmoqda "
        f"({age_label(status.now - oldest)}dan beri)"
    )


def _phone_line(status: health.Status) -> str | None:
    """The companion app's last accepted batch — informational, never a fault.

    None (no line at all) when no phone has ever uploaded: a phone-less
    install is healthy and should not read as missing something.
    """
    phone = getattr(status, "phone", None)
    if phone is None:
        return None
    detail = phone.detail or {}
    parts = []
    if detail.get("calls"):
        parts.append(f"{detail['calls']} qo'ng'iroq")
    if detail.get("sms"):
        parts.append(f"{detail['sms']} sms")
    tail = f" ({', '.join(parts)})" if parts else ""
    return f"📱 Telefon — oxirgi yuklash: {_ago(phone.age)}{tail}"


OWNER_ALIASES_BLANK = (
    "ℹ️ OWNER_ALIASES bo'sh — guruhlarda ismingizni yozib murojaat qilinganlar "
    "«sizga» deb belgilanmaydi (faqat @-eslatma va javoblar). .env ga qo'shing."
)


def status_report(
    status: health.Status,
    problems: list[health.Problem],
    *,
    questions_waiting: int | None = None,
    questions_used: int = 0,
) -> str:
    """`/holat`: every part of MIYA on one line each, then what to do."""
    now = status.now.astimezone(settings.tz)
    lines = [
        f"{STATUS_HEADER} · {short_date(now.date())} {clock(now)}",
        _bot_line(status),
        _userbot_line(status),
        *([line] if (line := _catch_up_line(status)) else []),
        _worker_line(status),
        _db_line(status),
        _disk_line(status),
        _backup_line(status),
        _anthropic_line(status),
        _search_line(status),
        *([line] if (line := _phone_line(status)) else []),
        "<b>Navbatda</b>: "
        f"kutayotgan suhbatlar {status.windows_pending} · "
        f"batch'da {status.windows_submitted} · "
        f"ishlanmagan {status.needs_review} (/tekshir) · "
        + (
            f"pul tekshiruvi {status.money_review} · "
            if getattr(status, "money_review", 0)
            else ""
        )
        + (
            f"savollar {questions_waiting} (/savollar) · "
            f"bugun {questions_used}/{settings.question_budget_per_day}"
            if questions_waiting is not None
            else f"da'volar {status.claims_pending} (/davolar)"
        ),
        "<b>Xarajat</b>: "
        f"bugun {usd(status.cost_today_usd)} · "
        f"bu oy {usd(status.cost_month_usd)} (/xarajat)",
    ]
    if not settings.owner_aliases_parsed:
        # Information, not a problem: it never alerts, it only explains why
        # plain-text "Bekzod aka, …" in groups is not marked as his.
        lines.append(OWNER_ALIASES_BLANK)
    if problems:
        lines.append("")
        lines += [problem.text for problem in problems]
    return clip("\n".join(lines))


# --- the question batch (WP-18) --------------------------------------------------
#
# One sender (worker.question_job) puts up to QUESTION_BATCH_MAX questions in
# one numbered message; each line's buttons carry the same number.

QUESTIONS_HEADER = "❓ <b>Savollar</b> · bugun {used}/{budget}"
QUESTIONS_FOOTER = (
    "<i>Javob bermasang ham hech narsa yo'qolmaydi — hammasi /savollar da turadi.</i>"
)
CLAIMS_QUEUED = "<i>Tasdiqlash navbatda — /savollar</i>"
MONEY_ITEM_LINE = "💳 {clock} · {amount} · {label} — kirimmi, chiqimmi yoki pul emasmi?"


def _media_item_line(interaction, people: dict | None) -> str:
    media = dict(interaction.media or {})
    person = (people or {}).get(interaction.person_id)
    who = escape(person.display_name) if person is not None else "Nomaʼlum"
    kind = MEDIA_ASK_LABELS.get(
        approvals_reason(interaction), str(media.get("type") or "fayl")
    )
    detail = " · ".join(
        p for p in (escape(media.get("filename") or ""), _size(media.get("size"))) if p
    )
    return (
        f"📎 {who} {escape(kind)} yubordi"
        + (f" · {detail}" if detail else "")
        + (" — o'qiymi?")
    )


def approvals_reason(interaction) -> str:
    approval = (interaction.media or {}).get("approval") or {}
    return str(approval.get("reason") or "")


def _money_item_line(interaction) -> str:
    info = (interaction.media or {}).get("money") or {}
    amount = (
        money(
            Decimal(info["amount"]),
            Currency(info.get("currency") or Currency.UZS.value),
        )
        if info.get("amount")
        else MONEY_NO_AMOUNT
    )
    return MONEY_ITEM_LINE.format(
        clock=clock(interaction.occurred_at),
        amount=amount,
        label=MONEY_REASON_LABEL.get(info.get("reason") or "", ""),
    )


def question_item_line(item, people: dict | None = None) -> str:
    """One queued question (questions.Pending) as one line, number excluded."""
    subject = item.subject
    if item.kind == "claim":
        return claim_line(claims.view(subject))
    if item.kind == "missed":
        return missed_line(subject)
    if item.kind == "nudge":
        return "❓ " + question_line(subject)
    if item.kind == "media":
        return _media_item_line(subject, people)
    if item.kind == "money":
        return _money_item_line(subject)
    # still_open
    body = (
        _balance_line(subject.balance)
        if subject.balance is not None
        else record_line(subject.kind, subject.record, subject.person)
    )
    return f"📌 {body} — hali ochiqmi?"


def question_batch(
    items, *, header: str, used: int, budget: int, people: dict | None = None
) -> tuple[str, int]:
    """(body, how many lines fit). ``used`` counts this batch whole; when not
    everything fits, the header counts only what is shown."""
    lines = [question_item_line(item, people) for item in items]
    kept: list[str] = []
    for line in lines:
        numbered = [f"{n}. {text}" for n, text in enumerate([*kept, line], 1)]
        trial = "x" * 60 + "\n" + "\n".join(numbered) + "\n" + QUESTIONS_FOOTER
        if len(trial) > TELEGRAM_LIMIT - 60:
            break
        kept.append(line)
    used -= len(lines) - len(kept)
    title = header.format(used=used, budget=budget)
    body = "\n".join(
        [title, *(f"{n}. {text}" for n, text in enumerate(kept, 1)), QUESTIONS_FOOTER]
    )
    return body, len(kept)


QUESTIONS_BRIEF_HEADER = "❓ <b>Bugungi savollar</b> · bugun {used}/{budget}"
QUESTIONS_EVENING_HEADER = "❓ <b>Kun yakunidagi savollar</b> · bugun {used}/{budget}"

# --- /savollar (WP-19): the pull view; answering here spends nothing ----------

SAVOLLAR_PAGE_SIZE = 8
SAVOLLAR_HEADER = (
    "❓ <b>Savollar</b> — {total} ta javob kutmoqda · bugun {used}/{budget} ta so'radim"
)
SAVOLLAR_EMPTY = "✅ Javob kutayotgan savol yo'q."
SAVOLLAR_BUDGET_USED = (
    "<i>Bugungi {budget} ta savol chegarasi tugadi — qolganlarini ertalab so'rayman. "
    "Hozir o'zing javob bersang ham bo'ladi.</i>"
)
SAVOLLAR_PAGE = "<i>{page}/{pages}-sahifa</i>"
SAVOLLAR_GROUPS_BUTTON = "👥 Yangi guruhlar ({k} ta)"


def savollar(
    items,
    *,
    page: int,
    pages: int,
    total: int,
    used: int,
    budget: int,
    people: dict | None = None,
) -> str:
    if total == 0:
        return SAVOLLAR_EMPTY
    lines = [SAVOLLAR_HEADER.format(total=total, used=used, budget=budget)]
    lines += [
        f"{n}. {question_item_line(item, people)}" for n, item in enumerate(items, 1)
    ]
    if used >= budget:
        lines.append(SAVOLLAR_BUDGET_USED.format(budget=budget))
    if pages > 1:
        lines.append(SAVOLLAR_PAGE.format(page=page, pages=pages))
    return clip("\n".join(lines))


# --- client codes by hand (WP-33) ---------------------------------------------

KOD_USAGE = (
    "🏷 <b>Mijoz kodlari</b>\n"
    "<code>/kod Akmal GS367</code> — kodni odamga biriktirish\n"
    "<code>/kod GS367</code> — bu kod kimniki\n"
    "<code>/kod GS367 o'chir</code> — kodni olib tashlash\n"
    "<code>/kod yangi Akmal GS367</code> — yangi odam qo'shib, kod berish\n"
    "<code>/kodlar</code> — xabarlardan topilgan takliflar"
)
CODE_STALE = "Bu savol eskirgan — <code>/kod</code> ni qaytadan yozing."
CODE_MOVE_DECLINED = "O'zgarmadi."
KODLAR_HEADER = "🏷 <b>Kod takliflari</b> — xabarlardan topildi. To'g'risini tasdiqlang:"
KODLAR_EMPTY = "🏷 Yangi kod taklifi yo'q."
MEDIA_EXPIRED_LINE = "📎 {n} ta fayl so'ralmay eskirdi — xabarlari saqlangan."
CODE_SUGGESTIONS_LINE = "🏷 {n} ta kod taklifi kutyapti — /kodlar"


def code_attached(code: str, name: str, all_codes: list[str]) -> str:
    text = f"🏷 <b>{escape(code)}</b> → <b>{escape(name)}</b> biriktirildi."
    if len(all_codes) > 1:
        text += f"\nBarcha kodlari: {escape(', '.join(all_codes))}"
    return text


def code_already(code: str, name: str) -> str:
    return f"🏷 <b>{escape(code)}</b> allaqachon shu odamda: <b>{escape(name)}</b>."


def code_taken(code: str, holder: str, target: str) -> str:
    return (
        f"⚠️ <b>{escape(code)}</b> hozir boshqa odamda: <b>{escape(holder)}</b>.\n"
        f"<b>{escape(target)}</b> nomiga o'tkazaymi?"
    )


def code_moved(code: str, target: str, holder: str) -> str:
    return (
        f"🏷 <b>{escape(code)}</b> endi <b>{escape(target)}</b> nomida "
        f"(avval: <b>{escape(holder)}</b>). Eski bog'lanish tarixda saqlandi."
    )


def code_person_not_found(name: str, code: str) -> str:
    return (
        f"❓ «<b>{escape(name)}</b>» topilmadi. Yangi odam qilib qo'shish: "
        f"<code>/kod yangi {escape(name)} {escape(code)}</code>"
    )


def code_detached(code: str, name: str) -> str:
    return f"🗑 <b>{escape(code)}</b> olib tashlandi (avval: <b>{escape(name)}</b>)."


def code_bad(text: str) -> str:
    return (
        f"❓ Bu mijoz kodiga o'xshamaydi: «{escape(text)}». "
        f"Masalan: <code>GS367</code>"
    )


def code_suggestion_line(code: str, name: str, day: str, excerpt: str) -> str:
    return (
        f"• <b>{escape(code)}</b> → <b>{escape(name)}</b> "
        f"<i>({escape(day)}: «{escape(excerpt)}»)</i>"
    )


def code_suggestion_accepted(code: str, name: str) -> str:
    return f"✅ <b>{escape(code)}</b> → <b>{escape(name)}</b>."


def code_suggestion_rejected(code: str, name: str) -> str:
    return (
        f"✖️ Rad etildi: <b>{escape(code)}</b> → <b>{escape(name)}</b>. "
        f"Qayta so'ramayman."
    )


# --- client-list import (WP-34) -------------------------------------------------

IMPORT_BAD_FILE = (
    "⚠️ Faylni o'qib bo'lmadi. Kerakli ustunlar: <code>kod</code>, <code>ism</code> "
    "(ixtiyoriy: <code>telefon</code>, <code>telegram</code>, <code>izoh</code>). "
    "CSV yoki Excel (.xlsx)."
)
IMPORT_CANCELLED = "Bekor qilindi — hech narsa yozilmadi."
IMPORT_CONFLICTS_MAX = 10


def import_preview(filename: str, counts: dict[str, int]) -> str:
    return (
        f"📋 <b>Mijozlar ro'yxati</b> ({escape(filename)})\n"
        f"• Kod biriktiriladi: {counts['attach']}\n"
        f"• Yangi odam qo'shiladi: {counts['create']}\n"
        f"• Allaqachon to'g'ri: {counts['same']}\n"
        f"• To'qnashuv (kod boshqa odamda): {counts['conflict']}\n"
        f"• O'qib bo'lmadi: {counts['bad']}\n\n"
        f"Yozaymi?"
    )


def import_done(written: dict[str, int], conflicts) -> str:
    """``conflicts``: (code, the name in the file, who holds it)."""
    lines = [
        f"✅ Yozildi: {written['attach']} ta kod, {written['create']} ta yangi odam."
    ]
    conflicts = list(conflicts)
    if conflicts:
        lines[0] += " To'qnashuvlar o'zgartirilmadi:"
        lines += [
            f"• <b>{escape(code)}</b>: faylda «{escape(name)}», "
            f"bazada <b>{escape(holder)}</b>"
            for code, name, holder in conflicts[:IMPORT_CONFLICTS_MAX]
        ]
    return "\n".join(lines)


# --- exact code and waybill lookups (WP-40) -----------------------------------------

CODE_MENTIONS_HEADER = "📦 <b>Kodi tilga olingan xabarlar</b>"
EXACT_HITS_HEADER = "📦 <b>Aniq topilganlar</b>"
YUK_USAGE = "Yuk xati raqamini yozing: <code>/yuk YW26-004715</code>"
QIDIR_EXACT_MAX = 5


def mention_line(line) -> str:
    """'{day} {clock} · {chat yoki odam} · «{excerpt}»'."""
    where = line.chat_title or line.speaker or "eslatma"
    return (
        f"{day_label(line.when)} {clock(line.when)} · {escape(where)} · "
        f"{quote(line.text, 160)}"
    )


def yuk_report(code: str, lines) -> str:
    """`/yuk`: every mention, oldest at the top, newest at the bottom."""
    if not lines:
        return f"📦 <b>{escape(code)}</b> hech bir xabarda uchramadi."
    header = f"📦 <b>{escape(code)}</b> — {len(lines)} ta xabarda:"
    rendered = [mention_line(line) for line in lines]  # newest first
    shown = _fit_oldest_first(rendered, budget=TELEGRAM_LIMIT - len(header) - 40)
    return clip(f"{header}\n" + bullet_list(shown, empty="—"))


def exact_hits_block(lines) -> str:
    shown = [mention_line(line) for line in lines[:QIDIR_EXACT_MAX]]
    return f"{EXACT_HITS_HEADER}\n" + bullet_list(shown, empty="—")


def money_split_done(txn) -> str:
    """The ➕ Bu boshqa to'lov answer (WP-42)."""
    icon = "📈" if txn.type.value == "income" else "📉"
    label = "Kirim" if txn.type.value == "income" else "Chiqim"
    return (
        f"✅ Alohida yozildi: {icon} {label} {money(txn.amount, txn.currency)}"
        f"{tag('transaction', txn.id)}"
    )


def money_typed_confirmed(txn) -> str:
    """The bank confirmed a payment the owner had typed (WP-42, optional)."""
    amount = money(txn.amount, txn.currency)
    return f"✉️ SMS tasdiqladi:{tag('transaction', txn.id)} — {amount}."


MONEY_SPLIT_GONE = "Bu to'lov allaqachon alohida yozilgan yoki topilmadi."


# --- what MIYA resolved by itself (WP-45) -------------------------------------------

AUTO_RESOLVED_LINE = "🤖 Bugun {n} ta savolni o'zim hal qildim — /savollar hal"
AUTO_RESOLVED_HEADER = "🤖 <b>O'zim hal qilganlarim</b> — oxirgi 7 kun"
AUTO_RESOLVED_EMPTY = "🤖 Oxirgi 7 kunda o'zim hal qilgan savol yo'q."
AUTO_RESOLVED_MAX = 25


def _claim_what(view) -> str:
    if view.amount is not None and view.currency is not None:
        return money(view.amount, view.currency)
    return escape(view.description or "—")


def auto_claim_line(claim) -> str:
    view = claims.view(claim)
    head = f"🤖 <code>{claim_ref(claim.id)}</code> {escape(view.person_name or '?')}"
    what = _claim_what(view)
    if claim.answered_by == claims.BY_AUTO_BANK and view.evidence is not None:
        when = view.evidence.occurred_at.astimezone(settings.tz)
        tail = f"bank SMS bilan tasdiqlandi ({short_date(when.date())} {clock(when)})"
    elif claim.answered_by == claims.BY_AUTO_DUPLICATE and claim.duplicate_of:
        primary = claim_ref(claim.duplicate_of)
        tail = f"takror da'vo, <code>{primary}</code> javobi bilan yopildi"
    else:
        handle = next(
            (
                h.get("new")
                for h in reversed(claim.history or [])
                if h.get("field") == "auto"
            ),
            None,
        )
        tail = "buni o'zing allaqachon yozgansan" + (
            f" (<code>{escape(handle)}</code>)" if handle else ""
        )
    return f"{head}: {what} — {tail}"


def auto_group_line(monitor) -> str:
    title = escape(monitor.title or str(monitor.tg_chat_id))
    if monitor.decided_by == "rule:channel":
        return f"📢 {title} — kanal, so'ramadim (o'chiq)"
    return f"👥 {title} — ikki marta javobsiz qoldi, o'chiq qoldirdim"


def auto_media_line(interaction, people: dict | None = None) -> str:
    media = dict(interaction.media or {})
    person = (people or {}).get(interaction.person_id)
    who = escape(person.display_name) if person is not None else "Nomaʼlum"
    kind = MEDIA_ASK_LABELS.get(
        approvals_reason(interaction), str(media.get("type") or "fayl")
    )
    return f"📎 {who} {escape(kind)} — so'ralmay eskirdi"


def auto_resolved_report(resolved, people: dict | None = None) -> str:
    if not resolved.total:
        return AUTO_RESOLVED_EMPTY
    lines = [auto_claim_line(c) for c in resolved.claims[:AUTO_RESOLVED_MAX]]
    lines += [auto_group_line(m) for m in resolved.groups]
    lines += [auto_media_line(i, people) for i in resolved.media]
    return clip(AUTO_RESOLVED_HEADER + "\n" + "\n".join(lines))


def claim_reopened(view) -> str:
    return f"↩️ <b>{claim_ref(view.id)} yana ochiq</b> — javob ber:\n{claim_line(view)}"
