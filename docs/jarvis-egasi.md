# Jarvis — egasi uchun qo'llanma (1–3-bosqichlar)

Jarvis — MIYA'ning mijozlarga sizning nomingizdan javob yozadigan qismi. Hozir faqat
**Telegram** ishlaydi.

> Bu qisqa varaq. To'liq qo'llanma (yangilash, yangi himoyalar, cheklar, kartalar va
> tugmalar, barcha buyruqlar, muammolar va xarajat): [jarvis-qollanma.md](jarvis-qollanma.md).

> **Gmail va WhatsApp hali yo'q.** Ular 4-bosqichda (JV-24…JV-31) qilinadi va hali
> qurilmagan: `make gmail-auth` yo'q, WhatsApp ulash kodi kelmaydi, `.env` da ular uchun
> kalit yo'q. Shu qo'llanmadagi hamma narsa faqat Telegram haqida.

Jarvis bosqichma-bosqich yoqiladi va har bir bosqichni **siz** o'tkazasiz:

| Rejim | Nima bo'ladi | Qanday yoqiladi |
|---|---|---|
| o'chiq | Jarvis hech narsa qilmaydi, MIYA'ning qolgan qismi ishlayveradi | `.env`: `JARVIS_ENABLED=false` |
| kuzatuv | Javob tayyorlab, faqat yozib boradi — sizga ko'rsatmaydi, yubormaydi | `JARVIS_ENABLED=true` qilgandan keyin o'zi shu rejimda boshlanadi |
| qoralama | Har javob sizga karta bo'lib keladi, siz tasdiqlaysiz | botda `/jarvis qoralama` → «✅ Ha, qoralama rejimi» |
| avtomatik | Faqat siz ruxsat bergan javob turlari o'zi yuboriladi | MIYA taklif qiladi → siz «✅ Ha, avtomatik» bosasiz, va `.env`: `JARVIS_MODE=auto` |

Hech bir rejim o'z-o'zidan ko'tarilmaydi. Pastga tushirish darhol: `/jarvis kuzatuv` yoki
hammasini to'xtatish — `/jarvis toxtat`.

## 0. Eng muhim qoida: `.env` dan keyin `make reload`

`.env` dagi Jarvis qatorlarini (`JARVIS_…`, `TELEGRAM_SEND_ENABLED`, `SHEETS_…`,
`EXTRACT_MODEL`, `REASON_MODEL`, `LOG_FORMAT`) o'zgartirgandan keyin serverda:

```
make doctor
make reload
```

`make reload` = `docker compose up -d --force-recreate bot worker userbot`. Uchalasi ham shu
qatorlarni o'qiydi: bot — tugmalar va buyruqlar, worker — kartalar va ishlar, userbot —
mijoz xabarlarini o'qish va yuborish. `docker compose restart` **yetmaydi**: u `.env` ni qayta
o'qimaydi.

## 1. Serverni yangilash

Buyruqlarni **bittadan** yozing va har biridan keyin natijaga qarang. To'liq tartib:
[jarvis-qollanma.md, 2-bo'lim](jarvis-qollanma.md).

1. Ulanish:

   ```
   cd miya
   git fetch
   ```

   Xatosiz o'tsin. «Permission denied (publickey)» desa, bir marta
   `git config core.sshCommand "ssh -i $HOME/.ssh/miya_deploy -o IdentitiesOnly=yes"`,
   keyin yana `git fetch`.

2. Zaxira:

   ```
   make backup
   ```

   Oxiri `error=None)` bilan tugasin. Boshqacha bo'lsa (masalan `error='no_recipient'`) —
   **to'xtang**, `make update` ni yozmang, avval zaxirani tuzating (`BACKUP_AGE_RECIPIENT`).

3. Yangilash:

   ```
   make update
   make doctor
   ```

   - `make update` zaxira oladi, kodni tortadi va bazani o'zi yangilaydi (migratsiyalar). Hech
     qanday SQL yozish kerak emas.
   - «Stopped» qatorlaridan oldin to'xtasa (zaxira yoki `git pull` xatosi): tuzating va yana
     `make update`. Keyin to'xtasa: tuzating, keyin `make up`.
   - `make doctor` «.env faylida N ta yangi sozlama yo'q» deb bitta ⚠️ qator chiqarsa — bu
     normal: ular standart qiymat bilan ishlaydi. Faqat ❌ qolmasin.
   - `.env` da `REPORT_TIME=19:00` bo'lsa, `REPORT_TIME=20:00` qiling, keyin `make reload`.
   - Botda `/holat` — hamma qator ✅.

## 2. Tayyorgarlik — Jarvis yoqilishidan oldin

1. **Shaxsiy chatlar.** `/shaxsiylar` → MIYA oila va do'st bo'lishi mumkin bo'lganlarni
   taklif qiladi, har biri yonida 🔒. Qo'shimcha: `/shaxsiy Ism` (yoki GS kod). Shaxsiy
   chatga MIYA hech qachon o'zi yozmaydi. **Buni birinchi qoralamadan oldin qiling.**
2. **Uslubingiz.** `/uslub yukla` — MIYA eski shaxsiy yozishmalaringizdan uslubingizni
   o'rganadi (faqat o'qiydi). Bir necha daqiqadan keyin `/uslub` → «✅ Faollashtirish».
