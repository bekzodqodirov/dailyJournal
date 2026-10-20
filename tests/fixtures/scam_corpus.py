"""The spam and scam message corpus (JV-09): one case per shape the message
shield must get right. tests/test_safety_messages.py reads it; real messages
from the owner's chats are added here, anonymised — no real names, card
digits, phone numbers or live links.

Each case: the message text, what else the message carried (Telegram text
links, URL buttons, an attachment name, the sender's account flags, a
forward or an inline bot) and what is known of the sender (a saved contact,
their earlier messages), with the expected verdict and, for a hard hit, the
rule that must fire.
"""

from __future__ import annotations

from typing import Any


def case(text: str, verdict: str, reason: str | None = None, **features: Any) -> dict:
    return {"text": text, "verdict": verdict, "reason": reason, "features": features}


# A known client: a saved contact who has written before.
KNOWN = {"saved_contact": True, "prior_messages": 20, "owner_wrote_before": True}


# --- hard hits: any one rule quarantines ------------------------------------------

SCAM: list[dict] = [
    case(
        "Hurmatli mijoz, kartangiz bloklandi. Tiklash uchun: https://payme-uz.xyz/login",
        "scam",
        "brand_domain",
    ),
    case("To'lovni tasdiqlang: http://paymee.uz/confirm", "scam", "lookalike_domain"),
    case("Click orqali bonus oling: https://click-bonus.top", "scam", "brand_domain"),
    case(
        "Sizga 500 000 so'm o'tkazildi, qabul qiling: https://uzcard-online.site/get",
        "scam",
        "brand_domain",
    ),
    case(
        "Soliq qarzingiz bor, to'lang: https://soliq-uz.com/pay",
        "scam",
        "brand_domain",
    ),
    case(
        "Telegram Premium sovg'a! https://telegram-premium.click/gift",
        "scam",
        "brand_domain",
    ),
    # A Cyrillic «а» inside a Latin name, and a name spelled only in Cyrillic
    # stand-ins.
    case("Hisobingizni tekshiring: http://pаyme.uz/check", "scam", "mixed_script_domain"),
    case("Kiring: https://рауме.uz", "scam", "mixed_script_domain"),
    # A text link that shows one site and opens another.
    case(
        "To'lov cheki",
        "scam",
        "text_link_mismatch",
        text_links=[("payme.uz/cheque/123", "https://evil-cheque.top/c")],
    ),
    case(
        "Kirish",
        "scam",
        "text_link_mismatch",
        text_links=[("https://my.gov.uz", "http://gov-uz.online/auth")],
    ),
    case("Batafsil: https://payme.uz@login-check.top/x", "scam", "userinfo_url"),
    # Installers and archives, by link or by file.
    case(
        "Yangi ilovani o'rnating: https://files.example.com/Payme_new.apk",
        "scam",
        "exec_url",
    ),
    case("Hujjatlar: https://dl.example.org/docs.rar", "scam", "archive_url"),
    case("", "scam", "exec_file", attachment="Rasmlar.jpg.apk"),
    case("Mana hujjatlar", "scam", "archive_file", attachment="hujjatlar.zip", **KNOWN),
    # A code or card data asked for.
    case("Sizga kelgan SMS kodni ayting, xatolik bo'ldi", "scam", "code_request"),
    case("Telefoningizga kelgan kodni yozib yuboring iltimos", "scam", "code_request"),
    case("Скажите код из смс, который пришел вам", "scam", "code_request"),
    case("Tasdiqlash kodini yuboring", "scam", "code_request"),
    case("Kelgan kodni ayting, adashib yuborishdi", "scam", "code_request"),
    # Uzbek Cyrillic, «ё» and all.
    case("Кодни ёзинг, смс келган", "scam", "code_request"),
    case("Kodni aytasizmi? SMS orqali keldi", "scam", "code_request", **KNOWN),
    case("Kartangiz CVV kodini yozib yuboring", "scam", "card_data_request"),
    case(
        "Karta amal qilish muddati va orqasidagi 3 raqamni yuboring",
        "scam",
        "card_data_request",
    ),
    # Telegram's own account flags.
    case("Assalomu alaykum", "scam", "telegram_scam_flag", sender_scam=True),
    case("Salom, yordam kerak", "scam", "telegram_fake_flag", sender_fake=True),
    # «Is this you in the photo?» with a file or a link.
    case("Bu sizmi rasmda? 😳", "scam", "photo_lure", attachment="rasm_2026.pdf"),
    case("Это ты на фото? https://telegra.ph/foto-10-20", "scam", "photo_lure"),
    # A known contact gets no pass for a hard rule.
    case("Akmal aka, kelgan sms kodni ayting", "scam", "code_request", **KNOWN),
]


# --- soft signals that add up -----------------------------------------------------

