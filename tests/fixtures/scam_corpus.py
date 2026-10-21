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
    # «My code» claimed, but sent to the reader by mistake; a question about a
    # code that came to the reader; a one-time password.
    case("Kodim adashib sizga keldi, aytib yuboring iltimos", "scam", "code_request"),
    case("Скажите, какой код пришёл вам в смс?", "scam", "code_request"),
    case("Введите одноразовый пароль из смс", "scam", "code_request"),
    # The card's expiry and its back digits, by any name.
    case(
        "Пришлите номер карты, срок действия и три цифры на обороте",
        "scam",
        "card_data_request",
    ),
    case("Orqasidagi 3 raqamni yozib yuboring, pul tushadi", "scam", "card_data_request"),
    # «Is this you?» as the whole message, with a link or a document.
    case(
        "Привет, это ты на этом фото?? https://bit.ly/x1",
        "scam",
        "photo_lure",
        **KNOWN,
    ),
    case("Bu sizmi?", "scam", "photo_lure", attachment="video_2026.pdf"),
    # An everyday-word brand alone or beside a lure word is the brand.
    case("Kartangizga bonus: https://humo-bonus.uz/karta", "scam", "brand_domain"),
    case("Sovg'a sizni kutmoqda: https://humo.top/bonus", "scam", "brand_domain"),
    # A brand glued to an Uzbek or Russian lure word (a refund, a cashback, a
    # promotion), and the post and the customs by name.
    case(
        "Click: sizga 2 450 000 so'm qaytarildi. Kartangizni ulang "
        "https://click-qaytarish-test.uz/pay",
        "scam",
        "brand_domain",
    ),
    case("Humo keshbek tushdi! https://humo-cashback-test.site", "scam", "brand_domain"),
    case(
        "Bojxona: boj to'lovi to'lanmagan, yuk musodara qilinadi! "
        "https://customs-uz-test.online/boj",
        "scam",
        "brand_domain",
    ),
    case(
        "Посылка задержана на таможне, оплатите пошлину: "
        "https://uzpost-tamozhnya-test.site/pay",
        "scam",
        "brand_domain",
    ),
    case(
        "O'zbekiston pochtasi: manzilni yangilang https://uzpost.uz-delivery-test.cfd/track",
        "scam",
        "brand_domain",
    ),
    # Letters that pass for the brand's own: «rn» for «m».
    case(
        "Bepul Telegram Premium: https://telegrarn-test.org/premium",
        "scam",
        "brand_domain",
    ),
    # A payment domain written into another site's path, and a brand behind a
    # bare address.
    case(
        "Payme: to'lovingiz to'xtatildi https://secure-pay-test.top/payme.uz/confirm?id=1",
        "scam",
        "brand_path",
    ),
    case("Hisobingiz bloklandi: http://91.198.10.17/click/restore", "scam", "brand_path"),
    # A login page on a free host, and a restore page behind a bare address.
    case(
        "Yiwu sklad: yukingizni ko'rish uchun kiring https://yiwu-sklad-login-test.web.app",
        "scam",
        "login_page",
    ),
    case("Tiklash: http://91.198.10.17/account/restore", "scam", "login_page"),
    # A web page as a file from a stranger.
    case(
        "Akt sverka, ochib tekshiring", "scam", "html_file", attachment="Akt_sverki.html"
    ),
    # The one-time code as «digits from the SMS», and the vote scam's «a code
    # will come, send it to me» from a (hijacked) known contact.
    case(
        "Назовите, пожалуйста, цифры из СМС для подтверждения получения груза",
        "scam",
        "code_request",
    ),
    case("SMS dagi raqamlarni yozib yuboring iltimos", "scam", "code_request"),
    case(
        "Jiyanim tanlovda, ovoz bering 🙏 Hozir raqamingizga kod keladi, shuni menga "
        "yuborib qo'ying",
        "scam",
        "code_request",
        **KNOWN,
    ),
    # Money to a card under a pretext, with pressure, from a stranger: a new
    # number «it's me», a customs officer's threat, a payee's changed account.
    case(
        "Salom, bu men, yangi raqamim. Hozir gaplasha olmayman. Shu kartaga tezda "
        "4 mln tashlab yuboring: 9860 0000 1111 2222",
        "scam",
        "money_pretext",
    ),
    case(
        "Bojxona inspektoriman. Yukingizda taqiqlangan tovar chiqdi. 500$ shu kartaga "
        "o'tkazing, aks holda jinoiy ish ochiladi",
        "scam",
        "money_pretext",
    ),
    case(
        "Hello, this is supplier accounting. Our bank account changed, please pay the "
        "invoice to the new account below today. Urgent.",
        "scam",
        "money_pretext",
    ),
    # «Scan this QR to receive the money».
    case(
        "Pul o'tkazdim, qabul qilish uchun shu QR kodni Payme ilovasida skanerlang",
        "scam",
        "qr_receive",
        attachment="qr_test.png",
    ),
    # «Is this you in the video?», «your photo is being spread», with a link.
    case("Bu sizmi videoda? 😱 https://telegra.ph/Video-test-1", "scam", "photo_lure"),
    case("Расмингизни тарқатишибди, қаранг! https://bit.ly/test4", "scam", "photo_lure"),
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
    # A channel to join, a site built, a portfolio: an advert's shape.
    case(
        "Kripto signal kanalimizga qo'shiling, kunlik foyda 30% gacha "
        "https://t.me/kripto_signal_test",
        "spam",
        forwarded=True,
    ),
    case(
        "Бизнесингиз учун сайт ва Телеграм бот ясаб берамиз. "
        "Портфолио: https://webstudio-test.online",
        "spam",
    ),
    # «Do'stlaringizga ham yuboring» and «faqat shu hafta», in Cyrillic.
    case(
        "🎁 Чегирма! Фақат шу ҳафта юк ташиш 3$/кг. Дўстларингизга ҳам юборинг!",
        "spam",
        forwarded=True,
    ),
    # Trading signals, mass ad posting, a money mule, work from home, «only
    # this month».
    case(
        "🚀 Bitcoin signal! Kunlik 40% foyda, VIP kanalga qo'shiling: "
        "https://t.me/+TestSignal Joylar cheklangan!",
        "spam",
        forwarded=True,
    ),
    case(
        "Telegram guruhlarga reklama: 5000+ guruhga tarqatamiz, narxi 50 000 so'm. "
        "Batafsil @test_reklama_bot",
        "spam",
    ),
    case(
        "Ish taklifi: kartangiz orqali pul o'tkazib berasiz, har o'tkazmadan 10% "
        "sizniki. Kuniga 2-3 mln daromad 💰",
        "spam",
    ),
    case(
        "Uydan turib kuniga 1 000 000 so'm ishlang! Tajriba shart emas: "
        "https://ish-test.site123.me",
        "spam",
    ),
    case(
        "📢 Xitoydan yuk 2$/kg dan! Eng arzon narx faqat shu oy! Do'stlaringizga "
        "ulashing https://t.me/test_kargo",
        "spam",
        forwarded=True,
    ),
    # A pretext and money to a card from a stranger, with no pressure: spam,
    # not the hard rule.
    case("Yangi raqamim, shu kartaga 500 ming tashlang 9860 0000 3333 4444", "spam"),
]