3. **Bilimlar bazasi.** `/bilim qo'sh Savol | Javob` — ish vaqti, manzil, yo'nalishlar,
   qanday yuklar olinadi, qanday hujjatlar kerak. Narx, chegirma, muddat va va'da yozib
   bo'lmaydi — bular doim sizga keladi. `/bilim` — ro'yxat.
   Yozuv qo'shilgan zahoti qidiruvga tayyor bo'ladi. Faqat ma'no bo'yicha qidiruv
   tayyorlanmay qolsa, Jarvis yoqiq bo'lganda soatlik ish uni qayta tayyorlaydi.
4. **Savollar banki.** `/savolbank` — MIYA mijozga o'zi bera oladigan savollar (qayerdan,
   qayerga, qanday yuk, og'irligi…). Keraksizini 🗑 bilan o'chiring.
5. **Guruhlar.** Jarvis hozir faqat yakkama-yakka (1:1) chatlarda javob tayyorlaydi, guruhlarga
   yozmaydi. `/guruhai` faqat ruxsatni saqlaydi — hozircha uni yoqish shart emas.

## 3. 1-qadam: kuzatuv (bir necha kun)

`.env` da:

```
JARVIS_ENABLED=true
JARVIS_MODE=draft
TELEGRAM_SEND_ENABLED=false
```

Keyin `make doctor` va `make reload`. `USERBOT_ENABLED=true` va `TELETHON_SESSION` to'ldirilgan
bo'lishi kerak — aks holda Jarvis mijoz xabarlarini ko'rmaydi (`make doctor` aytadi).

Botda `/jarvis` → «🤖 Jarvis: kuzatuv». Bu rejimda hech narsa ko'rsatilmaydi va yuborilmaydi.
`/qoralamalar` — MIYA nima deb javob bergan bo'lardi, shuni ko'rib chiqing.

**Xarajat.** Kuzatuv bepul emas: har bir mos mijoz xabari uchun uchta model chaqiriladi
(niyatni aniqlash, javob yozish, tekshirish), kuniga `JARVIS_DAILY_COST_CAP_USD` gacha
(standart 3 $). Kuzatuv kunlarida pastroq qo'yish mumkin, masalan
`JARVIS_DAILY_COST_CAP_USD=1.00`, keyin `make reload`. Chegaraga yetsa, Jarvis o'sha kuni
faqat sizga yuboradi. Xarajat: `/holat` va `/xarajat`.

## 4. 2-qadam: qoralama

Botda:

```
/jarvis qoralama
```

→ «✅ Ha, qoralama rejimi». Endi mijoz yozsa, sizga karta keladi: mijoz xabari, MIYA'ning
javobi, tugmalar. Narx, pul, muddat va va'da savollari ⚠️ eskalatsiya bo'lib keladi.

- `TELEGRAM_SEND_ENABLED=false` bo'lsa — **nusxa rejimi**: kartadagi matnni nusxa olib,
  mijozga o'zingiz yuborasiz. MIYA Telegramga hech narsa yozmaydi. Nusxa rejimidagi
  qoralamalar avtomatikka o'tish hisobiga kirmaydi — buning uchun 3-qadam kerak.
- Kuzatuvga qaytish: `/jarvis kuzatuv` (darhol).
- `/jarvis` «qoralama» demasa va `.env` da chegarani ko'rsatsa — `.env` dagi `JARVIS_MODE`
  ni `draft` qiling, keyin `make reload`.

## 5. 3-qadam: MIYA o'zi yuborsin (sizning tasdig'ingiz bilan)

`.env` da `TELEGRAM_SEND_ENABLED=true`, keyin `make reload`. Endi kartada «✅ Yuborish»
tugmasi bor: bossangiz, MIYA odamga o'xshab biroz kutib, «yozmoqda…» ko'rsatib, xabarni
sizning akkauntingizdan yuboradi. `/holat` da «📤 Telegram yuborish: ✅ ishlayapti».

- `/jarvis toxtat` — hammasini darhol to'xtatadi (MIYA'ning qolgan qismi ishlayveradi).
- `/jarvis yoq` — to'xtatishni bekor qiladi. U rejimni o'zgartirmaydi: kuzatuv rejimida
  bo'lsangiz, MIYA buni aytadi. To'xtatilgan paytda tasdiqlangan xabar `yoq` dan keyin
  bir daqiqagacha kutib keyin ketadi — bu normal.
