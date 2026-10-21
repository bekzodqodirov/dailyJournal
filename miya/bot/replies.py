"""Owner-facing message bodies, in Uzbek."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from miya.bot.formatting import (
    PRIORITY_LABEL,
    SOURCE_WORD,
    TELEGRAM_LIMIT,
    WEEKDAYS_UZ,
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
    jarvis_day_lines,
    jarvis_intent_label,
    missed_line,
    money,
    new_record_lines,
    person_label,
    question_line,
    queue_line,
    quiet_line,
    quote,
    record_due,
    record_line,
    record_short_due,
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
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    InteractionSource,
    PromiseMadeBy,
)
from miya.services import claims, health, reports, safety
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
/birlashtir &lt;kim&gt; &gt; &lt;kimga&gt; — ikki yozuvni bitta odamga qo'shish
/kodlar — xabarlardan topilgan kod takliflari
/tarix &lt;ism&gt; [N] — odam bilan to'liq aloqa tarixi (oxirgi N ta)
/eslab &lt;ism&gt;: &lt;matn&gt; — odam haqida biror narsani eslab qolish
/yuk &lt;YW26-004715 yoki GS367&gt; — yuk xati yoki kod qayerda tilga olingan
/qidir &lt;so'z&gt; — xotiradan qidirish
/hisobot — bugun nima bo'ldi (hozirgacha)
/kecha — kecha nima bo'ldi (qisqa xulosa)
/fikr &lt;matn&gt; — yozuvlarga qarab fikrimni aytaman
/savol &lt;matn&gt; — yozib qo'ymasdan, savol sifatida javob beraman
/manba &lt;m123&gt; — javobdagi manbaning asl matni
/ertalab — kechagi xulosa va bugungi ishlar, ochiq qolganlar
/reja — ertangi reja
/chats — qaysi Telegram chatlar o'qilishi
/process — javob yozilgan media'ni qayta ishlash
/xarajat — MIYA'ning API xarajati
/holat — MIYA'ning ahvoli
/jarvis — Jarvis holati (toxtat — to'xtatish, yoq [kanal] — yoqish, tarix, rejim)
/shaxsiy &lt;ism yoki GS kod&gt; — shaxsiy chat: MIYA u yerga hech qachon o'zi yozmaydi
/shaxsiylar — shaxsiy chatlar va oila/do'st takliflari
/guruhai &lt;guruh&gt; — guruhda MIYA javob yozishini yoqish yoki o'chirish
/uslub [yukla] — yozish uslubingiz (yukla — eski yozishmalardan o'rganish)
/bilim [qo'sh|tuzat|o'chir|taklif] — bilimlar bazasi: mijozlarga umumiy javoblar
/savolbank [qo'sh|o'chir] — savollar banki: MIYA mijozga o'zi faqat shu savollarni beradi
/qoralamalar — tasdiq kutayotgan qoralamalar va kuzatuv rejimidagi javoblar
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
    ("manba", "Javobdagi manbaning asl matni"),
    ("hisobot", "Bugun nima bo'ldi"),
    ("kecha", "Kecha nima bo'ldi"),
    ("fikr", "Yozuvlarga qarab fikr"),
    ("savol", "Savol sifatida javob"),
    ("ertalab", "Ertalabki xulosa"),
    ("reja", "Ertangi reja"),
    ("chats", "Qaysi chatlar o'qilishi"),
    ("xarajat", "MIYA'ning API xarajati"),
    ("holat", "MIYA'ning ahvoli"),
    ("jarvis", "Jarvis holati, to'xtatish va yoqish"),
    ("shaxsiy", "Chatni shaxsiy deb belgilash"),
    ("shaxsiylar", "Shaxsiy chatlar va takliflar"),
    ("guruhai", "Guruhda MIYA javobini yoqish/o'chirish"),
    ("uslub", "Yozish uslubingiz"),
    ("bilim", "Bilimlar bazasi: umumiy javoblar"),
    ("savolbank", "Savollar banki: MIYA o'zi beradigan savollar"),
    ("qoralamalar", "Kutayotgan va kuzatuv qoralamalari"),
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
    ("birlashtir", "Ikki odamni birlashtirish"),
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

# --- the dangerous-file shield (JV-01) ----------------------------------------

DANGEROUS_FILE_CAUTION = (
    "Tanish odamdan kelgan bo'lsa ham ehtiyot bo'ling. Shubha bo'lsa, unga qo'ng'iroq "
    "qilib so'rang."
)
# Where a file arrived, when the chat is a private one: its title would only
# repeat the sender's name.
PRIVATE_CHAT_LABEL = "Telegram"
APK_RULE_LINE = (
    "MIYA ilovasi faqat MIYA botidan keladi; boshqa hech kimdan kelgan APK faylni "
    "o'rnatmang."
)


def _shown(text: str) -> str:
    """A sender-chosen name, HTML-escaped and without bidi or zero-width
    characters: an RLO must not make «gpj.apk» read as «kpa.jpg» or reverse
    the rest of the warning."""
    return escape(safety.display_name(text))


def dangerous_file_warning(
    *, who: str | None, filename: str | None, chat: str, android: bool = False
) -> str:
    """Someone sent an installer or an archive into a chat; MIYA left it alone."""
    lines = [
        f"⚠️ {_shown(who or 'Nomaʼlum')} sizga «{_shown(filename or 'fayl')}» faylini "
        f"yubordi ({_shown(chat)}). Bu o'rnatiladigan dastur yoki arxiv — xavfli "
        "bo'lishi mumkin. Ochmang va o'rnatmang. MIYA uni yuklab olmadi.",
        DANGEROUS_FILE_CAUTION,
    ]
    if android:
        lines.append(APK_RULE_LINE)
    return "\n".join(lines)


def dangerous_file_refused(filename: str | None, *, android: bool = False) -> str:
    """The owner sent the bot an installer or an archive: it was not fetched."""
    text = (
        f"⚠️ «{_shown(filename or 'fayl')}» — o'rnatiladigan dastur yoki arxiv. MIYA "
        "bunday fayllarni yuklab olmaydi va ochmaydi. Ichidagi ma'lumot kerak bo'lsa, "
        "PDF yoki rasm qilib yuboring."
    )
    return f"{text}\n{APK_RULE_LINE}" if android else text


def macro_document_note(filename: str | None) -> str:
    """A macro-enabled document was read as text only."""
    return (
        f"ℹ️ «{_shown(filename or 'fayl')}» makrosli hujjat. MIYA uni faqat matn "
        "sifatida o'qidi. Kompyuterda ochsangiz, makrosni yoqmang."
    )


# --- the spam and scam message shield (JV-09) ---------------------------------

SCAM_WARNING = (
    "🚫 {ism}dan kelgan xabar firibgarlikka o'xshaydi ({sabab}). Javob bermang, "
    "havolani ochmang."
)
HIJACK_WARNING = (
    "⚠️ {ism}ning akkaunti buzilgan bo'lishi mumkin: odatda bunday yozmaydi. Unga "
    "qo'ng'iroq qilib tekshiring, pul o'tkazmang."
)
UNSAFE_IGNORED_LINE = "🚫 Kecha {n} ta spam/firibgar xabar e'tiborsiz qoldirildi."
UNKNOWN_SENDER = "Nomaʼlum"
# {sabab}: what made a message look like a scam, the hard rules only, in the
# order the shield found them.
SCAM_REASON_LABELS: dict[str, str] = {
    safety.R_TEXT_LINK_MISMATCH: "havola yozilgan saytga emas, boshqasiga olib boradi",
    safety.R_LOOKALIKE_DOMAIN: "bank yoki to'lov saytiga o'xshatilgan soxta sayt",
    safety.R_BRAND_DOMAIN: "tanish nom begona saytda",
    safety.R_MIXED_SCRIPT_DOMAIN: "sayt nomida kirill harflari yashiringan",
    safety.R_USERINFO_URL: "havolaning haqiqiy manzili yashirilgan",
    safety.R_EXEC_URL: "havola o'rnatiladigan dasturga olib boradi",
    safety.R_ARCHIVE_URL: "havola arxiv faylga olib boradi",
    safety.R_EXEC_FILE: "o'rnatiladigan dastur yuborilgan",
    safety.R_ARCHIVE_FILE: "arxiv fayl yuborilgan",
    safety.R_CODE_REQUEST: "SMS kod yoki parol so'ralyapti",
    safety.R_CARD_DATA_REQUEST: "karta ma'lumotlari so'ralyapti",
    safety.R_TELEGRAM_SCAM: "Telegram bu akkauntni firibgar deb belgilagan",
    safety.R_TELEGRAM_FAKE: "Telegram bu akkauntni soxta deb belgilagan",
    safety.R_PHOTO_LURE: "«rasmda sizmi?» degan aldov",
    safety.R_BRAND_PATH: "bank yoki to'lov sayti nomi begona sayt ichiga yashirilgan",
    safety.R_LOGIN_PAGE: "begona saytda soxta kirish yoki tasdiqlash sahifasi",
    safety.R_HTML_FILE: "notanish odam HTML fayl yuborgan (soxta sahifa bo'lishi mumkin)",
    safety.R_MONEY_PRETEXT: (
        "o'zini tanish yoki xodim deb tanitib, shoshilinch pul so'ralyapti"
    ),
    safety.R_QR_RECEIVE: "«pulni olish uchun QR skanerlang» degan aldov",
    safety.R_VOTE_LINK: (
        "«tanlovda ovoz bering» degan havola (akkauntni o'g'irlash usuli)"
    ),
    safety.R_GIFT_LINK: "tanish nomdan sovg'a va begona havola",
    safety.R_FEE_LINK: "«posilka ushlab qolindi, boj to'lang» degan yashirin havola",
    safety.R_ADVANCE_FEE: "yutuq yoki daromad va'da qilinib, oldindan pul so'ralyapti",
    safety.R_PROFIT_PROMISE: "kafolatlangan yoki juda katta foyda va'da qilinyapti",
}
SCAM_REASON_FALLBACK = "shubhali belgilar"
SCAM_REASONS_SHOWN = 2


def scam_warning(*, who: str | None, reasons: list[str] | tuple[str, ...]) -> str:
    """A message looked like a scam; MIYA left it alone."""
    labels = [SCAM_REASON_LABELS[r] for r in reasons if r in SCAM_REASON_LABELS]
    sabab = "; ".join(labels[:SCAM_REASONS_SHOWN]) or SCAM_REASON_FALLBACK
    return SCAM_WARNING.format(ism=_shown(who or UNKNOWN_SENDER), sabab=sabab)


def hijack_warning(*, who: str | None) -> str:
    """A known contact wrote unlike themselves: their account may be taken."""
    return HIJACK_WARNING.format(ism=_shown(who or UNKNOWN_SENDER))


# --- receipts read from images (JV-04) -----------------------------------------

RECEIPT_FOOTER = (
    "Summa rasmdan o'qildi; hisobga faqat bank SMS yoki Payme tasdig'i bilan " "yoziladi."
)
RECEIPT_UNREADABLE = "🧾 Chek o'qilmadi — rasm aniq emas. /tekshir ro'yxatiga qo'ydim."
RECEIPT_HEIC_HINT = "Bu rasm formati (HEIC) o'qilmaydi — JPG qilib yuboring."
RECEIPT_BANK_WAITING = "Bank tasdig'i: ⏳ kutilmoqda"
# Who sent it and where, for a photo the owner sent the bot themselves.
RECEIPT_FROM_OWNER = "siz"
RECEIPT_BOT_CHANNEL = "MIYA bot"
_MISSING = "—"


def _or_missing(value: str | None) -> str:
    return _shown(value) if value else _MISSING


# The bank cross-check (JV-05): the card's last line, edited in place.
RECEIPT_CONFIRMED = "✅ Chek bankda tasdiqlandi: {kanal} {vaqt}."
RECEIPT_UNCONFIRMED = (
    "⏳ Bankdan tasdiq hali yo'q (SMS kelmaydigan hisob bo'lishi mumkin)."
)
RECEIPT_SUSPICIOUS = (
    "⚠️ Chek shubhali: {sabab}. Pul kartangizga tushganini ilovadan tekshiring."
)
RECEIPT_REASON_RESENT = "bu chek avval ham yuborilgan ({sana})"
RECEIPT_REASON_RESENT_UNDATED = "bu chek avval ham yuborilgan"
RECEIPT_REASON_STALE = "chekdagi sana eski ({sana})"
RECEIPT_REASON_FUTURE = "chekdagi sana kelajakda"
RECEIPT_REASON_OWN_CARD = "karta sizniki, lekin bankdan xabar kelmadi"
RECEIPT_OWNER_OK = "✅ Pul keldi — o'zingiz tasdiqladingiz."
RECEIPT_OWNER_FAKE = "🚩 Soxta deb belgiladingiz."


def _when(value: datetime | None) -> str:
    return f"{day_label(value)} {clock(value)}" if value is not None else _MISSING


def _suspicion(receipt, detail) -> str:
    reasons = list(receipt.verdict_reasons or [])
    parts: list[str] = []
    if "duplicate_image" in reasons or "duplicate_reference" in reasons:
        earlier = getattr(detail, "earlier_at", None)
        parts.append(
            RECEIPT_REASON_RESENT.format(sana=day_label(earlier))
            if earlier is not None
            else RECEIPT_REASON_RESENT_UNDATED
        )
    if "stale_date" in reasons:
        parts.append(RECEIPT_REASON_STALE.format(sana=day_label(receipt.paid_at)))
    if "future_date" in reasons:
        parts.append(RECEIPT_REASON_FUTURE)
    if "own_card_no_bank" in reasons:
        parts.append(RECEIPT_REASON_OWN_CARD)
    return RECEIPT_SUSPICIOUS.format(sabab=", ".join(parts) or _MISSING)


def receipt_verdict_line(receipt, detail=None) -> str:
    """The card's bank line for the receipt's verdict (JV-05)."""
    verdict = getattr(receipt, "verdict", None)
    if verdict == "confirmed":
        label = getattr(detail, "bank_label", None)
        return RECEIPT_CONFIRMED.format(
            kanal=_shown(label) if label else _MISSING,
            vaqt=_when(getattr(detail, "bank_at", None)),
        )
    if verdict == "unconfirmed":
        return RECEIPT_UNCONFIRMED
    if verdict == "suspicious":
        return _suspicion(receipt, detail)
    if verdict == "owner_ok":
        return RECEIPT_OWNER_OK
    if verdict == "owner_fake":
        return RECEIPT_OWNER_FAKE
    return RECEIPT_BANK_WAITING


def receipt_card(
    receipt, *, sender: str, channel: str, footer: bool = True, detail=None
) -> str:
    """The owner's receipt card: what the image says, never the bank's word.

    A receipt that could not be read is the one line saying so. ``footer``
    is dropped only where the owner's own words already booked the payment
    (a photo sent to the bot), so the card never claims otherwise. The last
    line is the bank cross-check (JV-05), ``detail`` the
    ``receipts.VerdictDetail`` it names.
    """
    if receipt.state != "read":
        return RECEIPT_UNREADABLE
    amount = (
        money(receipt.amount, receipt.currency)
        if receipt.amount is not None and receipt.currency is not None
        else _MISSING
    )
    when = (
        f"{day_label(receipt.paid_at)} {clock(receipt.paid_at)}"
        if receipt.paid_at is not None
        else _MISSING
    )
    card = f" ****{receipt.payee_card_last4}" if receipt.payee_card_last4 else ""
    lines = [
        f"🧾 Chek · {_shown(sender)} · {_shown(channel)}",
        f"Chekda: {amount} · {when}",
        f"To'lovchi: {_or_missing(receipt.payer_name)} → "
        f"{_or_missing(receipt.payee_name)}{card}",
    ]
    issued = [_shown(receipt.issuer)] if receipt.issuer else []
    if receipt.reference:
        issued.append(f"№ {_shown(receipt.reference)}")
    if issued:
        lines.append(" · ".join(issued))
    lines.append(receipt_verdict_line(receipt, detail))
    if footer:
        lines.append(RECEIPT_FOOTER)
    return "\n".join(lines)


def receipt_card_for(receipt, source, detail=None) -> str:
    """The card from ``receipts.CardSource``: the owner's own image says
    «siz · MIYA bot», a chat receipt names its sender and the group (or
    Telegram for a private chat)."""
    if source.from_bot:
        sender, channel = RECEIPT_FROM_OWNER, RECEIPT_BOT_CHANNEL
    else:
        sender = safety.display_name(source.sender or _MISSING)
        channel = source.chat_title or PRIVATE_CHAT_LABEL
    return receipt_card(
        receipt, sender=sender, channel=channel, footer=source.footer, detail=detail
    )


# --- the Google Sheets mirror (JV-07) --------------------------------------------
#
# Plain cell text, never HTML: the sheet shows it verbatim. Header rows and the
# words the cells use; services/sheets_sync.py fills them from SQL.

SHEETS_TASKS_HEADER = (
    "ID",
    "Tur",
    "Kim",
    "Kimga",
    "Matn",
    "Muddat",
    "Holat",
    "Kanal",
    "Manba vaqti",
    "Kontekst",
)
SHEETS_FOLLOWUPS_HEADER = (
    "ID",
    "Mijoz",
    "Kod",
    "Kanal",
    "Oxirgi xabar",
    "Javobsiz",
    "Ochiq eskalatsiya",
    "Ochiq va'dalar",
    "Keyingi muddat",
)
SHEETS_RECEIPTS_HEADER = (
    "ID",
    "Chekdagi vaqt",
    "Summa",
    "Valyuta",
    "To'lovchi",
    "Qabul qiluvchi karta",
    "Bank/ilova",
    "Chek №",
    "Kimdan",
    "Kanal",
    "Bank tasdig'i",
    "Bank yozuvi",
)
# Tur, Kim / Kimga: the owner is «siz», like the receipt card.
SHEETS_KIND_PROMISE = "va'da"
SHEETS_KIND_TASK = "vazifa"
SHEETS_OWNER = RECEIPT_FROM_OWNER
SHEETS_PROMISE_STATUS = {
    "open": "ochiq",
    "done": "bajarilgan",
    "broken": "buzilgan",
    "cancelled": "yopilgan",
}
SHEETS_TASK_STATUS = {
    "todo": "ochiq",
    "doing": "jarayonda",
    "done": "bajarilgan",
    "dropped": "yopilgan",
}
# Javobsiz: "ha · <when they asked>", else "yo'q".
SHEETS_UNANSWERED_YES = "ha · {vaqt}"
SHEETS_UNANSWERED_NO = "yo'q"
# Ochiq eskalatsiya (JV-22): why the client waits for the owner, since when.
SHEETS_ESCALATION_OPEN = "{sabab} · {vaqt} dan beri"
# Kanal of a receipt: the bot, a group's title, or Telegram for a private chat.
SHEETS_BOT_CHANNEL = RECEIPT_BOT_CHANNEL
SHEETS_PRIVATE_CHANNEL = PRIVATE_CHAT_LABEL
# Bank tasdig'i: the bank cross-check's verdict (JV-05), never a figure.
SHEETS_VERDICT = {
    "waiting": "⏳ kutilmoqda",
    "confirmed": "✅ bankda tasdiqlandi",
    "unconfirmed": "⏳ bankdan tasdiq yo'q",
    "suspicious": "⚠️ shubhali",
    "owner_ok": "✅ pul keldi (o'zingiz tasdiqladingiz)",
    "owner_fake": "🚩 soxta (o'zingiz belgiladingiz)",
}
SHEETS_UNREADABLE = "🧾 chek o'qilmadi"
SHEETS_STATUS_LINE = "📊 Sheets: oxirgi yangilanish {vaqt}"


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


# --- /birlashtir (WP-77) ----------------------------------------------------------

MERGE_USAGE = (
    "Birlashtirish: <code>/birlashtir Акмал &gt; Akmal</code> — birinchisi "
    "ikkinchisiga qo'shiladi."
)
MERGE_REFUSED = (
    "⚠️ Ikkalasining ham alohida Telegram hisobi bor — bular ikki xil odam. "
    "Birlashtirmadim."
)
MERGE_SAME = "Bu bitta odamning o'zi — birlashtiradigan narsa yo'q."
MERGE_STALE = "Bu savol eskirgan — <code>/birlashtir</code> ni qaytadan yozing."
MERGE_CANCELLED = "Bekor qilindi — hech narsa o'zgarmadi."


def merge_preview(source, target, counts: dict) -> str:
    return (
        f"🔗 <b>{escape(source.display_name)}</b> → <b>{escape(target.display_name)}</b> "
        "ga qo'shiladi:\n"
        f"• qarzlar: {counts.get('debts', 0)}\n"
        f"• va'dalar: {counts.get('promises', 0)}\n"
        f"• xarajat/kirim: {counts.get('transactions', 0)}\n"
        f"• xabar va qo'ng'iroqlar: {counts.get('interactions', 0)}\n"
        f"• eslab qolinganlar: {counts.get('memories', 0)}\n"
        f"• kodlar: {counts.get('codes', 0)}\n"
        "Birlashtiraymi?"
    )


def merge_done(target) -> str:
    return f"✅ Birlashtirildi: endi hammasi <b>{escape(target.display_name)}</b> nomida."


def placeholder_merged(code: str, name: str) -> str:
    return (
        f"🏷 <b>{escape(code)}</b> → <b>{escape(name)}</b>. «{escape(code)}» nomli "
        f"vaqtinchalik yozuv {escape(name)} ga qo'shildi."
    )


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
    "shaxsiy": "/shaxsiy {name}",
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
        tail = f" ({record_short_due(promise)})" if promise.due_date else ""
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
        tail = f" ({record_short_due(task)})" if task.due_date else ""
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
        f"{escape(person_label(b.person))}: {money(b.outstanding, b.currency)}"
        + (f" · {relative_day(b.earliest_due)}" if b.earliest_due else "")
        + tags("debt", b.ids)
    )


def promises_report(items) -> str:
    if not items:
        return "✅ Ochiq va'da yo'q."

    mine = [
        timed_promise_line(p, person)
        if getattr(p, "due_time", None) is not None
        else f"{escape(p.description)} — {escape(person_label(person))}"
        + (f" · {relative_day(p.due_date)}" if p.due_date else "")
        + tag("promise", p.id)
        for p, person in items
        if p.made_by is PromiseMadeBy.me
    ]
    theirs = [
        timed_promise_line(p, person)
        if getattr(p, "due_time", None) is not None
        else f"{escape(person_label(person))}: {escape(p.description)}"
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
        new_lines = new_record_lines(summary.new_debts, summary.new_promises)
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
                escape(person_label(b.person)),
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
            + (f" · {record_due(p)}" if p.due_date else "")
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


# --- timed deadlines (JV-03) ------------------------------------------------

# A promise whose deadline names a time of day, in a list.
TIMED_PROMISE_LINE = "⏰ {muddat} — {ism}: {matn}"
# The ping PROMISE_REMIND_LEAD_MINUTES before a timed deadline: the owner's
# own promise, the other side's, and a task.
TIMED_REMINDER_MINE = "⏰ {vaqt} da muddat: {ism}ga va'da — {matn}"
TIMED_REMINDER_THEIRS = "⏰ {vaqt} da muddat: {ism}ning va'dasi — {matn}"
TIMED_REMINDER_TASK = "⏰ {vaqt} da muddat: {matn}"


def timed_promise_line(promise, person) -> str:
    """'⏰ ertaga 14:00 — Akmal: hisob-kitob p12'."""
    return TIMED_PROMISE_LINE.format(
        muddat=record_due(promise),
        ism=escape(person_label(person)),
        matn=escape(promise.description),
    ) + tag("promise", promise.id)


def _due_promise_line(promise, person) -> str:
    """A promise in the reminder and the brief: timed ones lead with ⏰ and
    the time, date-only ones read as they always have."""
    if getattr(promise, "due_time", None) is not None:
        return timed_promise_line(promise, person)
    return (
        f"{escape(person_label(person))}: {escape(promise.description)} · "
        f"{relative_day(promise.due_date)}" + tag("promise", promise.id)
    )


def timed_reminder_line(kind: str, record, person=None) -> str:
    """One ping before a timed deadline, ref last for the buttons."""
    vaqt = record_due(record)
    matn = escape(record.description)
    if kind == "task":
        return TIMED_REMINDER_TASK.format(vaqt=vaqt, matn=matn) + tag("task", record.id)
    template = (
        TIMED_REMINDER_MINE
        if record.made_by is PromiseMadeBy.me
        else TIMED_REMINDER_THEIRS
    )
    name = escape(person_label(person)) if person is not None else "?"
    return template.format(vaqt=vaqt, ism=name, matn=matn) + tag("promise", record.id)


def timed_reminder_with_counts(promises, tasks) -> tuple[str, dict[str, int]]:
    """The before-the-deadline pings in one message, plus how many of each
    kind fit — only what is shown may be marked as sent, as for the hourly
    reminder."""
    entries = [
        ("promise", timed_reminder_line("promise", p, person)) for p, person in promises
    ]
    entries += [("task", timed_reminder_line("task", t)) for t in tasks]
    counts = {"promise": 0, "task": 0}
    lines: list[str] = []
    for kind, line in entries:
        if len("\n".join([*lines, line])) > TELEGRAM_LIMIT - 60:
            break
        lines.append(line)
        counts[kind] += 1
    return "\n".join(lines), counts


def timed_reminder_refs(
    promises, tasks, rendered: dict[str, int]
) -> list[tuple[str, int]]:
    """The rows the shown pings' buttons act on, in line order."""
    refs = [("promise", p.id) for p, _ in promises][: rendered.get("promise", 0)]
    refs += [("task", t.id) for t in tasks][: rendered.get("task", 0)]
    return refs


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
                f"{escape(person_label(b.person))}: {money(b.outstanding, b.currency)} · "
                f"{relative_day(b.earliest_due)}" + tags("debt", b.ids)
                for b in debts
            ],
        ),
        (
            "promise",
            "🤝 <b>Va'da muddati</b>",
            [_due_promise_line(p, person) for p, person in promises],
        ),
        (
            "task",
            "✔️ <b>Vazifalar</b>",
            [
                f"{escape(t.description)} · {record_due(t)}" + tag("task", t.id)
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
    "<code>/tuzat p7 ertaga 14:00</code> — muddat va vaqt (va'da, vazifa)\n"
    "<code>/tuzat c12 summa 4 mln</code> — tasdiqlanmagan da'voni, javobdan oldin\n\n"
    "<i>Aniq bo'lmasa: «kim Sardor», «summa 5 mln», «muddat juma».</i>"
)


# What the ✏️ button offers, per kind: only what `set_field` accepts for
# that row (records.EDITABLE), so the hint never teaches a refused edit.
_TUZAT_EXAMPLES = {
    "debt": ("6 mln", "300 $", "teskari", "Sardor", "ertaga"),
    "promise": ("Sardor", "ertaga", "ertaga 14:00"),
    "task": ("ertaga", "ertaga 14:00"),
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
    "<code>/tuzat {ref} ichki</code> — o'z kartalar orasida · "
    "<code>/tuzat {ref} tashqi</code> — oddiy to'lov · "
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
    "internal": "o'z kartalar orasida",
}

FIELD_NOT_EDITABLE = {
    "debt": "Qarzda bunday maydon yo'q.",
    "promise": "Va'dada faqat odam va muddatni tuzatish mumkin.",
    "task": "Vazifada faqat muddatni tuzatish mumkin.",
    "transaction": (
        "Pul harakatida faqat summa, valyuta, tomon (kirim/chiqim), odam, sana, "
        "izoh, turkum va ichki/tashqini tuzatish mumkin."
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


MONEY_GS_UNLINKED = "🏷 {code} — bu kod hali hech kimga bog'lanmagan"


def money_internal_receipt(txn, pair) -> str:
    """WP-69: a transfer between the owner's own cards, kept out of the totals."""
    expense, income = (txn, pair) if txn.type.value == "expense" else (pair, txn)
    first, second = sorted((txn.id, pair.id))
    return (
        f"🔁 O'z kartalaringiz orasida: *{expense.card_last4 or '?'} → "
        f"*{income.card_last4 or '?'}, {money(txn.amount, txn.currency)} — "
        f"kirim/chiqimga qo'shilmadi. <code>x{first}</code>, <code>x{second}</code>"
    )


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
    gs = [
        str(c)
        for c in (((interaction.media or {}).get("money") or {}).get("gs_codes") or [])
    ]
    if person is not None:
        tag = f" ({escape(gs[0])})" if len(gs) == 1 else ""
        parts.append(f"👤 {escape(person.display_name)}{tag}")
    elif len(gs) == 1:
        parts.append(MONEY_GS_UNLINKED.format(code=escape(gs[0])))
    elif gs:
        parts.append("🏷 " + ", ".join(escape(c) for c in gs))
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


def claim_promise_kept(promise) -> str:
    """ "✅ p7": the promise the owner picked is closed (WP-71)."""
    return (
        f"{CLAIM_ACCEPTED_PREFIX} <code>{_ref_handle('promise', promise.id)}</code> "
        f"{escape(promise.description)} — bajarildi."
    )


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


QUESTION_REPLY_DONE = "✅ Javoblar qabul qilindi: {list}"
QUESTION_REPLY_PARTIAL = "⚠️ {n}-savolni tushunmadim — tugma bilan javob ber."
MANBA_USAGE = "Qaysi yozuv? Masalan: <code>/manba m1234</code>"
MANBA_NOT_FOUND = "Bunday yozuv topilmadi — o'chirilgan bo'lishi mumkin."
QIDIR_USAGE = "Nima qidiray? <code>/qidir bojxona</code>"
MANBA_EDIT_LINE = "✏️ tahrirlangan {when}; avvalgi matn: «{text}»"
QIDIR_HIT_CHARS = 140
QIDIR_FACT_CHARS = 160


def qidir_header(query: str, count: int) -> str:
    return f"🔍 <b>{escape(query)}</b> — {count} ta topildi"


def qidir_empty(query: str) -> str:
    return f"🔎 <b>{escape(query)}</b> bo'yicha yozuvlarda hech narsa topilmadi."


def _day(value: datetime) -> str:
    return short_date(value.astimezone(settings.tz).date())


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def qidir_results(result, query: str) -> str:
    """`/qidir`: the recall hits (WP-59) — where, who, the words, the ref;
    no model involved."""
    lines = []
    for episode in result.episodes:
        for line in episode.lines:
            if not line.hit:
                continue
            lines.append(
                f"• {_day(line.when)} · {escape(episode.where)} · "
                f"{escape(line.who)}: «{escape(_one_line(line.text, QIDIR_HIT_CHARS))}» "
                f"<code>{escape(line.ref)}</code>"
            )
    for fact in result.facts:
        lines.append(
            f"• {_day(fact.when)} · 💡 "
            f"{escape(_one_line(fact.text, QIDIR_FACT_CHARS))} "
            f"<code>{escape(fact.ref)}</code>"
        )
    if not lines:
        return qidir_empty(query)
    return clip("\n".join([qidir_header(query, len(lines)), *lines]))


def manba(source) -> str:
    """`/manba`: the original words behind a citation, escaped and clipped."""
    when = source.when
    if source.kind == "fact":
        header = f"💡 <b>Xotira</b> · {_day(when)}"
        if source.about:
            header += f" · {escape(source.about)}"
        parts = [header, escape(source.text)]
        if source.source_ref:
            parts.append(f"Manba: /manba {source.source_ref}")
        return clip("\n\n".join(parts))
    header = (
        f"📄 <b>Asl yozuv</b> · {_day(when)} {clock(when)}"
        f" · {escape(source.where or '')}"
    )
    if source.lines:
        body = "\n".join(
            (
                f"<b>{clock(line.when)} {escape(line.who)}: {escape(line.text)}</b>"
                if line.hit
                else f"{clock(line.when)} {escape(line.who)}: {escape(line.text)}"
            )
            for line in source.lines
        )
    else:
        body = escape(source.text)
    edits = [
        MANBA_EDIT_LINE.format(
            when=(f"{_day(at)} {clock(at)}" if at is not None else "?"),
            text=escape(_one_line(old, QIDIR_HIT_CHARS)),
        )
        for at, old in getattr(source, "edits", [])
    ]
    if edits:
        body += "\n\n" + "\n".join(edits)
    return clip(f"{header}\n\n{body}")


SEARCH_UNAVAILABLE = (
    "⚠️ Qidiruv hozircha ishlamayapti (embedding xizmati tayyor emas) — "
    "birozdan keyin qayta urinib ko'ring."
)


OPERATION_LABEL = {
    "recap": "kunlik xulosa (AI)",
    "rag_opinion": "fikr so'rovlari",
    "extract": "xabarlardan ajratish",
    "extract_window": "telegram suhbatlari (batch)",
    "extract_window_instant": "telegram suhbatlari (tezkor)",
    "extract_window_fallback": "telegram suhbatlari (qayta)",
    "transcribe": "ovozni matnga o'girish",
    "vision": "rasmlarni o'qish",
    "receipt_vision": "cheklarni o'qish",
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
    "claims": "da'vo",
    "receipts": "chek",
    "group_lines": "guruhlardagi xabari",
    "person_card": "odam kartasi (ism, telefon, taxalluslar)",
    "reports": "kunlik xulosa",
    "digests": "AI xulosa",
    "outbox": "Jarvis xabari (qoralama yoki yuborilgan)",
    "escalations": "Jarvis eskalatsiyasi",
    "style_examples": "uslub misoli",
    "knowledge_entries": "bilimlar bazasi yozuvi",
}
PURGE_REPORTS_NOTE = (
    "<i>O'sha kunlarning saqlangan kunlik xulosalari butunlay o'chadi (boshqa "
    "odamlar haqidagi qatorlari bilan birga); pul va qarz yozuvlari o'z joyida "
    "qoladi.</i>"
)
_KEPT_LABEL = {
    "transactions_kept": "pul harakati: {n} ta — summa qoladi, ism olib tashlanadi",
    "mentions_kept": "boshqa yozuvlarda tilga olingan: {n} ta",
    "payments_in_range": (
        "shu kunlardagi qarz to'lovlari: {n} ta — qarz qoldig'i buzilmasligi uchun"
    ),
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
    kept = [
        _KEPT_LABEL[key].format(n=count)
        for key, count in (getattr(plan, "kept", None) or {}).items()
        if count and key in _KEPT_LABEL
    ]
    kept_block = (
        "\n\n<b>Qoladi (o'chirilmaydi):</b>\n" + bullet_list(kept, empty="—")
        if kept
        else ""
    )

    return clip(
        f"⚠️ <b>O'chirishni tasdiqlang</b>\n"
        f"{_PURGE_KIND.get(plan.kind, plan.kind)}: <b>{escape(plan.label)}</b>\n\n"
        + bullet_list(lines, empty="—")
        + kept_block
        + (f"\n\n{PURGE_REPORTS_NOTE}" if plan.counts.get("reports") else "")
        + "\n\n<i>Bu amalni ortga qaytarib bo'lmaydi.</i>"
    )


def purge_done(plan, result) -> str:
    tail = f", {result.files_deleted} ta fayl" if result.files_deleted else ""
    if plan.kind == "person":
        return (
            f"🗑 <b>{escape(plan.label)}</b> o'chirildi: {result.interactions} ta yozuv, "
            f"{result.memories} ta xotira, {result.claims} ta da'vo{tail}."
        )
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
# JV-08: today's deadlines that name a time of day, by that time, right after
# the meetings; the due section below leaves them out.
BRIEF_TIMED = "⏰ Bugungi muddatlar:"
BRIEF_TIMED_LINE = "{vaqt} — {ism}: {matn}"
# A task has no counterparty, so its line has no name.
BRIEF_TIMED_TASK_LINE = "{vaqt} — {matn}"
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
    system = _brief_system_line(brief)
    unsafe = _brief_unsafe_line(brief)
    if brief.is_empty():
        # Yesterday's Jarvis activity alone is not a reason to say more.
        tail = [line for line in (_brief_jarvis(brief), unsafe, system) if line]
        return "\n\n".join([*parts, BRIEF_ALL_CLEAR, *tail])

    if brief.events:
        lines = [
            f"{clock(e.start_at)} — {escape(e.title)}"
            + (f" ({escape(e.location)})" if e.location else "")
            for e in brief.events
        ]
        parts.append(f"{BRIEF_EVENTS}\n" + bullet_list(lines, empty="—"))

    timed = _brief_timed(brief)
    if timed:
        parts.append("\n".join([BRIEF_TIMED, *(_timed_line(*item) for item in timed)]))
    said = {(kind, record.id) for kind, record, _ in timed}

    due_lines = [
        f"{escape(person_label(b.person))}: {money(b.outstanding, b.currency)} · "
        f"{relative_day(b.earliest_due)}" + tags("debt", b.ids)
        for b in brief.due.get("debts", [])
    ]
    due_lines += [
        _due_promise_line(p, person)
        for p, person in brief.due.get("promises", [])
        if ("promise", p.id) not in said
    ]
    due_lines += [
        f"{escape(t.description)} · {record_due(t)}" + tag("task", t.id)
        for t in brief.due.get("tasks", [])
        if ("task", t.id) not in said
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
    jarvis = _brief_jarvis(brief)
    if jarvis:
        parts.append(jarvis)
    if unsafe:
        parts.append(unsafe)
    line = queue_line(getattr(brief, "queue", None))
    if line:
        parts.append(line)
    if system:
        parts.append(system)

    return "\n\n".join(parts)


def _brief_jarvis(brief: MorningBrief) -> str | None:
    """The Jarvis section (JV-18, JV-22); None with Jarvis off, when its
    reads failed, or when there is nothing to say."""
    lines = jarvis_brief_lines(
        getattr(brief, "jarvis", None), getattr(brief, "jarvis_day", None)
    )
    return "\n".join(lines) if lines else None


def _brief_unsafe_line(brief: MorningBrief) -> str | None:
    """Yesterday's spam and scam, counted (JV-09); read defensively like the
    brief's other optional fields."""
    count = getattr(brief, "unsafe_ignored", 0) or 0
    return UNSAFE_IGNORED_LINE.format(n=count) if count else None


def _brief_timed(brief: MorningBrief) -> list:
    """The brief's timed deadlines, read defensively like its other optional
    fields: a brief built without them has none."""
    return list(getattr(brief, "timed", None) or [])


def _timed_line(kind: str, record, person) -> str:
    """'14:00 — Akmal: hisob-kitob p12'. The name and the text come from a
    counterparty's words, so both are escaped and stripped of bidi marks."""
    vaqt = record.due_time.strftime("%H:%M")
    matn = _shown(record.description or "")
    if kind == "task" or person is None:
        return BRIEF_TIMED_TASK_LINE.format(vaqt=vaqt, matn=matn) + tag(kind, record.id)
    return BRIEF_TIMED_LINE.format(
        vaqt=vaqt, ism=_shown(person_label(person)), matn=matn
    ) + tag(kind, record.id)


def _brief_system_line(brief: MorningBrief) -> str | None:
    """One line on MIYA itself (WP-80); the evening footer names the rest."""
    count = getattr(brief, "system_problems", None)
    if count is None:
        return None
    return health.BRIEF_PROBLEMS.format(n=count) if count else health.BRIEF_OK


def morning_brief_refs(
    brief: MorningBrief, parts: list[str] | None = None
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """``(due, stale)`` — the rows the brief's buttons act on, in line order."""
    # Line order: today's timed deadlines come first, then the due section.
    due: list[tuple[str, int]] = [(kind, r.id) for kind, r, _ in _brief_timed(brief)]
    for b in brief.due.get("debts", []):
        due += [("debt", i) for i in b.ids]
    due += [
        ("promise", p.id)
        for p, _ in brief.due.get("promises", [])
        if ("promise", p.id) not in due
    ]
    due += [
        ("task", t.id) for t in brief.due.get("tasks", []) if ("task", t.id) not in due
    ]
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


def _drill_line(status: health.Status) -> str | None:
    """The last weekly restore drill (WP-81); nothing before the first."""
    drill = getattr(status, "restore_drill", None)
    if drill is None or drill.last_seen_at is None:
        return None
    detail = drill.detail or {}
    day = short_date(drill.last_seen_at.astimezone(settings.tz).date())
    if detail.get("skipped"):
        why = escape(str(detail["skipped"]))
        return f"⏸ Tiklash sinovi — {day}: o'tkazib yuborildi ({why})"
    if detail.get("ok") is False:
        return f"❌ Tiklash sinovi — {day}: muvaffaqiyatsiz"
    counts = detail.get("counts") or {}
    return (
        f"✅ Tiklash sinovi — {day}: zaxira ochildi, {counts.get('debts', 0)} ta qarz, "
        f"{counts.get('transactions', 0)} ta tranzaksiya"
    )


def _sheets_line(status: health.Status) -> str | None:
    """The Google Sheets mirror (JV-07): when it last caught up with SQL.
    No line while the mirror is switched off."""
    if not settings.sheets_enabled:
        return None
    sheets = getattr(status, "sheets", None)
    last = health.sheets_last_ok_at(sheets) if sheets is not None else None
    return SHEETS_STATUS_LINE.format(vaqt=_since_when(last, status.now))


def _phone_line(status: health.Status) -> str | None:
    """The phone app: last contact, last new event, and any stream it wants
    but cannot use (WP-66).

    None (no line at all) when no phone has ever been in touch: a phone-less
    install is healthy and should not read as missing something.
    """
    last = health.last_phone_contact(status)
    if last is None:
        return None
    phone = getattr(status, "phone", None)
    event = _ago(phone.age) if phone is not None else STATUS_NEVER
    silent = health.phone_silent_for(status)
    if silent is not None:
        return (
            f"⚠️ Telefon — {age_label(silent)}dan beri jim · "
            f"oxirgi yangi voqea: {event}"
        )
    revoked, never = health.phone_streams(status)
    tail = ""
    if revoked:
        tail += " · o'chgan: " + ", ".join(health.STREAM_LABEL[k] for k in revoked)
    if never:
        tail += " · ruxsat berilmagan: " + ", ".join(
            health.STREAM_LABEL[k] for k in never
        )
    contact = _ago(max(status.now - last, timedelta(0)))
    return f"📱 Telefon — oxirgi aloqa: {contact} · oxirgi yangi voqea: {event}{tail}"


# --- Jarvis in /holat (JV-21) ----------------------------------------------------
# Only with JARVIS_ENABLED: off, /holat has none of these lines (rule 20). The
# values come from health.JarvisHealth, plain, never from miya.jarvis.

JARVIS_HEALTH_LINE = "🤖 Jarvis: {rejim} · bugun {y}/{cap} yuborildi · navbatda {q}"
JARVIS_HEALTH_PAUSED = "{rejim}, to'xtatilgan"
JARVIS_HEALTH_TODAY = (
    "🧾 Jarvis bugun: {d} ta qoralama · {e} ta eskalatsiya · xarajat {x} / {cap}"
)
JARVIS_HEALTH_CAPPED = " — chegaraga yetdi"
JARVIS_HEALTH_UNPRICED = " · narxi noma'lum: {n} ta chaqiruv"
JARVIS_HEALTH_UNREADABLE = (
    "⚠️ Jarvis — holatini o'qib bo'lmadi (<code>make bot</code> logini ko'ring)"
)
JARVIS_SEND_LINE = "📤 {kanal} yuborish: {holat}"
JARVIS_SEND_WORKING = "✅ ishlayapti"
JARVIS_SEND_LIMITED = "⛔ cheklangan ({gacha})"
JARVIS_SEND_OFF = "⏸ o'chirilgan ({kalit}=false)"
JARVIS_SEND_PAUSED = "⏸ to'xtatilgan (/jarvis yoq)"
JARVIS_SEND_WAITING = "⏳ Telegram kutishni so'radi ({gacha})"
JARVIS_SEND_DOWN = "⚠️ yuboruvchi ishlamayapti ({sabab})"
JARVIS_SEND_LAST = " · oxirgi yuborish: {vaqt}"
JARVIS_UNTIL = "{vaqt} gacha"
JARVIS_UNTIL_LIFTED = "/jarvis yoq {kanal} bilan yoqilguncha"
# A breaker this far ahead has no end Telegram named: it lasts until lifted.
JARVIS_UNTIL_LIFTED_AFTER = timedelta(days=365)
# The sender's own state on the userbot beat (JV-19) in the owner's words.
JARVIS_SENDER_STATES = {
    "restarting": "qayta ishga tushirilmoqda",
    "stopped": "to'xtagan",
    "not_started": "ishga tushmadi",
}
JARVIS_SENDER_NEVER = "hali ishga tushmagan"
JARVIS_SENDER_USERBOT_DOWN = "Telegram o'quvchi jim"
JARVIS_LEASES_STUCK = "⚠️ Jarvis: {n} ta xabar yuborilish holatida qotib qoldi"
JARVIS_UNKNOWN_LINE = (
    "❓ Jarvis: {n} ta xabar yuborilganmi — aniq emas (24 soat ichida). "
    "Chatni tekshiring."
)
JARVIS_RESPONDER_OK = "✅ Javob beruvchi — oxirgi ishga tushish: {vaqt}"
JARVIS_RESPONDER_NEVER = "⚠️ Javob beruvchi — hali ishga tushmagan"
JARVIS_RESPONDER_SILENT = "❌ Javob beruvchi — {vaqt}dan beri ishlamagan"
JARVIS_RESPONDER_FAILED = "❌ Javob beruvchi — xato: <code>{xato}</code>"
# The channels' names in the send lines (JARVIS_CHANNEL_LABELS is defined with
# the cards, further down; the send switch names the .env key to change).
JARVIS_SEND_SWITCH_KEYS = {
    "telegram": "TELEGRAM_SEND_ENABLED",
    "whatsapp": "WHATSAPP_SEND_ENABLED",
    "gmail": "GMAIL_SEND_ENABLED",
}


def _until_label(until: datetime, now: datetime, channel: str) -> str:
    """'bugun 18:30 gacha' / 'ertaga 09:00 gacha' / '/jarvis yoq telegram
    bilan yoqilguncha' for a breaker with no end Telegram named."""
    if until - now > JARVIS_UNTIL_LIFTED_AFTER:
        return JARVIS_UNTIL_LIFTED.format(kanal=channel)
    local = until.astimezone(settings.tz)
    days = (local.date() - now.astimezone(settings.tz).date()).days
    if days == 0:
        day = "bugun"
    elif days == 1:
        day = "ertaga"
    else:
        day = short_date(local.date())
    return JARVIS_UNTIL.format(vaqt=f"{day} {clock(local)}")


def _parsed_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=settings.tz)


def _telegram_send_state(status: health.Status, channel: health.ChannelSend) -> str:
    """The Telegram send line's state: the switch, the breaker, the owner's
    pause, the sender's own wait and its loop, from the userbot beat."""
    if not channel.send_enabled:
        return JARVIS_SEND_OFF.format(kalit=JARVIS_SEND_SWITCH_KEYS["telegram"])
    if channel.limited and channel.breaker_until is not None:
        return JARVIS_SEND_LIMITED.format(
            gacha=_until_label(channel.breaker_until, status.now, "telegram")
        )
    if channel.paused:
        return JARVIS_SEND_PAUSED
    userbot = status.components.get("userbot")
    detail = (userbot.detail if userbot is not None else None) or {}
    if userbot is None or userbot.stale or userbot.disabled:
        return JARVIS_SEND_DOWN.format(sabab=JARVIS_SENDER_USERBOT_DOWN)
    state = detail.get("sender")
    if state != "running":
        sabab = JARVIS_SENDER_STATES.get(str(state), JARVIS_SENDER_NEVER)
        error = detail.get("sender_error")
        if state in JARVIS_SENDER_STATES and error:
            sabab += f": <code>{escape(str(error))}</code>"
        return JARVIS_SEND_DOWN.format(sabab=sabab)
    paused_until = _parsed_at(detail.get("sender_paused_until"))
    if paused_until is not None and paused_until > status.now:
        return JARVIS_SEND_WAITING.format(
            gacha=_until_label(paused_until, status.now, "telegram")
        )
    line = JARVIS_SEND_WORKING
    sent_at = _parsed_at(detail.get("sender_sent_at"))
    if sent_at is not None:
        line += JARVIS_SEND_LAST.format(vaqt=_since_when(sent_at, status.now))
    return line


def _jarvis_send_lines(status: health.Status, jarvis: health.JarvisHealth) -> list[str]:
    """One line for Telegram, whose sender exists (JV-19); another channel
    only while its breaker is in force (its adapter lands with JV-27/JV-30)."""
    lines: list[str] = []
    for channel in jarvis.channels:
        name = JARVIS_CHANNEL_LABELS.get(channel.channel, channel.channel)
        if channel.channel == "telegram":
            holat = _telegram_send_state(status, channel)
        elif channel.limited and channel.breaker_until is not None:
            holat = JARVIS_SEND_LIMITED.format(
                gacha=_until_label(channel.breaker_until, status.now, channel.channel)
            )
        else:
            continue
        lines.append(JARVIS_SEND_LINE.format(kanal=name, holat=holat))
    return lines


def _jarvis_responder_line(status: health.Status) -> str:
    """The responder job's last run, from its ``job:`` heartbeat."""
    job = status.jobs.get(health.JARVIS_RESPOND_JOB)
    if job is None or job.age is None:
        return JARVIS_RESPONDER_NEVER
    if job.detail.get("ok") is False:
        error = str(job.detail.get("error") or "?")[:120]
        return JARVIS_RESPONDER_FAILED.format(xato=escape(error))
    if job.stale:
        return JARVIS_RESPONDER_SILENT.format(vaqt=age_label(job.age))
    return JARVIS_RESPONDER_OK.format(vaqt=_ago(job.age))


def jarvis_status_lines(status: health.Status) -> list[str] | None:
    """Jarvis's lines in /holat (JV-21); None while JARVIS_ENABLED is off.

    The mode with today's capped count against the mode's daily cap (auto
    sends in auto mode, else draft cards, as /jarvis shows it) and the send
    queue, today's drafts, escalations and spend against
    JARVIS_DAILY_COST_CAP_USD, each channel's sending, sends stuck in a
    claim, sends in doubt, and the responder's last run. A stuck queue, the
    Telegram limit and the cost cap are also problems, listed at the end.
    """
    if not settings.jarvis_enabled:
        return None
    jarvis = status.jarvis
    if jarvis is None:
        return [JARVIS_HEALTH_UNREADABLE] if status.jarvis_unreadable else None
    rejim = jarvis_mode_label(jarvis.mode).split(" (")[0]
    if jarvis.paused:
        rejim = JARVIS_HEALTH_PAUSED.format(rejim=rejim)
    today = JARVIS_HEALTH_TODAY.format(
        d=jarvis.drafts,
        e=jarvis.escalations,
        x=usd(jarvis.cost_usd),
        cap=usd(jarvis.cost_cap_usd),
    )
    if jarvis.cost_unpriced:
        today += JARVIS_HEALTH_UNPRICED.format(n=jarvis.cost_unpriced)
    if jarvis.cost_capped:
        today += JARVIS_HEALTH_CAPPED
    lines = [
        JARVIS_HEALTH_LINE.format(
            rejim=rejim, y=jarvis.daily_used, cap=jarvis.daily_cap, q=jarvis.queued
        ),
        today,
        *_jarvis_send_lines(status, jarvis),
    ]
    if jarvis.leases_stuck:
        lines.append(JARVIS_LEASES_STUCK.format(n=jarvis.leases_stuck))
    if jarvis.unknown:
        lines.append(JARVIS_UNKNOWN_LINE.format(n=jarvis.unknown))
    lines.append(_jarvis_responder_line(status))
    return lines


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
        *([line] if (line := _drill_line(status)) else []),
        *([line] if (line := _sheets_line(status)) else []),
        *(jarvis_status_lines(status) or []),
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


# --- questions and opinions (WP-58) ---------------------------------------------

SAVOL_USAGE = "Savolni yozing: <code>/savol Akmal qachon keladi</code>"
FIKR_USAGE = "Nima haqida? <code>/fikr Akmal bilan konteyner masalasi</code>"
SAVED_AS_NOTE = "📝 Eslatma sifatida yozib qo'ydim:"
ALREADY_SAVED = "Bu allaqachon yozib qo'yilgan."
QUESTION_GONE = "Bu xabar topilmadi — o'chirilgan bo'lishi mumkin."
VOICE_QUESTION_CHARS = 300


def voice_question_header(text: str) -> str:
    """What was heard, so a misheard question is visible (WP-60)."""
    heard = " ".join(text.split())
    if len(heard) > VOICE_QUESTION_CHARS:
        heard = heard[: VOICE_QUESTION_CHARS - 1] + "…"
    return f"🎙 <i>Savolingiz:</i> «{escape(heard)}»"


# --- one day's promises and writers, straight from SQL (JV-06) ---------------------

PROMISES_MADE_HEADER = "📌 {kun} bergan va'dalaringiz ({n}):"
PROMISES_MADE_LINE = "{i}. {ism} — {matn} · muddat: {muddat} · {kanal}"
PROMISES_MADE_NONE = "{kun} hech kimga va'da bermagansiz (yozuvlarda topilmadi)."
WHO_WROTE_HEADER = "📨 {kun} sizga yozganlar ({n}):"
WHO_WROTE_LINE = "{i}. {ism} — {soni} ta xabar, oxirgisi {vaqt} · {javob}"
WHO_WROTE_NONE = "{kun} sizga hech kim yozmagan (yozuvlarda topilmadi)."
WHO_WROTE_ANSWERED = "javob berilgan"
WHO_WROTE_UNANSWERED = "javobsiz"
OWNER_DAY_MORE = "<i>… va yana {k} ta</i>"
OWNER_DAY_TODAY = "Bugun"
OWNER_DAY_YESTERDAY = "Kecha"
OWNER_DAY_OTHER = "{hafta_kuni} kuni ({sana})"
UNKNOWN_SENDER = "Nomaʼlum"


def owner_day(day, days_ago: int) -> str:
    """The day a question asked about, as the answer's first word."""
    if days_ago == 0:
        return OWNER_DAY_TODAY
    if days_ago == 1:
        return OWNER_DAY_YESTERDAY
    return OWNER_DAY_OTHER.format(
        hafta_kuni=WEEKDAYS_UZ[day.weekday()].capitalize(), sana=short_date(day)
    )


def _promise_channel(item) -> str:
    """Where a promise was made: the group's title, «Telegram» for a private
    chat, «MIYA bot» for a note to the bot, else the source's own word."""
    if item.source is InteractionSource.telegram_userbot:
        if item.chat_type is ChatType.group and item.chat_title:
            return _shown(item.chat_title)
        return PRIVATE_CHAT_LABEL
    if item.source in (InteractionSource.assistant_bot, InteractionSource.manual):
        return RECEIPT_BOT_CHANNEL
    if item.source is None:
        return _MISSING
    return SOURCE_WORD.get(item.source, item.source.value)


# Room a day report leaves under the Telegram limit for the «… va yana» tail
# and the heard-question header a spoken question puts above it.
_OWNER_DAY_ROOM = 400


def _owner_day_report(header: str, lines: list[str], total: int, *, newest: bool) -> str:
    """``header`` and the ``lines`` that fit, and a «… va yana {k} ta» line
    for every row of ``total`` not shown: cut by the query's limit or by the
    message size. ``newest`` keeps the latest lines when they do not all fit
    and puts that line first (who wrote), else keeps the earliest and puts
    it last (promises made)."""
    budget = TELEGRAM_LIMIT - len(header) - _OWNER_DAY_ROOM
    if newest:
        shown = _fit_oldest_first(list(reversed(lines)), budget=budget)
    else:
        shown, used = [], 0
        for line in lines:
            used += len(line) + 1
            if used > budget:
                break
            shown.append(line)
    hidden = total - len(shown)
    if hidden <= 0:
        return clip("\n".join([header, *shown]))
    more = OWNER_DAY_MORE.format(k=hidden)
    # The tail sits where the left-out rows would be.
    rows = [more, *shown] if newest else [*shown, more]
    return clip("\n".join([header, *rows]))


def promises_made_report(
    items, *, day, days_ago: int, today=None, total: int | None = None
) -> str:
    """«Bugun kimga nima va'da bergandim?», answered from queries.promises_made.
    ``total`` is how many promises there are in all (default: ``items``)."""
    kun = owner_day(day, days_ago)
    total = len(items) if total is None else total
    if not items:
        return PROMISES_MADE_NONE.format(kun=kun)
    lines = []
    for index, item in enumerate(items, start=1):
        promise = item.promise
        due = record_due(promise, today=today) if promise.due_date else short_date(None)
        lines.append(
            PROMISES_MADE_LINE.format(
                i=index,
                ism=_shown(person_label(item.person)),
                matn=_shown(promise.description),
                muddat=due,
                kanal=_promise_channel(item),
            )
        )
    header = PROMISES_MADE_HEADER.format(kun=kun, n=total)
    return _owner_day_report(header, lines, total, newest=False)


def _writer_name(item) -> str:
    if item.person is not None:
        return _shown(person_label(item.person))
    return _shown(item.chat_title) if item.chat_title else UNKNOWN_SENDER


def who_wrote_report(items, *, day, days_ago: int, total: int | None = None) -> str:
    """«Kecha kimlar yozdi?», answered from queries.who_wrote. ``total`` is
    how many people wrote in all (default: ``items``)."""
    kun = owner_day(day, days_ago)
    total = len(items) if total is None else total
    if not items:
        return WHO_WROTE_NONE.format(kun=kun)
    lines = []
    # The query keeps the latest writers: number them as in the whole day.
    for index, item in enumerate(items, start=total - len(items) + 1):
        lines.append(
            WHO_WROTE_LINE.format(
                i=index,
                ism=_writer_name(item),
                soni=item.messages,
                vaqt=clock(item.last_at),
                javob=WHO_WROTE_ANSWERED if item.answered else WHO_WROTE_UNANSWERED,
            )
        )
    header = WHO_WROTE_HEADER.format(kun=kun, n=total)
    return _owner_day_report(header, lines, total, newest=True)


# --- /jarvis (JV-11) ----------------------------------------------------------------
#
# The values come from miya.jarvis.status, passed in plainly: this module is
# imported at startup and must not import miya.jarvis (rule 20).

JARVIS_MODE_LABELS = {
    "off": "o'chiq",
    "shadow": "kuzatuv (faqat yozib boradi)",
    "draft": "qoralama (har javobni siz tasdiqlaysiz)",
    "auto": "avtomatik",
}
JARVIS_CHANNEL_STATES = {
    "working": "ishlayapti",
    "limited": "cheklangan",
    "paused": "to'xtatilgan",
    "not_connected": "ulanmagan",
}
JARVIS_STATUS = (
    "🤖 Jarvis: {rejim}\n"
    "Bugun: {q} ta qoralama · {y} ta yuborildi · {e} ta eskalatsiya\n"
    "Limit: {used}/{cap}\n"
    "Kanallar: Telegram {telegram} · WhatsApp {whatsapp} · Gmail {gmail}"
)
JARVIS_PAUSED = (
    "⛔ Jarvis to'xtatildi. Hech qanday xabar yuborilmaydi. Qayta yoqish: /jarvis yoq"
)
JARVIS_RESUMED = "✅ Jarvis yoqildi ({rejim} rejimi)."
JARVIS_USAGE = (
    "<code>/jarvis</code> — holat · <code>/jarvis toxtat</code> — hammasini "
    "to'xtatish · <code>/jarvis yoq [kanal]</code> — qayta yoqish · "
    "<code>/jarvis tarix</code> — oxirgi 20 ta amal · "
    "<code>/jarvis rejim</code> — har javob turining rejimi"
)
JARVIS_UNKNOWN_CHANNEL = "Kanal nomi: telegram, whatsapp yoki gmail."
JARVIS_FAILED = (
    "⚠️ Jarvis buyrug'ini bajarib bo'lmadi — hech narsa o'zgarmadi. "
    "Birozdan keyin qayta urinib ko'ring."
)
JARVIS_HISTORY_HEADER = "🧾 Jarvis tarixi (oxirgi {n} ta):"
JARVIS_HISTORY_EMPTY = "🧾 Jarvis tarixi hali bo'sh."
# `/jarvis tarix` speaks Uzbek: the stored history keys and values are
# rendered through these, and only a value with no label is shown as stored.
JARVIS_FIELD_LABELS = {
    "state": "holat",
    "pause": "to'xtatish",
    "breaker": "cheklov",
    "mode": "rejim",
    "guard": "tekshiruv",
    "hold": "kutish",
    "owner_told": "sizga aytildi",
    "card": "karta",
    "alert": "ogohlantirish",
    "snooze": "eslatish",
    "body": "matn",
    "authored_by": "muallif",
    "person_id": "shaxs",
    "warned_personal": "shaxsiy chat ogohlantirishi",
    "warned_safety": "xavf ogohlantirishi",
    "reason": "sabab",
    "linked": "bog'langan belgi",
    "repeat": "yana yozdi",
    "approved_by": "tasdiqlovchi",
    "offer": "avtomatikka o'tish taklifi",
    "incident": "hodisa",
    "demotion_told": "qoralamaga qaytgani aytildi",
    "auto_notice": "avtomatik yuborilgani aytildi",
    "mistake": "«Bu xato edi»",
    "corpus_candidate": "qizil chiziq uchun ko'rib chiqish",
    "question": "savol",
    "question_proposed": "taklif qilingan savol",
}
JARVIS_STATE_LABELS = {
    "shadow": "kuzatuv",
    "draft": "qoralama",
    "approved": "tasdiqlandi",
    "sending": "yuborilmoqda",
    "sent": "yuborildi",
    "failed": "yuborilmadi",
    "unknown": "aniq emas",
    "cancelled": "bekor qilindi",
    "expired": "muddati o'tdi",
    "blocked": "bloklandi",
    "active": "faol",
    "proposed": "taklif",
    "revoked": "bekor qilindi",
    "open": "ochiq",
    "snoozed": "keyinga qoldirildi",
    "owner_replying": "siz javob yozyapsiz",
    "resolved": "hal qilindi",
}
JARVIS_BY_LABELS = {
    "button": "siz",
    "reply": "siz (javob)",
    "command": "buyruq",
    "auto:own": "o'zingiz yozdingiz",
    "auto:policy": "avtomatik",
    "auto:expiry": "muddat",
    "auto:spam": "spam",
    "rule:group": "guruh qoidasi",
    "gate": "tekshiruv",
    "adapter": "yuboruvchi",
    "reconcile": "qayta tekshiruv",
    "migration": "yangilanish",
}
JARVIS_PAUSE_REASON_LABELS = {"owner": "siz"}
# A reply rule's mode and reason (JV-12) in `/jarvis tarix`.
JARVIS_RULE_MODE_LABELS = {"never": "hech qachon"}
JARVIS_RULE_REASON_LABELS = {
    "personal": "shaxsiy",
    "group": "guruh",
    "spam": "spam",
    "bot": "bot",
    "owner": "siz",
}
JARVIS_SCOPE_LABELS = {
    "global": "Jarvis",
    "channel:telegram": "Telegram",
    "channel:whatsapp": "WhatsApp",
    "channel:gmail": "Gmail",
    "question_bank": "Savollar banki",
}
# A send whose outcome is not known (JV-19 wording): never sent again.
JARVIS_SEND_UNKNOWN = (
    "❓ j{id} yuborilganmi — aniq emas. Chatni tekshiring; MIYA qayta yubormaydi."
)


# --- the Telegram sender (JV-19) -----------------------------------------------------
# Told by the worker from what the sender recorded: the Telegram breaker, a
# flood wait, and how each send the owner tapped ended.
# The trip notice is also the health sweep's repeat of it (JV-21), so its one
# wording lives with the problem in services/health.py.
JARVIS_TELEGRAM_LIMITED = health.TELEGRAM_SEND_LIMITED
JARVIS_TELEGRAM_FLOOD = (
    "⏳ Telegram {daqiqa} daqiqa kutishni so'radi — xabarlar keyinroq yuboriladi."
)
JARVIS_SEND_FAILED = "✖️ {ism}ga yuborib bo'lmadi: {sabab}"
JARVIS_SENT = "✅ Yuborildi ({vaqt})"
# The adapter's failure codes (miya.jarvis.outbox / miya.userbot.sender).
JARVIS_SEND_FAIL_REASONS = {
    "user_blocked": "u sizni bloklagan",
    "user_deactivated": "akkaunt o'chirilgan",
    "write_forbidden": "bu chatga yozish taqiqlangan",
    "paid_only": "faqat pullik xabar qabul qiladi",
}
# Any other code: the chat could not be found, Telegram refused the text, an
# error MIYA did not expect.
JARVIS_SEND_FAIL_OTHER = "kutilmagan xato"


def jarvis_send_failed(name: str | None, code: str | None) -> str:
    """«✖️ Akmalga yuborib bo'lmadi: u sizni bloklagan». The name is the
    client's to choose: shown through _shown."""
    return JARVIS_SEND_FAILED.format(
        ism=_shown(name or JARVIS_UNNAMED_CLIENT),
        sabab=JARVIS_SEND_FAIL_REASONS.get(code or "", JARVIS_SEND_FAIL_OTHER),
    )


def jarvis_sent(sent_at: datetime) -> str:
    return JARVIS_SENT.format(vaqt=clock(sent_at))


def jarvis_flood_wait(seconds: int) -> str:
    """The wait in whole minutes, rounded up (90 seconds is «2 daqiqa»)."""
    return JARVIS_TELEGRAM_FLOOD.format(daqiqa=max(1, -(-seconds // 60)))


# --- the owner first (JV-17) ---------------------------------------------------------
# Told once, about a draft card or an escalation alert the owner was shown.
JARVIS_OWN_REPLY_DRAFT = "🙋 Siz o'zingiz javob berdingiz — qoralama bekor qilindi."
JARVIS_CLIENT_EDITED = "✏️ Mijoz xabarini tahrirladi — qoralama yangilanadi."
JARVIS_OWN_REPLY_ESCALATION = "✅ {ism}ga o'zingiz javob berdingiz — eskalatsiya yopildi."
JARVIS_UNNAMED_CLIENT = "Mijoz"


def jarvis_takeover_notice(kind: str, name: str | None = None) -> str:
    """The note for a chat_state.Notice (kinds ``draft_cancelled``,
    ``draft_edited``, ``escalation_closed``). The client's name is theirs to
    choose: shown through _shown."""
    if kind == "escalation_closed":
        return JARVIS_OWN_REPLY_ESCALATION.format(
            ism=_shown(name or JARVIS_UNNAMED_CLIENT)
        )
    if kind == "draft_edited":
        return JARVIS_CLIENT_EDITED
    return JARVIS_OWN_REPLY_DRAFT


# --- draft and escalation cards (JV-18) ----------------------------------------------
#
# The values come from miya.jarvis.cards.Card, passed in plainly (rule 20). The
# client's words and names are theirs to choose: shown through _shown. The
# text a client would get is shown exactly (escaped, never clipped).

JARVIS_CHANNEL_LABELS = {"telegram": "Telegram", "whatsapp": "WhatsApp", "gmail": "Gmail"}
JARVIS_DRAFT_HEADER = "✍️ Qoralama j{id} · {ism} · {kanal}"
JARVIS_DRAFT_CLIENT = "💬 Mijoz ({vaqt}): «{xabar}»"
JARVIS_DRAFT_BODY = "🤖 Javob: «{qoralama}»"
JARVIS_COPY_FOOTER = "📋 Yuborish hali ulanmagan — matnni nusxa olib o'zingiz yuboring."
JARVIS_BUTTON_SEND = "✅ Yuborish"
JARVIS_BUTTON_EDIT = "✏️ Tahrirlash"
JARVIS_BUTTON_OWN = "🙋 O'zim yozaman"
JARVIS_BUTTON_CANCEL = "✖️ Bekor"
JARVIS_EDIT_HINT = (
    "✏️ Yangi matnni shu qoralamaga reply qilib yozing yoki ovozli xabar yuboring."
)
JARVIS_ESCALATION_HEADER = "⚠️ {ism} {sabab} · {kanal}"
JARVIS_ESCALATION_WHO = "👤 {ism}{kod}"
JARVIS_ESCALATION_CLIENT = "💬 «{xabar}» ({vaqt})"
JARVIS_ESCALATION_SUMMARY = "📝 {qisqacha}"
JARVIS_ESCALATION_MONEY = "💰 Hisob: {faktlar}"
JARVIS_NO_OPEN_DEBT = "Ochiq qarz yo'q"
JARVIS_ESCALATION_REPLY = "🤖 {holat}: «{stall}»"
JARVIS_REPLY_SENT = "Mijozga yuborildi"
JARVIS_REPLY_READY = "Yuborishga tayyor"
JARVIS_ESCALATION_FOOTER = (
    "O'z javobingizni shu xabarga reply qilib yozsangiz, «Tasdiqlash va yuborish» "
    "aynan shuni yuboradi."
)
# Why the client's message came to the owner, by escalation reason.
JARVIS_REASON_LABELS = {
    "price": "narx so'rayapti",
    "discount": "chegirma so'rayapti",
    "payment_terms": "to'lov shartini so'rayapti",
    "deadline": "muddat so'rayapti",
    "promise": "va'da yoki kafolat so'rayapti",
    "guarantee": "va'da yoki kafolat so'rayapti",
    "payment_claim": "to'lov qilganini aytyapti",
    "identity": "bot emasmi deb so'rayapti",
    "complaint": "shikoyat qilyapti",
    "other": "boshqa savol",
}
JARVIS_REASON_OTHER = "boshqa savol"
JARVIS_BUTTON_APPROVE = "✅ Tasdiqlash va yuborish"
JARVIS_BUTTON_WRITE = "✍️ O'zim javob yozaman"
JARVIS_BUTTON_SNOOZE = "⏰ 1 soatdan keyin eslat"
JARVIS_QUEUED = "⏳ Navbatga qo'yildi — {kanal} orqali yuboriladi"
JARVIS_OWN = "🙋 Mayli, o'zingiz yozasiz. {ism} chatida MIYA {soat} soat jim turadi."
JARVIS_SNOOZED = "⏰ Soat {vaqt} da yana eslataman."
JARVIS_REMINDER = "⏰ Eslatma ({n}-marta): {ism} hali javob kutyapti"
JARVIS_STALE = "Bu qoralama eskirdi."
JARVIS_ANSWERED = "Allaqachon javob berilgan."
JARVIS_OWNER_TEXT_WARNING = (
    "⚠️ Matningizda narx yoki muddat bor — bu sizning so'zingiz sifatida yuboriladi."
)
# The owner's own words into a chat marked personal (the JV-20 wording).
JARVIS_PERSONAL_WARNING = (
    "🔒 Bu shaxsiy chat — matn sizning nomingizdan aynan shunday ketadi."
)
JARVIS_DRAFT_CAP = (
    "Bugungi qoralama limiti tugadi ({cap}). Qolganlari ertalabki xulosada."
)
# Past JARVIS_ESCALATION_DAILY_CAP the day's further alerts are one message,
# a line per escalation.
JARVIS_ALERT_FOLD = "⚠️ Bugungi eskalatsiya limiti tugadi ({cap}). Qolganlari:"
JARVIS_CANCELLED = "✖️ Bekor qilindi"
# The morning brief's Jarvis section (JV-22): yesterday's activity, then what
# the cards leave to it (cards.BriefItems): the drafts waiting for a tap, the
# chats nobody answered, the escalations still open.
JARVIS_BRIEF_ACTIVITY = (
    "🤖 Jarvis (kecha): {y} ta javob yuborildi ({a} tasi avtomatik, {t} tasi siz "
    "tasdiqlagan), {e} ta eskalatsiya ({o} tasi ochiq)"
)
JARVIS_BRIEF_WAITING = "✍️ Kutayotgan qoralamalar: {n} — /qoralamalar"
JARVIS_BRIEF_ESCALATIONS = "⚠️ Ochiq eskalatsiyalar: {items}"
JARVIS_BRIEF_ESCALATION = "{ism} ({sabab}, {qachondan})"
JARVIS_BRIEF_UNANSWERED = "📨 Javobsiz chatlar: {n} — {ismlar}"
# How many names one brief line lists; the rest are counted.
JARVIS_BRIEF_NAMES_MAX = 10
JARVIS_BRIEF_MORE = "… va yana {n} ta"
# How much of one client message a card quotes.
JARVIS_CLIENT_SHOWN_CHARS = 300
# The room a card leaves under TELEGRAM_LIMIT for what joins it later: the
# line a tap adds under it, a snooze's reminder line above it.
JARVIS_CARD_ROOM = 200
# The owner's reply to a card would make a card too long to show whole.
JARVIS_TOO_LONG = "✂️ Matn juda uzun, kartaga sig'maydi — qisqaroq qilib qayta yozing."


def jarvis_channel_label(channel: str | None) -> str:
    return JARVIS_CHANNEL_LABELS.get(channel or "", channel or "")


def jarvis_reasons(reasons) -> str:
    """«narx so'rayapti, muddat so'rayapti»: each reason once, in order."""
    labels = [JARVIS_REASON_LABELS.get(r, JARVIS_REASON_OTHER) for r in reasons or ()]
    return ", ".join(dict.fromkeys(labels)) or JARVIS_REASON_OTHER


def _jarvis_name(name: str | None) -> str:
    return _shown(name or JARVIS_UNNAMED_CLIENT)


def _client_words(text: str) -> str:
    value = " ".join((text or "").split())
    if len(value) > JARVIS_CLIENT_SHOWN_CHARS:
        value = value[: JARVIS_CLIENT_SHOWN_CHARS - 1].rstrip() + "…"
    return _shown(value)


def _card_when(at: datetime) -> str:
    """«14:05» today, «20-okt 14:05» before."""
    local = at.astimezone(settings.tz)
    if local.date() == datetime.now(settings.tz).date():
        return clock(local)
    return f"{short_date(local.date())} {clock(local)}"


def _card_body(card) -> str:
    """The exact text the client would get. In copy mode it is tap-to-copy."""
    body = escape(card.body or "")
    return f"<code>{body}</code>" if card.copy_mode else body


def _owner_warnings(card) -> list[str]:
    lines: list[str] = []
    if card.owner_text and card.redline_warning:
        lines.append(JARVIS_OWNER_TEXT_WARNING)
    if card.owner_text and card.personal_warning:
        lines.append(JARVIS_PERSONAL_WARNING)
    note = card.safety if card.owner_text else None
    if note is not None:
        who = card.name
        lines.append(
            hijack_warning(who=who)
            if note.verdict == safety.VERDICT_HIJACK
            else scam_warning(who=who, reasons=note.reasons)
        )
    return lines


def _jarvis_money(balances) -> str:
    """The client's open balances, from the ledger: «→ senga: 5 mln so'm»."""
    if not balances:
        return JARVIS_NO_OPEN_DEBT
    parts = []
    for b in balances:
        arrow = "→ senga" if b.direction is DebtDirection.they_owe_me else "← sen"
        parts.append(f"{arrow}: {money(b.amount, b.currency)}")
    return " · ".join(parts)


def jarvis_escalation_header(card) -> str:
    return JARVIS_ESCALATION_HEADER.format(
        ism=_jarvis_name(card.name),
        sabab=jarvis_reasons(card.reasons),
        kanal=jarvis_channel_label(card.channel),
    )


def jarvis_card(card) -> str:
    """A draft card (``j``) or an escalation alert (``e``) as the owner reads
    it. The figures come from SQL (``card.balances``); the summary is shown
    only when it holds none. A card is never clipped (it shows the exact
    text and its warnings): while it would not fit one message with room for
    a tap's line, the quoted client messages go, oldest first."""
    messages = list(card.messages)
    text = _jarvis_card_text(card, messages)
    while messages and len(text) > TELEGRAM_LIMIT - JARVIS_CARD_ROOM:
        messages.pop(0)
        text = _jarvis_card_text(card, messages)
    return text


def jarvis_card_fits(card) -> bool:
    """The card goes out whole, with room for the line a tap adds under it
    and a reminder's line above it."""
    return len(jarvis_card(card)) <= TELEGRAM_LIMIT - JARVIS_CARD_ROOM


def _jarvis_card_text(card, messages) -> str:
    if card.kind == "command":
        return _command_card_text(card)
    lines: list[str] = []
    if card.reminder:
        lines += [
            JARVIS_REMINDER.format(n=card.reminder, ism=_jarvis_name(card.name)),
            "",
        ]
    if card.kind == "draft":
        lines.append(
            JARVIS_DRAFT_HEADER.format(
                id=card.id,
                ism=_jarvis_name(card.name),
                kanal=jarvis_channel_label(card.channel),
            )
        )
        lines += [
            JARVIS_DRAFT_CLIENT.format(vaqt=_card_when(m.at), xabar=_client_words(m.text))
            for m in messages
        ]
        lines.append(JARVIS_DRAFT_BODY.format(qoralama=_card_body(card)))
        lines += _owner_warnings(card)
        if card.copy_mode:
            lines.append(JARVIS_COPY_FOOTER)
        return "\n".join(lines)
    kod = f" · {_shown(card.code)}" if card.code else ""
    lines += [
        jarvis_escalation_header(card),
        JARVIS_ESCALATION_WHO.format(ism=_jarvis_name(card.name), kod=kod),
    ]
    lines += [
        JARVIS_ESCALATION_CLIENT.format(
            xabar=_client_words(m.text), vaqt=_card_when(m.at)
        )
        for m in messages
    ]
    if card.summary:
        lines.append(JARVIS_ESCALATION_SUMMARY.format(qisqacha=_shown(card.summary)))
    lines.append(JARVIS_ESCALATION_MONEY.format(faktlar=_jarvis_money(card.balances)))
    if card.body:
        lines.append(
            JARVIS_ESCALATION_REPLY.format(
                holat=JARVIS_REPLY_SENT if card.body_sent else JARVIS_REPLY_READY,
                stall=_card_body(card),
            )
        )
    lines += _owner_warnings(card)
    lines.append(JARVIS_COPY_FOOTER if card.copy_mode else JARVIS_ESCALATION_FOOTER)
    return "\n".join(lines)


def jarvis_card_outcome(card_text: str, outcome: str) -> str:
    """The card with what the owner's tap came to under it."""
    return f"{card_text}\n\n{outcome}"


def jarvis_queued(channel: str | None) -> str:
    return JARVIS_QUEUED.format(kanal=jarvis_channel_label(channel))


def _hours(minutes: int) -> str:
    hours = Decimal(minutes) / Decimal(60)
    if hours == hours.to_integral_value():
        return str(int(hours))
    return f"{hours.quantize(Decimal('0.1'))}".replace(".", ",")


def jarvis_own(name: str | None, minutes: int) -> str:
    return JARVIS_OWN.format(ism=_jarvis_name(name), soat=_hours(minutes))


def jarvis_snoozed(until: datetime) -> str:
    return JARVIS_SNOOZED.format(vaqt=clock(until))


def jarvis_draft_cap(cap: int) -> str:
    return JARVIS_DRAFT_CAP.format(cap=cap)


def jarvis_fold(cards, cap: int) -> str:
    """The day's alerts past the cap, one line each."""
    lines = [JARVIS_ALERT_FOLD.format(cap=cap)]
    lines += [jarvis_escalation_header(card) for card in cards]
    return clip("\n".join(lines))


def _brief_names(names: list[str], separator: str) -> str:
    """At most JARVIS_BRIEF_NAMES_MAX names, the rest counted."""
    shown = separator.join(names[:JARVIS_BRIEF_NAMES_MAX])
    extra = len(names) - JARVIS_BRIEF_NAMES_MAX
    return (
        f"{shown}{separator}{JARVIS_BRIEF_MORE.format(n=extra)}" if extra > 0 else shown
    )


def jarvis_brief_lines(items, day=None) -> list[str]:
    """The morning brief's Jarvis section: yesterday's activity
    (status.DayReport, ``day``) and the lines of cards.BriefItems (``items``).
    Names a counterparty chose are shown through _shown."""
    lines = jarvis_day_lines(day, JARVIS_BRIEF_ACTIVITY)
    if items is None:
        return lines
    if getattr(items, "waiting", 0):
        lines.append(JARVIS_BRIEF_WAITING.format(n=items.waiting))
    if items.unanswered:
        names = [_jarvis_name(n) for n in items.unanswered]
        lines.append(
            JARVIS_BRIEF_UNANSWERED.format(
                n=len(items.unanswered), ismlar=_brief_names(names, ", ")
            )
        )
    if items.escalations:
        shown = [
            JARVIS_BRIEF_ESCALATION.format(
                ism=_jarvis_name(e.name),
                sabab=jarvis_reasons(e.reasons),
                qachondan=_card_when(e.since),
            )
            for e in items.escalations
        ]
        lines.append(JARVIS_BRIEF_ESCALATIONS.format(items=_brief_names(shown, "; ")))
    return lines


# --- the owner's commands (JV-20) ---------------------------------------------------
#
# «Sardorga Telegramdan yozib yubor …»: every proposal is a card the owner
# taps; the recipient's name, code and the chat's title are theirs to choose,
# shown through _shown. The text is the owner's, shown exactly.

JARVIS_COMMAND_HEADER = "📤 Yuborishni tasdiqlang j{id}"
JARVIS_COMMAND_WHO = "👤 Kimga: {ism}{kod} · {kanal}"
# The chat's own title, when it is not just the person's name again.
JARVIS_COMMAND_CHAT = " («{chat}» chati)"
JARVIS_COMMAND_WHEN = "🕕 Qachon: {qachon}"
JARVIS_COMMAND_BODY = "💬 Matn: «{matn}»"
JARVIS_COMMAND_NOW = "hozir"
JARVIS_COMMAND_TODAY = "bugun"
JARVIS_COMMAND_TOMORROW = "ertaga"
JARVIS_COMMAND_WHICH = "Qaysi {ism}?"
JARVIS_COMMAND_NO_CHAT = "{ism} uchun {kanal} chati topilmadi."
JARVIS_COMMAND_SCHEDULED = "🕕 {vaqt} da yuboriladi."
JARVIS_COMMAND_NOTHING = "Hech narsa navbatga qo'yilmadi — qayta urinib ko'ring."
# A command over JARVIS_DAILY_COST_CAP_USD (§7: no Jarvis model call); the
# plan gives no wording for it.
JARVIS_COMMAND_COST_CAP = (
    "💸 Jarvis bugungi xarajat chegarasiga yetdi — hech narsa navbatga "
    "qo'yilmadi, ertaga qayta urinib ko'ring."
)
# A setting the owner commanded (the plan gives no wording for these cards;
# the JV-12 cards are reused for the two loosenings they cover).
JARVIS_SETTING_QUESTIONS = {
    "personal_on": (
        "🔒 {ism}ni shaxsiy deb belgilaymi? MIYA bu chatga hech qachon o'zi "
        "yozmaydi (o'qish davom etadi)."
    ),
    "group_reply_off": "👥 «{ism}» guruhida MIYA javob yozishini o'chiraymi?",
    "monitor_on": (
        "👁 «{ism}» guruhini o'qishni boshlaymi? MIYA undagi xabarlarni o'qib, "
        "yozib boradi."
    ),
    "monitor_off": "🙈 «{ism}» guruhini o'qishni to'xtataymi?",
    "sheets_resync": "📊 Google Sheets jadvalini bazadan qaytadan to'liq yozaymi?",
}
JARVIS_SETTING_YES = {
    "personal_on": "✅ Ha, belgila",
    "group_reply_off": "✅ Ha, o'chir",
    "monitor_on": "✅ Ha, o'qi",
    "monitor_off": "✅ Ha, to'xtat",
    "sheets_resync": "✅ Ha, yangila",
}
JARVIS_SETTING_NO = "✖️ Yo'q"
JARVIS_MONITOR_ON = "👁 «{ism}» guruhi endi o'qiladi."
JARVIS_MONITOR_OFF = "🙈 «{ism}» guruhi endi o'qilmaydi."
JARVIS_SHEETS_QUEUED = "📊 Jadval keyingi sinxronlashda bazadan to'liq qayta yoziladi."
JARVIS_SETTING_STALE = "Bu taklif eskirdi — buyruqni qayta yozing."


def jarvis_command_when(moment: datetime | None, *, now: datetime | None = None) -> str:
    """«hozir», «bugun 18:00», «ertaga 09:00», «24-okt 18:00»."""
    if moment is None:
        return JARVIS_COMMAND_NOW
    local = moment.astimezone(settings.tz)
    today = (now or datetime.now(settings.tz)).astimezone(settings.tz).date()
    if local.date() == today:
        return f"{JARVIS_COMMAND_TODAY} {clock(local)}"
    if local.date() == today + timedelta(days=1):
        return f"{JARVIS_COMMAND_TOMORROW} {clock(local)}"
    return f"{short_date(local.date())} {clock(local)}"


def _same_name(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").casefold().split()) == " ".join(
        (b or "").casefold().split()
    )


def _command_card_text(card) -> str:
    kod = f" · {_shown(card.code)}" if card.code else ""
    who = JARVIS_COMMAND_WHO.format(
        ism=_jarvis_name(card.name), kod=kod, kanal=jarvis_channel_label(card.channel)
    )
    if card.chat and not _same_name(card.chat, card.name):
        who += JARVIS_COMMAND_CHAT.format(chat=_shown(card.chat))
    lines = [
        JARVIS_COMMAND_HEADER.format(id=card.id),
        who,
        JARVIS_COMMAND_WHEN.format(qachon=jarvis_command_when(card.not_before)),
        JARVIS_COMMAND_BODY.format(matn=_card_body(card)),
    ]
    lines += _owner_warnings(card)
    if card.copy_mode:
        lines.append(JARVIS_COPY_FOOTER)
    return "\n".join(lines)


def jarvis_command_scheduled(moment: datetime) -> str:
    return JARVIS_COMMAND_SCHEDULED.format(vaqt=jarvis_command_when(moment))


def jarvis_command_which(name: str | None) -> str:
    return JARVIS_COMMAND_WHICH.format(ism=_jarvis_name(name))


def jarvis_command_no_chat(name: str | None, channel: str | None) -> str:
    return JARVIS_COMMAND_NO_CHAT.format(
        ism=_jarvis_name(name), kanal=jarvis_channel_label(channel)
    )


def jarvis_setting_question(setting: str, name: str | None) -> str:
    """The card of a setting the owner commanded (the loosenings JV-12 has a
    card for use that card: ``personal_unmark_question``,
    ``group_reply_question``)."""
    if setting == "personal_off":
        return personal_unmark_question(name)
    if setting == "group_reply_on":
        return group_reply_question(name)
    return JARVIS_SETTING_QUESTIONS[setting].format(ism=_rule_name(name))


def jarvis_setting_done(setting: str | None, name: str | None, state: str | None) -> str:
    """What a ✅ on a setting card came to."""
    if setting == "personal_on":
        return personal_marked(name)
    if setting == "group_reply_off":
        if state == "personal":
            return group_reply_personal(name)
        return group_reply_off(name)
    if setting in ("monitor_on", "monitor_off"):
        line = JARVIS_MONITOR_ON if state == "on" else JARVIS_MONITOR_OFF
        return line.format(ism=_rule_name(name))
    if setting == "sheets_resync":
        return JARVIS_SHEETS_QUEUED
    return JARVIS_SETTING_STALE


# --- the responder in observation mode (JV-16) -------------------------------------
JARVIS_SHADOW_LINE = (
    "Kuzatuv: bugun {n} ta javob tayyorlanardi ({e} tasi sizga yo'naltirilardi)."
)
# Drafts per kind with a free-form statement: never sent on their own.
JARVIS_SHADOW_STRAYED = "Erkin gapli qoralamalar (o'zi yuborilmasdi): {items}"
# Also the health sweep's repeat of it (JV-21): one wording, in health.py.
JARVIS_COST_STOP = health.JARVIS_COST_CAP
SHADOW_DRAFTS_HEADER = "🧪 Kuzatuv qoralamalari:"
SHADOW_DRAFT_LINE = "{i}. {ism}: «{mijoz}» → «{qoralama}» [{intent}]"
SHADOW_DRAFTS_EMPTY = "✍️ Kutayotgan va kuzatuv qoralamalari hali yo'q."
# The drafts waiting for a tap (JV-22), above the observation drafts: the
# morning brief points here for them; j{id} is the card's own ref.
WAITING_DRAFTS_HEADER = "✍️ Kutayotgan qoralamalar:"
WAITING_DRAFT_LINE = "{i}. j{id} · {ism}: «{mijoz}» → «{qoralama}» [{intent}]"
# How much of a client's message and of a draft a list line shows.
SHADOW_SHOWN_CHARS = 120


def _shadow_text(text: str | None) -> str:
    value = " ".join((text or "").split())
    if len(value) > SHADOW_SHOWN_CHARS:
        value = value[: SHADOW_SHOWN_CHARS - 1].rstrip() + "…"
    return _shown(value)


def _draft_list_line(i: int, item, line: str = SHADOW_DRAFT_LINE) -> str:
    return line.format(
        i=i,
        id=item.id,
        ism=_shown(item.name or "Nomaʼlum"),
        mijoz=_shadow_text(item.client_text),
        qoralama=_shadow_text(item.body),
        intent=escape(jarvis_intent_label(item.intent)),
    )


def shadow_drafts(drafts, waiting=()) -> str:
    """`/qoralamalar`: the drafts waiting for the owner's tap (JV-22, the
    brief's «✍️ Kutayotgan qoralamalar»), then the newest observation drafts
    (status.ShadowDraft both). The client's words, their name and the draft
    are shown through _shown."""
    if not drafts and not waiting:
        return SHADOW_DRAFTS_EMPTY
    lines: list[str] = []
    if waiting:
        lines.append(WAITING_DRAFTS_HEADER)
        lines += [
            _draft_list_line(i, item, WAITING_DRAFT_LINE)
            for i, item in enumerate(waiting, start=1)
        ]
    if drafts:
        if lines:
            lines.append("")
        lines.append(SHADOW_DRAFTS_HEADER)
        lines += [_draft_list_line(i, item) for i, item in enumerate(drafts, start=1)]
    return clip("\n".join(lines))


def jarvis_shadow_lines(shadow) -> list[str]:
    """The `/jarvis` lines of status.ShadowCounts."""
    lines = [JARVIS_SHADOW_LINE.format(n=shadow.prepared, e=shadow.escalated)]
    if shadow.unapproved:
        items = " · ".join(
            f"{escape(jarvis_intent_label(kind))} {n}" for kind, n in shadow.unapproved
        )
        lines.append(JARVIS_SHADOW_STRAYED.format(items=items))
    return lines


def jarvis_mode_label(mode: str) -> str:
    return JARVIS_MODE_LABELS.get(mode, JARVIS_MODE_LABELS["off"])


def jarvis_status(status) -> str:
    """`/jarvis`: the mode, today's counts from SQL, the limit, the channels."""
    states = {
        channel: JARVIS_CHANNEL_STATES.get(state, JARVIS_CHANNEL_STATES["not_connected"])
        for channel, state in status.channels
    }
    body = JARVIS_STATUS.format(
        rejim=jarvis_mode_label(status.mode),
        q=status.counts.drafts,
        y=status.counts.sent,
        e=status.counts.escalations,
        used=status.limit_used,
        cap=status.limit_cap,
        telegram=states.get("telegram", JARVIS_CHANNEL_STATES["not_connected"]),
        whatsapp=states.get("whatsapp", JARVIS_CHANNEL_STATES["not_connected"]),
        gmail=states.get("gmail", JARVIS_CHANNEL_STATES["not_connected"]),
    )
    shadow = getattr(status, "shadow", None)
    if shadow is not None:
        body = "\n".join([body, *jarvis_shadow_lines(shadow)])
    return f"{body}\n{JARVIS_PAUSED}" if status.paused else body


def jarvis_resumed(mode: str) -> str:
    """The mode's short name: «qoralama rejimi», not the whole explanation."""
    return JARVIS_RESUMED.format(rejim=jarvis_mode_label(mode).split(" (")[0])


def _jarvis_ref(kind: str, ref: str) -> str:
    """j12 (an outbox row), e3 (an escalation), «qoida 5», Jarvis or a channel."""
    if kind == "outbox":
        return f"j{ref}"
    if kind == "escalation":
        return f"e{ref}"
    if kind == "rule":
        return f"qoida {ref}"
    parts = ref.split(":")
    if len(parts) == 4 and parts[0] == "channel" and parts[2] == "intent":
        # A channel × intent scope (JV-23): «Telegram · salomlashish».
        return f"{jarvis_channel_label(parts[1])} · {jarvis_intent_label(parts[3])}"
    if len(parts) == 2 and parts[0] == "intent":
        return jarvis_intent_label(parts[1])
    return JARVIS_SCOPE_LABELS.get(ref, ref)


def _jarvis_value(field: str, value: str | None) -> str:
    if value is None:
        return "—"
    if field in ("state", "owner_told"):
        return JARVIS_STATE_LABELS.get(value, value)
    if field == "mode" and value in JARVIS_MODE_LABELS:
        return jarvis_mode_label(value).split(" (")[0]
    if field == "mode" and value in JARVIS_RULE_MODE_LABELS:
        return JARVIS_RULE_MODE_LABELS[value]
    if field == "reason":
        return JARVIS_RULE_REASON_LABELS.get(value, value)
    if field == "linked":
        return _jarvis_ref("rule", value)
    if field == "pause":
        return JARVIS_PAUSE_REASON_LABELS.get(value, value)
    return value


def jarvis_history(actions) -> str:
    """`/jarvis tarix`: the last actions, newest first. Fields, states and who
    acted are in Uzbek; a code stays a code, in brackets."""
    if not actions:
        return JARVIS_HISTORY_EMPTY
    lines = [JARVIS_HISTORY_HEADER.format(n=len(actions))]
    for action in actions:
        old = _jarvis_value(action.field, action.old)
        new = _jarvis_value(action.field, action.new)
        change = f"{escape(old)} → {escape(new)}"
        code = f" [{escape(action.code)}]" if action.code else ""
        by = (
            f" ({escape(JARVIS_BY_LABELS.get(action.by, action.by))})"
            if action.by
            else ""
        )
        field = JARVIS_FIELD_LABELS.get(action.field, action.field)
        lines.append(
            f"• {short_date(action.at.astimezone(settings.tz).date())} "
            f"{clock(action.at)} · {escape(_jarvis_ref(action.kind, action.ref))} · "
            f"{escape(field)}: {change}{code}{by}"
        )
    return clip("\n".join(lines))


# --- the rollout (JV-23) ---------------------------------------------------------------
#
# The graduation offer, the silent notice after an autonomous send, a
# demotion and /jarvis rejim. The values come from miya.jarvis.rollout and
# outbox as plain values: this module never imports miya.jarvis (rule 20).
# A client's name is theirs to choose: shown through _shown, and so is the
# text sent (it may carry their name).

JARVIS_GRADUATION_OFFER = (
    "🎓 «{intent}» javoblari tayyor: {d} kunda {n} ta qoralama, {p}% tini "
    "o'zgartirmasdan tasdiqladingiz, qizil chiziq buzilmadi. Endi bunday "
    "javoblarni MIYA o'zi yuborsinmi? (kuniga {cap} tagacha)"
)
JARVIS_BUTTON_GRADUATE = "✅ Ha, avtomatik"
JARVIS_BUTTON_KEEP_DRAFT = "⏳ Yana kuzataylik"
JARVIS_CEILING_HINT = (
    "Avtomatik rejim uchun serverdagi .env da JARVIS_MODE=auto qilish kerak."
)
JARVIS_GRADUATED = (
    "✅ «{intent}» javoblarini endi MIYA o'zi yuboradi (kuniga {cap} tagacha). "
    "Har biridan keyin «↩️ Bu xato edi» tugmasi keladi."
)
JARVIS_KEPT_DRAFT = (
    "⏳ Mayli, «{intent}» javoblari qoralama bo'lib qoladi. "
    "{d} kundan keyin yana so'rayman."
)
JARVIS_GRADUATION_STALE = "Bu taklif eskirdi — holat: /jarvis rejim"
JARVIS_GRADUATION_NOT_READY = (
    "⏳ «{intent}» hali tayyor emas: taklifdan keyin holat o'zgardi. "
    "Holat: /jarvis rejim"
)
JARVIS_NEVER_AUTO = "⛔ «{intent}» javoblari hech qachon avtomatik yuborilmaydi."
JARVIS_AUTO_SENT = "🤖 Avtomatik yuborildi · {ism}: «{matn}»"
JARVIS_BUTTON_MISTAKE = "↩️ Bu xato edi"
JARVIS_DEMOTED = "↩️ «{intent}» yana qoralama rejimiga qaytdi: {sabab}."
JARVIS_DEMOTION_REASONS = {
    "mistake": "siz «Bu xato edi» deb belgiladingiz",
    "breaker": "kanalda cheklov ishladi",
    "incident": "yuborilgan javobda qizil chiziq topildi",
    "cost_cap": "xarajat chegarasi bir haftada ikki marta oshdi",
}
JARVIS_DEMOTION_OTHER = "xavfsizlik uchun"
JARVIS_MISTAKE_STALE = "Bu xabar eskirdi."
JARVIS_MODES_HEADER = "🎛 Jarvis rejimlari — {kanal}:"
JARVIS_MODES_LINE = "• {intent}: {rejim}"
JARVIS_MODES_PROGRESS = " — {d} kun, {n} ta qoralama, {p}% o'zgartirmasdan"
JARVIS_MODES_WAITING = ", {n} ta kutmoqda"
JARVIS_MODES_CAPPED = " (ruxsat berilgan, lekin hozir: {rejim})"
JARVIS_MODES_LIMITED = " · kanal cheklovi oxirgi 7 kunda"
JARVIS_MODES_NEVER = "🔒 Hech qachon avtomatik emas: {intents}"
JARVIS_MODES_CEILING = "Yuqori chegara (.env JARVIS_MODE): {rejim}"
JARVIS_MODES_CRITERIA = (
    "Avtomatikka o'tish: kamida {d} kun, {n} ta qoralama, {p}% o'zgartirmasdan "
    "tasdiq, «Bu xato edi», qizil chiziq hodisasi va kanal cheklovisiz — keyin "
    "MIYA so'raydi, siz «Ha» bossangizgina o'tadi."
)
JARVIS_MODES_REVIEWS = "🧪 Qizil chiziq uchun ko'rib chiqiladigan holatlar: {n} ta"


def _rollout_intent(intent: str, channel: str | None = None) -> str:
    """«salomlashish», or «salomlashish · WhatsApp» off Telegram."""
    label = jarvis_intent_label(intent)
    if channel and channel != "telegram":
        label = f"{label} · {jarvis_channel_label(channel)}"
    return escape(label)


def _short_mode(mode: str) -> str:
    return jarvis_mode_label(mode).split(" (")[0]


def jarvis_graduation_offer(
    *,
    intent: str,
    channel: str,
    days: int,
    drafts: int,
    pct: int,
    cap: int,
    ceiling_ok: bool,
) -> str:
    """«🎓 …» with the ceiling hint when JARVIS_MODE is not auto."""
    text = JARVIS_GRADUATION_OFFER.format(
        intent=_rollout_intent(intent, channel), d=days, n=drafts, p=pct, cap=cap
    )
    return text if ceiling_ok else f"{text}\n{JARVIS_CEILING_HINT}"


def jarvis_graduated(*, intent: str, channel: str, cap: int, ceiling_ok: bool) -> str:
    text = JARVIS_GRADUATED.format(intent=_rollout_intent(intent, channel), cap=cap)
    return text if ceiling_ok else f"{text}\n{JARVIS_CEILING_HINT}"


def jarvis_kept_draft(*, intent: str, channel: str, days: int) -> str:
    return JARVIS_KEPT_DRAFT.format(intent=_rollout_intent(intent, channel), d=days)


def jarvis_never_auto(intent: str) -> str:
    return JARVIS_NEVER_AUTO.format(intent=_rollout_intent(intent))


def jarvis_graduation_not_ready(*, intent: str, channel: str) -> str:
    return JARVIS_GRADUATION_NOT_READY.format(intent=_rollout_intent(intent, channel))


def jarvis_auto_sent(name: str | None, body: str) -> str:
    """«🤖 Avtomatik yuborildi · Akmal: «…»». The name and the text sent are
    shown escaped and stripped of bidi and zero-width characters."""
    return JARVIS_AUTO_SENT.format(
        ism=_shown(name or JARVIS_UNNAMED_CLIENT), matn=_shown(body)
    )


def jarvis_demoted(*, intent: str, channel: str | None, code: str) -> str:
    return JARVIS_DEMOTED.format(
        intent=_rollout_intent(intent, channel),
        sabab=JARVIS_DEMOTION_REASONS.get(code, JARVIS_DEMOTION_OTHER),
    )


def _mode_line(found) -> str:
    """One intent of /jarvis rejim: its mode now, and in draft its progress
    towards graduation; an ``auto`` the ceiling holds down says so."""
    stored, effective = found.stored, found.effective
    shown = "auto" if stored == "auto" else effective
    line = JARVIS_MODES_LINE.format(
        intent=escape(jarvis_intent_label(found.intent)), rejim=_short_mode(shown)
    )
    if stored == "auto" and effective != "auto":
        line += JARVIS_MODES_CAPPED.format(rejim=_short_mode(effective))
    elif effective == "draft":
        line += JARVIS_MODES_PROGRESS.format(d=found.days, n=found.drafts, p=found.pct)
        if found.pending:
            line += JARVIS_MODES_WAITING.format(n=found.pending)
    if found.breaker_recent:
        line += JARVIS_MODES_LIMITED
    return line


def jarvis_modes(view) -> str:
    """`/jarvis rejim`: per channel each reply kind's mode (and in draft its
    progress towards graduation), the kinds that are never autonomous, the
    env ceiling (with the hint when it is not auto) and the review cases."""
    lines: list[str] = []
    for channel in view.channels:
        lines.append(
            JARVIS_MODES_HEADER.format(kanal=jarvis_channel_label(channel.channel))
        )
        lines.extend(_mode_line(found) for found in channel.intents)
        lines.append("")
    lines.append(
        JARVIS_MODES_NEVER.format(
            intents=escape(", ".join(jarvis_intent_label(i) for i in view.never_auto))
        )
    )
    lines.append(JARVIS_MODES_CEILING.format(rejim=_short_mode(view.ceiling)))
    if view.ceiling != "auto":
        lines.append(JARVIS_CEILING_HINT)
    lines.append(
        JARVIS_MODES_CRITERIA.format(
            d=settings.jarvis_graduation_min_days,
            n=settings.jarvis_graduation_min_drafts,
            p=settings.jarvis_graduation_min_approval_pct,
        )
    )
    if view.reviews:
        lines.append(JARVIS_MODES_REVIEWS.format(n=view.reviews))
    return clip("\n".join(lines))


# --- /savolbank: the question bank (JV-23) ---------------------------------------------
#
# The questions are the owner's own words, shown escaped. qbank's refusal
# codes are worded here.

QBANK_HEADER = (
    "❓ Savollar banki ({n} ta) — MIYA mijozga o'zi (sizning tugmangizsiz) faqat "
    "shu savollarni bera oladi; boshqa har qanday savol sizga qoralama bo'lib keladi:"
)
QBANK_LINE = "{i}. {savol}"
QBANK_EMPTY = "❓ Savollar banki bo'sh — MIYA mijozga o'zi hech qanday savol bermaydi."
QBANK_USAGE = (
    "<code>/savolbank</code> — ro'yxat · <code>/savolbank qo'sh Savol?</code> — "
    "qo'shish · <code>/savolbank o'chir 3</code> — o'chirish"
)
QBANK_PROPOSED = (
    "❓ Savollar bankiga qo'shilsinmi?\n«{savol}»\n"
    "Shundan keyin MIYA bu savolni mijozlarga o'zi bera oladi."
)
QBANK_ADD = "✅ Qo'shish"
QBANK_DISMISS = "✖️ Kerak emas"
QBANK_REMOVE = "🗑 {i}"
QBANK_ADDED = "✅ Savol qo'shildi: «{savol}»"
QBANK_DECLINED = "✖️ Qo'shilmadi: «{savol}»"
QBANK_REMOVED = "🗑 Savol olib tashlandi: «{savol}»"
QBANK_STALE = "Bu savol allaqachon o'zgargan — /savolbank"
QBANK_NOT_FOUND = "Bunday raqamli savol yo'q — /savolbank"
QBANK_REFUSED = "⛔ Bu savolni qo'shib bo'lmaydi: {sabab}."
QBANK_REASONS = {
    "empty": "savol yozilmagan",
    "too_long": "juda uzun (120 belgigacha)",
    "not_one_question": "bitta savol bo'lishi kerak",
    "red_line": "unda narx, muddat, va'da, raqam, havola yoki ism bor",
    "not_a_question": "bu savol emas (savol so'zi yoki «-mi» kerak)",
    "already": "bu savol bankda bor",
    "full": "bank to'lgan — avval birini o'chiring",
}
QBANK_REASON_OTHER = "tekshiruvdan o'tmadi"


def qbank_list(entries) -> str:
    """`/savolbank`: the questions in force, numbered as the 🗑 buttons."""
    if not entries:
        return QBANK_EMPTY
    lines = [QBANK_HEADER.format(n=len(entries))]
    lines.extend(
        QBANK_LINE.format(i=i, savol=escape(entry.text))
        for i, entry in enumerate(entries, start=1)
    )
    lines.append(QBANK_USAGE)
    return clip("\n".join(lines))


def qbank_proposed(text: str) -> str:
    return QBANK_PROPOSED.format(savol=escape(text))


def qbank_refused(code: str) -> str:
    return QBANK_REFUSED.format(sabab=QBANK_REASONS.get(code, QBANK_REASON_OTHER))


def qbank_added(text: str) -> str:
    return QBANK_ADDED.format(savol=escape(text))


def qbank_declined(text: str) -> str:
    return QBANK_DECLINED.format(savol=escape(text))


def qbank_removed(text: str) -> str:
    return QBANK_REMOVED.format(savol=escape(text))


# --- /shaxsiy, /shaxsiylar, /guruhai (JV-12) -------------------------------------------
#
# Names and titles here are a counterparty's choosing: shown through _shown.

PERSONAL_USAGE = (
    "Kimni shaxsiy deb belgilay? <code>/shaxsiy Ism</code> yoki "
    "<code>/shaxsiy GS367</code> (oilaviy guruh nomi ham bo'ladi).\n"
    "MIYA belgilangan chatga hech qachon o'zi yozmaydi. Ro'yxat: /shaxsiylar"
)
PERSONAL_MARKED = (
    "🔒 {ism} shaxsiy deb belgilandi. MIYA bu chatga hech qachon o'zi yozmaydi "
    "(o'qish davom etadi)."
)
PERSONAL_UNMARK_QUESTION = (
    "🔓 {ism}ni shaxsiydan chiqaraymi? Shunda MIYA unga qoralama tayyorlay oladi."
)
PERSONAL_UNMARK_YES = "✅ Ha, chiqar"
PERSONAL_UNMARK_NO = "✖️ Yo'q"
PERSONAL_UNMARKED = "🔓 {ism} shaxsiydan chiqarildi."
PERSONAL_LIST_HEADER = "🔒 Shaxsiy chatlar ({n}):"
PERSONAL_LIST_EMPTY = "Hali hech kim belgilanmagan. Belgilash: <code>/shaxsiy Ism</code>"
PERSONAL_SUGGESTIONS_HEADER = "💡 Oila yoki do'st bo'lishi mumkin:"
PERSONAL_LIST_HINT = (
    "<i>🔒 tugmasi — bir bosishda shaxsiy deb belgilash; 🔓 — shaxsiydan chiqarish "
    "(tasdiq so'raladi).</i>"
)
PERSONAL_UNKNOWN_CHAT = "Nomaʼlum chat"
# The most markings listed by name in one message (the buttons follow it).
PERSONAL_LIST_MAX = 30
RULE_KEPT = "✖️ Hech narsa o'zgarmadi."
RULE_STALE = "Bu savol eskirgan — ro'yxat: /shaxsiylar"

GROUP_REPLY_USAGE = (
    "Qaysi guruh? <code>/guruhai Guruh nomi</code> — shu guruhda MIYA javob "
    "yozishini yoqish yoki o'chirish (faqat sizga murojaat qilinganda)."
)
GROUP_REPLY_ENABLED = "👥 Javob yoqilgan guruhlar: {list}"
GROUP_REPLY_QUESTION = (
    "👥 «{guruh}» guruhida MIYA javob yozsinmi? Faqat sizga murojaat qilingan "
    "xabarlarga, har biri sizning tasdig'ingiz bilan."
)
GROUP_REPLY_YES = "✅ Ha, yoq"
GROUP_REPLY_NO = "✖️ Yo'q"
GROUP_REPLY_ON = (
    "👥 «{guruh}» guruhida MIYA javob yozishi yoqildi (faqat sizga murojaat "
    "qilinganda)."
)
GROUP_REPLY_OFF = "👥 «{guruh}» guruhida MIYA javob yozishi o'chirildi."
GROUP_REPLY_PERSONAL = (
    "🔒 «{guruh}» shaxsiy deb belgilangan — MIYA u yerda javob yozmaydi."
)
GROUP_NOT_FOUND = "❓ «{guruh}» nomli guruh topilmadi."
GROUP_AMBIGUOUS = (
    "Qaysi {kind}? {list} — nomini to'liqroq yozing yoki raqamini: "
    "<code>/{command} {example}</code>"
)


def _rule_name(name: str | None) -> str:
    return _shown(name) if name else PERSONAL_UNKNOWN_CHAT


def personal_marked(name: str | None) -> str:
    return PERSONAL_MARKED.format(ism=_rule_name(name))


def personal_unmark_question(name: str | None) -> str:
    return PERSONAL_UNMARK_QUESTION.format(ism=_rule_name(name))


def personal_unmarked(name: str | None) -> str:
    return PERSONAL_UNMARKED.format(ism=_rule_name(name))


def personal_list(marked, suggestions) -> str:
    """`/shaxsiylar`: the personal chats by name, then MIYA's suggestions of
    likely family and friends (each one a 🔒 button below)."""
    lines = [PERSONAL_LIST_HEADER.format(n=len(marked))]
    if not marked:
        lines.append(PERSONAL_LIST_EMPTY)
    lines += [f"• {_rule_name(item.name)}" for item in marked[:PERSONAL_LIST_MAX]]
    if len(marked) > PERSONAL_LIST_MAX:
        lines.append(f"… va yana {len(marked) - PERSONAL_LIST_MAX} ta")
    if suggestions:
        lines += ["", PERSONAL_SUGGESTIONS_HEADER]
        lines += [f"• {_rule_name(item.name)}" for item in suggestions]
    if marked or suggestions:
        lines += ["", PERSONAL_LIST_HINT]
    return clip("\n".join(lines))


def group_reply_question(title: str | None) -> str:
    return GROUP_REPLY_QUESTION.format(guruh=_rule_name(title))


def group_reply_on(title: str | None) -> str:
    return GROUP_REPLY_ON.format(guruh=_rule_name(title))


def group_reply_off(title: str | None) -> str:
    return GROUP_REPLY_OFF.format(guruh=_rule_name(title))


def group_reply_personal(title: str | None) -> str:
    return GROUP_REPLY_PERSONAL.format(guruh=_rule_name(title))


def group_not_found(title: str) -> str:
    return GROUP_NOT_FOUND.format(guruh=_shown(title))


def group_ambiguous(chats, *, command: str) -> str:
    """Several chats match: each with its Telegram id, which picks one
    (``/guruhai -100123``), since two chats can share a title."""
    shown = ", ".join(f"«{_rule_name(title)}» ({int(tg_id)})" for title, tg_id in chats)
    return GROUP_AMBIGUOUS.format(
        kind="guruh" if command == "guruhai" else "chat",
        list=shown,
        command=command,
        example=int(chats[0][1]),
    )


def group_reply_usage(enabled_titles) -> str:
    """`/guruhai` alone: how to use it, and which groups are switched on."""
    if not enabled_titles:
        return GROUP_REPLY_USAGE
    shown = ", ".join(f"«{_rule_name(t)}»" for t in enabled_titles)
    return f"{GROUP_REPLY_USAGE}\n{GROUP_REPLY_ENABLED.format(list=shown)}"


# --- /uslub (JV-14) -----------------------------------------------------------------
#
# The values come from miya.jarvis.style, passed in plainly: this module is
# imported at startup and must not import miya.jarvis (rule 20). An example's
# client line is the counterparty's text: shown through _shown, like the
# owner's own reply beside it.

STYLE_CARD = (
    "🗣 Uslubingiz ({n} ta xabardan):\n"
    "• Salomlashish: {salom}\n"
    "• Murojaat: {murojaat}\n"
    "• O'rtacha uzunlik: {k} so'z\n"
    "• Tez-tez: {iboralar}\n"
    "• Emoji: {emoji}\n"
    "• Yozuv: lotin {p}% · kirill {q}% · rus so'zlari {r}%\n"
    "Misollar:{misollar}"
)
STYLE_EXAMPLE_LINE = "• «{mijoz}» → «{siz}»"
STYLE_NONE = "—"
STYLE_EMOJI_LABELS = {"low": "kam", "medium": "o'rtacha", "high": "ko'p"}
STYLE_STATE_LINE = "Versiya: v{v} · {holat}"
STYLE_STATE_LABELS = {
    "active": "faol",
    "draft": "qoralama — faollashtirilmagan",
    "retired": "eskirgan",
}
STYLE_ACTIVATE = "✅ Faollashtirish"
STYLE_REBUILD = "🔄 Qayta tuzish"
STYLE_IMPORT_STARTED = (
    "📥 Uslub uchun eski yozishmalaringizni yuklayapman. Tayyor bo'lganda yozaman."
)
# Without JARVIS_ENABLED nothing sends «✅ Uslub tayyor», so nothing is promised.
STYLE_IMPORT_STARTED_MANUAL = (
    "📥 Uslub uchun eski yozishmalaringizni yuklayapman. Bir necha daqiqadan "
    "so'ng /uslub ni oching."
)
STYLE_IMPORT_NO_CHATS = (
    "📥 Yuklash uchun shaxsiy chat topilmadi (shaxsiy deb belgilangan va "
    "o'chirilgan chatlar o'qilmaydi)."
)
STYLE_READY = "✅ Uslub tayyor: {n} ta xabar, {m} ta misol. /uslub"
STYLE_TOO_FEW = (
    "🗣 Uslubni o'rganish uchun xabarlaringiz hali kam: {n} ta (kamida {min} ta "
    "kerak). Eski yozishmalardan o'rganish: <code>/uslub yukla</code>"
)
STYLE_ACTIVATED = "✅ Uslub v{v} faollashtirildi."
STYLE_STALE = "Bu uslub versiyasi eskirgan — hozirgisi: /uslub"
STYLE_USAGE = (
    "<code>/uslub</code> — yozish uslubingiz · <code>/uslub yukla</code> — eski "
    "yozishmalaringizdan o'rganish"
)


def _style_list(values) -> str:
    shown = [f"«{_shown(v)}»" for v in values if v]
    return ", ".join(shown) if shown else STYLE_NONE


def style_card(card) -> str:
    """`/uslub`: the counted traits, a few of the owner's own replies as
    examples (the client's name replaced), and the version's state."""
    examples = [
        STYLE_EXAMPLE_LINE.format(mijoz=_shown(pair.context), siz=_shown(pair.reply))
        for pair in card.examples
    ]
    body = STYLE_CARD.format(
        n=card.sample_count,
        salom=_style_list(card.greetings),
        murojaat=", ".join(_shown(v) for v in card.address) or STYLE_NONE,
        k=card.avg_words,
        iboralar=_style_list(card.phrases),
        emoji=STYLE_EMOJI_LABELS.get(card.emoji_level, STYLE_EMOJI_LABELS["low"]),
        p=card.latin_pct,
        q=card.cyrillic_pct,
        r=card.russian_pct,
        misollar=("\n" + "\n".join(examples)) if examples else f" {STYLE_NONE}",
    )
    state = STYLE_STATE_LINE.format(
        v=card.version, holat=STYLE_STATE_LABELS.get(card.state, card.state)
    )
    return clip(f"{body}\n{state}")


def style_ready(n: int, m: int) -> str:
    return STYLE_READY.format(n=n, m=m)


def style_too_few(n: int, minimum: int) -> str:
    return STYLE_TOO_FEW.format(n=n, min=minimum)


def style_activated(version: int) -> str:
    return STYLE_ACTIVATED.format(v=version)


# --- /bilim (JV-15) -----------------------------------------------------------------
#
# The entries come from miya.jarvis.kb as plain values (kb.EntryView): this
# module never imports miya.jarvis (rule 20). A suggestion is written from
# clients' messages, so every entry's text is shown through _shown.

KB_HEADER = "📚 Bilimlar bazasi ({n} ta faol):"
KB_LINE_FAQ = "<code>{ref}</code> {savol} — {javob}"
KB_LINE_FACT = "<code>{ref}</code> {javob}"
KB_MORE = "… yana {n} ta"
KB_EMPTY = "📚 Bilimlar bazasi bo'sh. Qo'shish: <code>/bilim qo'sh Savol | Javob</code>"
KB_ADDED = "✅ Qo'shildi: b{id} — {savol}"
KB_EDITED = "✏️ b{id} yangilandi: {savol}"
KB_REFUSED = (
    "⛔ Qo'shilmadi: javobda {belgilar} bor. Bunday savollarga MIYA doim sizga "
    "yo'naltiradi."
)
# A client's personal data is never stored (no phone, card, e-mail, handle,
# client code or a person's name).
KB_REFUSED_PRIVATE = (
    "⛔ Qo'shilmadi: matnda {belgilar} bor. Mijozlarning shaxsiy ma'lumotlari "
    "bilimlar bazasiga yozilmaydi."
)
KB_TOO_LONG = "⛔ Qo'shilmadi: matn juda uzun (ko'pi bilan {n} belgi)."
KB_SUGGESTED = "💡 Javoblaringizdan {n} ta bilim taklif qilindi: /bilim taklif"
KB_SUGGESTION_CARD = "💡 Taklif {ref}:\n{savol}\n— {javob}"
KB_NO_SUGGESTIONS = "💡 Hozircha bilim takliflari yo'q."
KB_ADD = "✅ Qo'shish"
KB_DISMISS = "✖️ Kerak emas"
KB_RETIRED = "🗑 b{id} o'chirildi."
KB_DISMISSED = "✖️ b{id} taklifi olib tashlandi."
KB_STALE = "Bu taklif allaqachon ko'rib chiqilgan: /bilim taklif"
KB_NOT_FOUND = "b{id} topilmadi yoki allaqachon o'chirilgan: /bilim"
KB_USAGE = (
    "<code>/bilim</code> — bilimlar bazasi\n"
    "<code>/bilim qo'sh Savol | Javob</code> — qo'shish (savolsiz bo'lsa — fakt)\n"
    "<code>/bilim tuzat b12 Savol | Javob</code> — tuzatish\n"
    "<code>/bilim o'chir b12</code> — o'chirish\n"
    "<code>/bilim taklif</code> — javoblaringizdan MIYA takliflari"
)
# How much of a long question or answer a list line or a confirmation shows.
KB_SHOWN_CHARS = 120


def _kb_text(text: str | None) -> str:
    value = (text or "").strip()
    if len(value) > KB_SHOWN_CHARS:
        value = value[: KB_SHOWN_CHARS - 1].rstrip() + "…"
    return _shown(value)


def _kb_title(entry) -> str:
    """What names an entry in a confirmation: its question, else its answer."""
    return _kb_text(entry.question or entry.answer)


def kb_list(entries, active: int) -> str:
    """`/bilim`: the active entries, oldest first."""
    if not active:
        return KB_EMPTY
    lines = [KB_HEADER.format(n=active)]
    for entry in entries:
        if entry.question:
            lines.append(
                KB_LINE_FAQ.format(
                    ref=entry.ref,
                    savol=_kb_text(entry.question),
                    javob=_kb_text(entry.answer),
                )
            )
        else:
            lines.append(KB_LINE_FACT.format(ref=entry.ref, javob=_kb_text(entry.answer)))
    if active > len(entries):
        lines.append(KB_MORE.format(n=active - len(entries)))
    return clip("\n".join(lines))


def kb_added(entry) -> str:
    return KB_ADDED.format(id=entry.id, savol=_kb_title(entry))


def kb_edited(entry) -> str:
    return KB_EDITED.format(id=entry.id, savol=_kb_title(entry))


def kb_refused(reason: str, labels, limit: int = 0) -> str:
    """Why an entry was not stored (kb.Refusal)."""
    if reason == "too_long":
        return KB_TOO_LONG.format(n=limit)
    if reason == "empty":
        return KB_USAGE
    shown = ", ".join(_shown(label) for label in labels)
    if reason == "private":
        return KB_REFUSED_PRIVATE.format(belgilar=shown)
    return KB_REFUSED.format(belgilar=shown)


def kb_suggested(n: int) -> str:
    return KB_SUGGESTED.format(n=n)


def kb_suggestion(entry) -> str:
    """One suggestion card, above ✅ Qo'shish / ✖️ Kerak emas."""
    return clip(
        KB_SUGGESTION_CARD.format(
            ref=entry.ref,
            savol=_shown(entry.question or ""),
            javob=_shown(entry.answer),
        )
    )


def kb_retired(entry_id: int) -> str:
    return KB_RETIRED.format(id=entry_id)


def kb_dismissed(entry_id: int) -> str:
    return KB_DISMISSED.format(id=entry_id)


def kb_not_found(entry_id: int) -> str:
    return KB_NOT_FOUND.format(id=entry_id)