# --- a contact with history suddenly unlike themselves ----------------------------

HIJACK: list[dict] = [
    case("Aka, pul kerak edi, qarz berib turing ertaga qaytaraman", "hijack", **KNOWN),
    case("Mana ko'ring: https://bit.ly/3abcde", "hijack", **KNOWN),
    case("", "hijack", attachment="hisob_faktura.html", **KNOWN),
    # Money to my card and a promise to repay, with no trade around it.
    case("Привет, можешь скинуть 2000 на карту? Завтра верну", "hijack", **KNOWN),
    # A goo.gl link that is no Google Maps one is still a short link.
    case("Mana ko'ring: https://goo.gl/TestAb12", "hijack", **KNOWN),
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
    # A named code (a client's GS code, a tariff code, a Wi-Fi password), the
    # writer's own code, a code said to the warehouse keeper at pickup, and a
    # question about how the code comes are no one-time code asked for.
    case("GS412 kodi kelganmi? Kelgan bo'lsa Samarqandga yuboring", "ok", **KNOWN),
    case(
        "SMS keldi yukim omborga keldi deb, lekin kodim esimda yo'q. "
        "Kodni yozib yuboring iltimos",
        "ok",
        prior_messages=6,
        owner_wrote_before=True,
    ),
    case(
        "Груз прибыл на склад. Код получения отправлен вам в СМС — при выдаче "
        "назовите его кладовщику.",
        "ok",
        **KNOWN,
    ),
    case("Скажите, а код для получения груза придёт в смс?", "ok", **KNOWN),
    case("Omborning wifi parolini yozib yuboring, internet yo'q", "ok", **KNOWN),
    case("Пришлите фото: поддоны одноразовые или евро?", "ok", **KNOWN),
    # A document's term and back, an ID card, the writer's own card.
    case(
        "Shartnoma amal qilish muddati 1 yil, to'lov kartaga yoki bank o'tkazmasi "
        "orqali. Imzolab yuboring",
        "ok",
        attachment="Shartnoma_2026.docx",
        **KNOWN,
    ),
    case(
        "ID kartaning orqa tomonini ham rasmga olib yuboring, broker so'rayapti",
        "ok",
        **KNOWN,
    ),
    case(
        "Kartamning amal qilish muddati o'tib ketibdi. Boshqa karta raqamingizni "
        "tashlang",
        "ok",
        **KNOWN,
    ),
    # «Bu sizmi?» under a warehouse photo from a known client, and «you in the
    # photo from the fair?» with a link, are about the cargo.
    case(
        "Bu sizmi? Ombordagi yigitlar yukni shu odamga berib yuborishgan",
        "ok",
        attachment="IMG_20261020_143512.jpg",
        **KNOWN,
    ),
    case(
        "Это вы на фото с выставки? https://www.cantonfair.org.cn/en-US/news",
        "ok",
        **KNOWN,
    ),
    # A company named with an everyday-word brand.
    case(
        "Мы — транспортная компания Humo, наш сайт: https://humo-logistika-test.uz",
        "ok",
    ),
    # Google Maps share links, the marketplaces clients buy from.
    case(
        "Manzil shu",
        "ok",
        buttons=["https://maps.app.goo.gl/AbCdEfGh12345"],
        forwarded=True,
    ),
    case("Lokatsiya: https://maps.app.goo.gl/AbCdEfGh12345", "ok", **KNOWN),
    case(
        "Посчитайте доставку, срочно: https://detail.tmall.com/item.htm?id=1 "
        "https://item.jd.com/2.html https://mobile.yangkeduo.com/goods.html?id=3",
        "ok",
    ),
    # A seller's track numbers on a parcel tracker, and a contact asked to be
    # shared, are no mass mail.
    case(
        "Mana sotuvchi yuborgan treklar: https://t.17track.net/en#nums=YT1 "
        "https://t.17track.net/en#nums=YT2 https://t.17track.net/en#nums=YT3",
        "ok",
        forwarded=True,
    ),
    case(
        "Поделитесь, пожалуйста, контактом вашего брокера, срочно нужно оформить.",
        "ok",
        forwarded=True,
    ),
    # A refund to the client's own card and shipping on credit are trade.
    case(
        "Ortiqcha to'lagan ekanman 85 000 so'm, kartamga tashlab bering iltimos",
        "ok",
        **KNOWN,
    ),
    case(
        "Отгрузите, пожалуйста, в долг, оплату переведём в пятницу. Как обычно.",
        "ok",
        **KNOWN,
    ),
    # «Признателен» holds no «приз», «акциз» no «акция», and free storage
    # days are a freight term.
    case(
        "Пересылаю запрос: нужна доставка из Иу, срочно. Буду признателен за ставку.",
        "ok",
        forwarded=True,
    ),
    case(
        "Ҳамкоримиз сўраяпти: акциз тўлови бўйича маслаҳат керак, срочно.",
        "ok",
        forwarded=True,
    ),
    # A company named with an everyday-word brand and no lure word, a customs
    # broker's site, a shop's own Payme page, a price list on a free host, a
    # LAN camera's login page.
    case("Humo Delivery: yukingiz yo'lda https://humo-delivery-test.uz", "ok"),
    case("Brokerimiz sayti: https://customs-broker-test.uz", "ok"),
    case("Do'kon: https://mening-dokonim-test.shop/pay/payme", "ok"),
    case("Yangi narxlar: https://telegra.ph/Yiwu-narxlar-test", "ok"),
    case("Narxlar: https://narx-test.web.app", "ok"),
    case("Kamera: http://192.168.1.20/login", "ok", **KNOWN),
    # The old goo.gl/maps links open only Google Maps.
    case(
        "Yetkazib berish manzili o'sha eski: https://goo.gl/maps/TestAbc123",
        "ok",
        **KNOWN,
    ),
    # The courier's handover code, said once the cargo is in hand; the number
    # in an SMS to call.
    case(
        "Kuryer yo'lda. Yetkazganda u sizdan SMS dagi 4 xonali kodni so'raydi — faqat "
        "yukni qo'lingizga olgandan keyin ayting",
        "ok",
        **KNOWN,
    ),
    case(
        "Iltimos, SMS dagi raqamga qo'ng'iroq qiling, omborchi javob beradi",
        "ok",
        **KNOWN,
    ),
    # A pretext alone, a known payee's changed account, an account manager,
    # a new invoice: no fraud.
    case("Assalomu alaykum, yangi raqamimdan yozyapman, Akmal GS367. Yuk keldimi?", "ok"),
    case(
        "Bizning hisob raqamimiz o'zgardi, yangi rekvizitlar ilova. Bugundan shu "
        "hisobga to'lang",
        "ok",
        attachment="rekvizit.pdf",
        **KNOWN,
    ),
    case(
        "Hello, this is the new account manager from Yiwu XH. Please pay the deposit to "
        "this account, urgent, production starts today",
        "ok",
    ),
    case(
        "Отправил новый счёт, оплатите, пожалуйста, на карту 8600 0000 5555 6666 сегодня",
        "ok",
    ),
    case(
        "Bojxona inspektori hujjatlarni so'rayapti, invoysni srochno tashlang",
        "ok",
        **KNOWN,
    ),
    # A QR code to pay with, or to collect the cargo with.
    case("To'lov uchun QR kod tashlang, Click orqali skanerlayman", "ok", **KNOWN),
    case(
        "To'lovni oldindan qilish uchun QR kodni skanerlang",
        "ok",
        attachment="qr.png",
        **KNOWN,
    ),
    case("Yukni olish uchun QR kodni omborda skanerlang", "ok", **KNOWN),
    # A job seeker, ordering from home, «only this month» from a client.
    case("Ish taklifi bormi? Haydovchiman, tajriba 10 yil", "ok"),
    case("Uydan turib buyurtma bermoqchiman, srochno kerak", "ok"),
    case("Faqat shu oy ichida to'lab qo'ysam bo'ladimi?", "ok", **KNOWN),
]