SPAM: list[dict] = [
    case(
        "Tekin premium! Faqat bugun: bit.ly/xyz12 Do'stlaringizga ulashing",
        "spam",
        forwarded=True,
    ),
    # A stranger who wrote a few times before is still a stranger: their spam
    # is spam, never a hijacked account.
    case(
        "Tekin premium! Faqat bugun: bit.ly/xyz12 Do'stlaringizga ulashing",
        "spam",
        forwarded=True,
        prior_messages=4,
    ),
    case("Kunlik daromad 500$! Batafsil: https://earn-money.xyz", "spam"),
    case("Aksiya! Chegirma 50%. https://cutt.ly/abc", "spam"),
    case("Kriptovalyuta investitsiya, kunlik foyda: https://site123.me/invest", "spam"),
    case(
        "Бесплатно! Заработок без вложений: https://earn.online",
        "spam",
        via_bot=True,
    ),
    case(
        "Yutib oldingiz! Sovrin olish uchun: https://tinyurl.com/prize",
        "spam",
    ),
]


# --- a contact with history suddenly unlike themselves ----------------------------

HIJACK: list[dict] = [
    case("Aka, pul kerak edi, qarz berib turing ertaga qaytaraman", "hijack", **KNOWN),
    case("Mana ko'ring: https://bit.ly/3abcde", "hijack", **KNOWN),
    case("", "hijack", attachment="hisob_faktura.html", **KNOWN),
]


# --- ordinary business: never flagged ---------------------------------------------

BENIGN: list[dict] = [
    case("Assalomu alaykum, GS367 yuki qachon keladi?", "ok"),
    case("Konteyner yo'lda, ertaga Toshkentda bo'ladi", "ok", **KNOWN),
    case("Karta raqamingizni tashlang, pulni o'tkazaman", "ok"),
    case("Payme orqali to'ladim, chek: https://payme.uz/checkout/abc", "ok"),
    case("To'lov qildim click.uz orqali", "ok", **KNOWN),
    case("Mening kodimni yuboring, GS kodini unutibman", "ok", **KNOWN),
    case("Kodni hech kimga aytmang deb yozibdi bank", "ok", **KNOWN),
    case("Hisob-faktura ilova qilindi", "ok", attachment="hisob_faktura.pdf", **KNOWN),
    case("Narxlar ro'yxati", "ok", attachment="narxlar.xlsx"),
    case("Здравствуйте, срочно нужно предложение по доставке из Китая", "ok"),
    case("Добрый день, отправил вам счет на почту akmal@mail.ru", "ok", **KNOWN),
    case("Lokatsiya: https://maps.google.com/?q=41.3,69.2", "ok", **KNOWN),
    case("Video: https://youtu.be/abc123 shu yerda ko'rsatilgan", "ok", **KNOWN),
    case("Davlat xizmatlari: https://my.gov.uz/uz/service/123", "ok"),
    case("Guruhga qo'shiling: https://t.me/+AbCdEf12345", "ok", **KNOWN),
    case("Taobao link: https://item.taobao.com/item.htm?id=123", "ok", **KNOWN),
    case("Salom", "ok"),
    case("Yuk xati YW26-004715, hujjat ilova", "ok", attachment="waybill.pdf"),
    case("Bu sizmi? Kecha ko'rishdik shekilli", "ok"),
    case("Qarzni ertaga qaytaraman, xavotir olmang", "ok"),
    case("Taklifimiz bor, narxlarni yuboraymi?", "ok"),
    case(
        "Bonus sifatida keyingi buyurtmaga chegirma beramiz",
        "ok",
        **KNOWN,
    ),
    # A client code is not a one-time code, «kelgan» is every delivery, and
    # what the writer did themselves is no request.
    case("Klient kodi: GS367. Kelgan tovarlarni yozib yuboring", "ok", **KNOWN),
    case("Kelgan yuk kodi nima? Yozib yuboring", "ok", **KNOWN),
    case("Kelgan tovarni hech kimga bermang, menga yozing", "ok", **KNOWN),
    case("Parolni yubordim", "ok", **KNOWN),
    case("Akkaunt parolini o'zgartirdim, yangisini yozdim", "ok", **KNOWN),
    # A contract's term and the back of a page are not a card's.
    case("Отправьте договор, срок действия до 2027 года", "ok", **KNOWN),
    case("Shartnomaning amal qilish muddatini yozib yuboring", "ok", **KNOWN),
    case("Подпишите на обороте и отправьте скан", "ok", **KNOWN),
    # «Is it you who …?» is an ordinary question; a real site is no lure.
    case("Это вы отправили этот счет?", "ok", attachment="schet.pdf", **KNOWN),
    case("Bu sizmi buyurtma bergansiz?", "ok", attachment="invoice.pdf", **KNOWN),
    case("Rasmlaringizni oldim, rahmat", "ok", attachment="rasmlar.pdf", **KNOWN),
    case("Salom! Bu sizmi? https://kun.uz/news/1", "ok", **KNOWN),
    # A new client asking for prices on the marketplaces they buy from.
    case(
        "Assalomu alaykum, shu tovarlarga narx taklif qiling: "
        "https://item.taobao.com/a https://detail.1688.com/b "
        "https://item.taobao.com/c",
        "ok",
    ),
    case("Здравствуйте, срочно нужна доставка, подробнее: https://ourcompany.shop", "ok"),
    # A text link that opens a real site of the same family.
    case("Video", "ok", text_links=[("youtube.com", "https://youtu.be/abc")]),
    case("Xizmat", "ok", text_links=[("gov.uz", "https://my.gov.uz/uz/service/1")]),
]