- Telegram cheklov qo'ysa, Jarvis o'sha kanalda o'zi to'xtaydi va sizga aytadi.

## 6. 4-qadam: avtomatik (faqat ruxsat berilgan javob turlari)

Faqat uch tur o'zi yuborilishi mumkin: salomlashish, ma'lumot so'rash, bilimlar bazasidagi
umumiy savol. Biror tur qoralama rejimida kamida 7 kun, 30 ta qoralama va 90% o'zgartirmasdan
tasdiqlangandan keyin MIYA «🎓 … Endi bunday javoblarni MIYA o'zi yuborsinmi?» deb so'raydi.
«✅ Ha, avtomatik» bossangiz — o'sha tur o'zi yuboriladi, lekin faqat `.env` da
`JARVIS_MODE=auto` bo'lsa (keyin `make reload`). `JARVIS_MODE=auto` ni oldinroq qo'yish
xavfsiz: u faqat yuqori chegara, sizning tapingizsiz hech narsa avtomatik bo'lmaydi.

- `/jarvis rejim` — har tur qaysi rejimda va qancha qolganini ko'rsatadi.
- Har avtomatik javobdan keyin «↩️ Bu xato edi» keladi — bossangiz, o'sha tur yana
  qoralamaga qaytadi.
- Narx, chegirma, to'lov, muddat, va'da, kafolat, shikoyat va «kutib turing» javobi —
  **hech qachon** avtomatik emas.

## 7. Google Sheets (ixtiyoriy)

MIYA va'dalar, vazifalar, mijozlar va cheklarni sizning jadvalingizga nusxalaydi (faqat bir
tomonga: jadvalda yozganingiz MIYA'ga qaytmaydi).

1. console.cloud.google.com → yangi loyiha → «APIs & Services» → «Library» →
   «Google Sheets API» → «Enable».
2. «IAM & Admin» → «Service accounts» → «Create service account» → nom bering → «Done».
   Yaratilgan hisobni oching → «Keys» → «Add key» → «Create new key» → JSON → fayl yuklanadi.
3. Faylni serverga ko'chiring va egasini to'g'rilang:

   ```
   scp kalit.json server:~/miya/secrets/sheets_service_account.json
   sudo chown 10001:10001 ~/miya/secrets/sheets_service_account.json
   ```

   (`chown` qilmasangiz, MIYA uni o'qiy olmaydi: `make doctor` «… faylini MIYA o'qiy
   olmayapti» deb ogohlantiradi, `/holat` esa «📊 Google Sheets yangilanmayapti:
   PermissionError» deydi; `make up` ham egasini to'g'rilaydi.)
4. sheets.google.com da yangi jadval oching. Manzildagi uzun id ni oling:
   `docs.google.com/spreadsheets/d/<id>/edit`.
5. `.env` da `SHEETS_ENABLED=true` va `SHEETS_SPREADSHEET_ID=<id>`, keyin `make doctor` —
   u «ℹ️ Google jadvalni shu manzilga «Muharrir» qilib ulashing: …» deb xizmat hisobining
   manzilini chiqaradi. Jadvalda «Share» → shu manzil → «Muharrir» (Editor).
6. `make reload`. Bir daqiqadan keyin jadvalda varaqlar paydo bo'ladi.

## 8. Loglar

Standartda loglar JSON qatorlar (`LOG_FORMAT=json`) — mashina uchun qulay. O'zingiz
o'qimoqchi bo'lsangiz: `.env` da `LOG_FORMAT=text`, keyin `make reload` va `make logs`.

## 9. Muammo bo'lsa

- **`/jarvis` hali ham «kuzatuv»** — `/jarvis qoralama` ni bosdingizmi? `.env` da
  `JARVIS_MODE=shadow` bo'lsa, MIYA buni aytadi: `JARVIS_MODE=draft`, `make reload`.
- **Hech narsa bo'lmayapti** — `/holat` → «Telegram o'quvchi» ✅ bo'lsin; `make doctor`
  Jarvis haqida ⚠️ chiqarmasin.
- **`/holat` da «narxi noma'lum»** — `EXTRACT_MODEL` yoki `REASON_MODEL` o'zgartirilgan.
  `.env` ga `EXTRACT_MODEL_PRICE=<kirish>,<chiqish>` (yoki `REASON_MODEL_PRICE`) yozing,
  USD / 1M token, keyin `make reload`. Narx noma'lum bo'lsa Jarvis faqat sizga yuboradi.
- **Darhol to'xtatish kerak** — `/jarvis toxtat`. Butunlay o'chirish: `.env` da
  `JARVIS_ENABLED=false`, `make reload`.
