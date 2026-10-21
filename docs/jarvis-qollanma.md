# MIYA va Jarvis — egasi uchun to'liq qo'llanma (1–3-bosqichlar)

Bu qo'llanma serverda ishlab turgan MIYA'ni yangilash, yangi himoyalarni ishlatish va
Jarvis'ni (mijozlarga sizning nomingizdan javob yozadigan qism) xavfsiz, bosqichma-bosqich
yoqish haqida. Dasturchi bo'lish shart emas: har bir qadam raqamlangan, serverda yoziladigan
buyruqlar alohida qutida berilgan.

> **Muhim: Gmail va WhatsApp hali yo'q.** Ular 4-bosqichda (JV-24…JV-31) qilinadi va hali
> qurilmagan. `make gmail-auth` yo'q, WhatsApp ulash kodi kelmaydi, `.env` da ular uchun
> sozlama yo'q. `docs/jarvis-plan.md` dagi Gmail va WhatsApp qadamlarini **qilmang**. Bu
> qo'llanmadagi hamma narsa faqat **Telegram** haqida.

Ikki xil joyda ish qilasiz:

- **Serverda** (SSH orqali kirib): `make …` buyruqlari va `.env` faylini tahrirlash.
- **Telegram'da, MIYA botida**: `/jarvis`, `/holat` va boshqa buyruqlar, tugmalar.

---

## 0. Eng muhim qoida: `.env` ni o'zgartirdingizmi — `make reload`

`.env` dagi istalgan qatorni o'zgartirgandan keyin serverda doim:

```
make doctor
make reload
```

- `make doctor` — `.env` ni tekshiradi. ❌ bo'lsa, ❌ yo'qolguncha tuzating. ⚠️ va ℹ️ qatorlar
  ogohlantirish va maslahat, ishga xalaqit bermaydi.
- `make reload` — bot, worker va userbot'ni qayta yaratadi
  (`docker compose up -d --force-recreate bot worker userbot`). Uchalasi ham Jarvis
  sozlamalarini o'qiydi: bot — tugmalar va buyruqlar, worker — kartalar va ishlar, userbot —
  mijoz xabarlarini o'qish va Telegram'ga yuborish.
- `docker compose restart` **yetmaydi**: u `.env` ni qayta o'qimaydi.

`.env` ni qanday tahrirlash:

1. Serverda: `cd miya` va `nano .env`.
2. Kerakli qatorni toping (`Ctrl+W` — qidirish), qiymatini o'zgartiring.
3. Saqlash: `Ctrl+O`, keyin `Enter`. Chiqish: `Ctrl+X`.
4. `make doctor`, keyin `make reload`.

Qator `.env` da umuman bo'lmasa, uni oxiriga yozib qo'yish kifoya (masalan
`JARVIS_ENABLED=true`). Yozilmagan sozlama standart qiymat bilan ishlaydi.

---

## 1. Nima yangi

**Jarvis'siz, darhol ishlaydigan narsalar (1-bosqich):**

- **Xavfli fayl himoyasi.** Kimdir APK, .exe yoki arxiv yuborsa, MIYA uni yuklab olmaydi,
  ochmaydi va sizni bir marta ogohlantiradi.
- **Firibgarlik va spam himoyasi.** Soxta bank saytiga havola, SMS kod so'rash, «rasmda
  sizmi?» kabi aldovlar va buzilgan akkauntdan kelgan g'alati xabarlar haqida ogohlantiradi.
- **Cheklar.** Mijoz yuborgan to'lov cheki rasmini o'qib, sizga karta qilib ko'rsatadi va
  bank SMS'i yoki Payme bilan solishtiradi.
- **Soatli muddatlar.** «Ertaga soat 14:00 gacha» kabi va'da va vazifalar muddatdan bir soat
  oldin eslatiladi.
- **Tezkor savollar.** «Bugun kimga nima va'da bergandim?», «Kecha kimlar yozdi?» — model
  chaqirilmasdan bazadan javob beriladi.
- **Kechki xulosa endi 20:00 da** (oldin 19:00 edi).
- **Google Sheets** (ixtiyoriy): va'dalar, mijozlar va cheklar jadvalingizga nusxalanadi.

**Jarvis (2–3-bosqich, siz yoqmaguningizcha o'chiq):**

- Mijoz Telegram'da shaxsiy chatda yozsa, MIYA javob qoralamasini tayyorlaydi.
- Narx, chegirma, to'lov, muddat, va'da, shikoyat kabi savollarni **o'zi hal qilmaydi** —
  sizga «⚠️ eskalatsiya» bo'lib keladi.
- Siz tasdiqlagan javobni MIYA sizning Telegram akkauntingizdan yuboradi (buni alohida
  yoqasiz).
- Faqat oddiy javob turlari (salomlashish, ma'lumot so'rash, bilimlar bazasidagi umumiy
  savol) va faqat siz ruxsat bersangiz, keyinchalik o'zi yuboriladi.
- Botga «Sardorga yozib yubor …» deb buyruq berish mumkin — yuborishdan oldin tasdiqlaysiz.

Jarvis'ning rejimlari:

| Rejim | Botda ko'rinishi | Nima bo'ladi |
|---|---|---|
| o'chiq | «o'chiq» | Jarvis hech narsa qilmaydi. MIYA'ning qolgan qismi odatdagidek ishlaydi. |
| kuzatuv | «kuzatuv (faqat yozib boradi)» | Javob tayyorlaydi, lekin sizga ko'rsatmaydi va yubormaydi. `/qoralamalar` da ko'rasiz. |
| qoralama | «qoralama (har javobni siz tasdiqlaysiz)» | Har javob sizga karta bo'lib keladi, siz tasdiqlaysiz. |
| avtomatik | «avtomatik» | Faqat siz ruxsat bergan javob turlari o'zi yuboriladi; qolgani baribir karta. |

Hech bir rejim o'z-o'zidan **yuqoriga** ko'tarilmaydi. Har bir ko'tarilish — sizning
tugmangiz yoki `.env` dagi o'zgarishingiz.

---

## 2. Serverni yangilash

1. Serverga kiring va MIYA papkasiga o'ting. O'zgarish yo'qligini, `master` da
   ekaningizni va GitHub'ga ulanish ishlashini tekshiring:

   ```
   cd miya
   git status
   git branch
   git rev-parse --short HEAD
   git fetch
   ```

   `git status` «nothing to commit» desin, `git branch` esa `* master` ni ko'rsatsin.
   `git rev-parse --short HEAD` chiqargan qisqa kodni (masalan `8328214`) yozib qo'ying —
   bu hozirgi versiya, kerak bo'lsa unga qaytasiz. `git fetch` xatosiz tugasin. `git fetch` «Permission denied (publickey)» desa, o'rnatishda
   yaratilgan kalitni git'ga bir marta ko'rsating va qaytadan `git fetch`:

   ```
   git config core.sshCommand "ssh -i $HOME/.ssh/miya_deploy -o IdentitiesOnly=yes"
   git fetch
   ```

2. Avval zaxirani alohida oling va natijasiga qarang:

   ```
   make backup
   ```

   Ekranda `BackupResult(path=PosixPath('/data/backups/miya-…'), …, error=None)` chiqsin.
   Oxiri `error=None` emas bo'lsa, yangilashni boshlamang. `error='no_recipient'` bo'lsa —
   [o'rnatishning 4-qadami](ornatish.md) («Zaxira kaliti»): `make backup-key`, chiqqan
   `BACKUP_AGE_RECIPIENT=age1…` qatorini `.env` ga yozing, keyin yana `make backup`. Boshqa
   xato bo'lsa, matnini dasturchiga yuboring. Bu zaxira
   serverdagi `data/backups/` papkasiga yoziladi, Telegram'ga yuborilmaydi — Telegram'ga
   zaxira fayli har kecha 03:30 dagi odatiy zaxiradan keyin keladi.

3. Yangilang:

   ```
   make update
   ```

   Bu buyruq o'zi: zaxira nusxa oladi → yangi kodni tortadi → bot, worker va userbot'ni
   to'xtatadi → `make doctor` ni o'tkazadi → bazani yangilaydi (migratsiyalar
   0022 dan 0028 gacha, ketma-ket, avtomatik; bir necha soniya) → hammasini qayta yoqadi.
   **Hech qanday SQL yozish kerak emas.**

   - O'rtada `make doctor` «✅ Hammasi joyida — endi: make up» (keyingi yangilashlarda
     «✅ Hammasi joyida — make update davom etadi, hech narsa yozmang.») deydi. Hech narsa
     yozmang — `make update` o'zi davom etadi.
   - Oxirida «Yangilandi. Botda /holat ni tekshir.» chiqishi kerak.
   - `make update` «Stopped» qatorlaridan **oldin** to'xtasa (zaxira yoki `git pull`
     xatosi): MIYA eski versiyada ishlab turibdi. Xatoni tuzating va yana `make update`
     ni yozing (`make up` emas — u yangilamaydi).
   - «Stopped» qatorlaridan **keyin** istalgan xato bilan to'xtasa (`make doctor` dagi ❌,
     yig'ish xatosi yoki migratsiya xatosi): bot, worker va userbot **to'xtagan holda
     qoladi**. Xatoni tuzating, keyin `make up` ni yozing (u doctor'ni, migratsiyalarni va
     qayta yoqishni o'zi bajaradi).
   - Migratsiya xatosi bo'lsa baza eski holatida qoladi (hech narsa yo'qolmaydi). Eski
     versiyaga qaytish: `git checkout <1-qadamda yozib olgan kod>` va `make up`; xato
     matnini dasturchiga yuboring. Tuzatilgandan keyin: `git checkout master` va
     `make update`.

4. Tekshiring:

   ```
   make doctor
   ```

   - ❌ qolmasin.
   - «⚠️ .env faylida N ta yangi sozlama yo'q — ular standart qiymat bilan ishlaydi …» degan
     qator (yoki yetishmagani 3 tadan kam bo'lsa, har biri uchun «⚠️ … .env faylida yo'q»)
     — **normal**. Ular yangi sozlamalar, standart qiymat bilan ishlaydi. Kerak
     bo'lganini keyin `.env.example` dan ko'chirasiz.
   - «ℹ️ REPORT_TIME=19:00 — yangi standart 20:00 …» — keyingi qadam.

5. Kechki xulosa vaqti. `.env` da `REPORT_TIME=19:00` deb yozilgan bo'lsa, uni
   `REPORT_TIME=20:00` qiling (yoki qatorni o'chiring), keyin:

   ```
   make reload
   ```

6. Botda `/holat` → hamma qator ✅ bo'lsin. `/ertalab` → ertalabki xulosa chiqsin.

7. Ixtiyoriy `.env` qatorlari (keyin `make doctor` va `make reload`):

   - `SAFETY_EXTRA_SAFE_DOMAINS=mycompany.uz` — o'zingiz tekshirgan saytlar (vergul bilan).
     Ularga havola hech qachon «soxta sayt» deb aytilmaydi. Lekin ular orqali kelgan dastur
     yoki arxiv baribir rad etiladi.
   - `PAYMENT_OWN_CARDS=1234,5678` — o'z kartalaringizning oxirgi 4 raqami (MIYA bank
     SMS'laridan o'zi ko'rgan kartalarga qo'shimcha). Chekdagi karta sizniki bo'lsa-yu,
     bankdan xabar kelmasa, chek «shubhali» deyiladi; o'z kartalaringiz orasidagi o'tkazma
     daromad va xarajatga qo'shilmaydi.
   - `LOG_FORMAT=text` — `make logs` o'qishga qulay bo'ladi. Standart `json` (bir qatorda bitta
     JSON) — mashina uchun.

---

## 3. Darhol ishlaydigan himoyalar

Bularni yoqish shart emas — yangilangandan keyin o'zi ishlaydi. Jarvis o'chiq bo'lsa ham.

### 3.1. Xavfli fayllar (APK, .exe, arxiv)

Kimdir chatga o'rnatiladigan dastur yoki arxiv yuborsa, MIYA uni yuklab olmaydi va sizga
bir marta yozadi:

> ⚠️ Akmal sizga «hujjat.apk» faylini yubordi (Telegram). Bu o'rnatiladigan dastur yoki
> arxiv — xavfli bo'lishi mumkin. Ochmang va o'rnatmang. MIYA uni yuklab olmadi.
> Tanish odamdan kelgan bo'lsa ham ehtiyot bo'ling. Shubha bo'lsa, unga qo'ng'iroq qilib
> so'rang.

- APK bo'lsa, qo'shimcha: «MIYA ilovasi faqat MIYA botidan keladi; boshqa hech kimdan kelgan
  APK faylni o'rnatmang.»
- Botga o'zingiz shunday fayl yuborsangiz ham, MIYA uni ochmaydi: ichidagi ma'lumot kerak
  bo'lsa, PDF yoki rasm qilib yuboring.
- Makrosli hujjat (Word/Excel) faqat matn sifatida o'qiladi: «… makrosli hujjat. MIYA uni
  faqat matn sifatida o'qidi. Kompyuterda ochsangiz, makrosni yoqmang.»

### 3.2. Firibgarlik va spam

Firibgarlikka o'xshagan xabar uchun:

> 🚫 Akmaldan kelgan xabar firibgarlikka o'xshaydi (bank yoki to'lov saytiga o'xshatilgan
> soxta sayt). Javob bermang, havolani ochmang.

Tanish odam odatdagidan boshqacha yozsa (akkaunti o'g'irlangan bo'lishi mumkin):

> ⚠️ Akmalning akkaunti buzilgan bo'lishi mumkin: odatda bunday yozmaydi. Unga qo'ng'iroq
> qilib tekshiring, pul o'tkazmang.

- Bir chat bo'yicha kuniga ko'pi bilan bitta shunday ogohlantirish keladi. Tungi
  «jim soatlar»da (`QUIET_HOURS`, standart 23:30–07:30) ovozsiz keladi.
- Ertalabki xulosada: «🚫 Kecha N ta spam/firibgar xabar e'tiborsiz qoldirildi.»
- Jarvis bunday xabarlarga hech qachon javob yozmaydi.

### 3.3. To'lov cheklari va bank tekshiruvi

Mijoz chatga to'lov cheki rasmini yuborsa (yoki o'zingiz botga yuborsangiz), MIYA uni o'qib
karta yuboradi:

```
🧾 Chek · Akmal · Telegram
Chekda: 1.5 mln so'm · bugun 14:05
To'lovchi: … → … ****1234
Bank tasdig'i: ⏳ kutilmoqda
Summa rasmdan o'qildi; hisobga faqat bank SMS yoki Payme tasdig'i bilan yoziladi.
```

Oxirgi qator keyin o'zi yangilanadi:

- «✅ Chek bankda tasdiqlandi: …» — chekdagi vaqtdan 30 daqiqa ichida shu summada bankdan
  kirim bor (`RECEIPT_MATCH_MINUTES`).
- «⏳ Bankdan tasdiq hali yo'q (SMS kelmaydigan hisob bo'lishi mumkin).» — 60 daqiqa
  o'tdi, bankdan xabar yo'q (`RECEIPT_BANK_WAIT_MINUTES`).
- «⚠️ Chek shubhali: … Pul kartangizga tushganini ilovadan tekshiring.» — masalan, bu chek
  avval ham yuborilgan, chekdagi sana eski (72 soatdan ortiq, `RECEIPT_STALE_HOURS`) yoki
  kelajakda, yoki karta sizniki, lekin bankdan xabar kelmadi.

Kartaning ostida ikki tugma bor: **«✅ Pul keldi»** va **«🚩 Soxta»** — sizning oxirgi
so'zingiz. **Hech bir tugma pulni hisobga yozmaydi.** Pul hisobga avvalgidek faqat bank SMS'i,
Payme yoki o'zingizning so'zlaringiz bilan yoziladi.

- Cheklar o'qilishi uchun chatda rasm o'qish yoqilgan bo'lishi kerak. Standartda hamma chatda
  yoqilgan. Tekshirish: botda `/chats` → chat yonida «👁✅» bo'lsin («👁❌» bo'lsa bosing).
- Tunda (jim soatlarda) chek kartasi ertalabgacha kutadi. Tunda ham ovozsiz kelsin desangiz:
  `.env` da `MONEY_RECEIPTS_SILENT_AT_NIGHT=true`.
- Rasm aniq bo'lmasa: «🧾 Chek o'qilmadi — rasm aniq emas. /tekshir ro'yxatiga qo'ydim.»

### 3.4. Soatli muddatlar

Va'da yoki vazifada aniq soat bo'lsa (masalan «Akmalga ertaga soat 14:00 gacha hujjatni
yuboraman»), MIYA muddatdan 60 daqiqa oldin eslatadi (`PROMISE_REMIND_LEAD_MINUTES`):

> ⏰ 14:00 da muddat: Akmalga va'da — hujjatni yuborish

Jim soatlarda eslatma yuborilmaydi.

### 3.5. Botga tezkor savollar

Botga yozing yoki ovozli xabar yuboring:

- «Bugun kimga nima va'da bergandim?»
- «Kecha kimlar yozdi?»

Kun so'zi bitta bo'lishi kerak: `bugun`, `kecha`, `o'tgan kuni` yoki hafta kuni
(`dushanba` …). Javob to'g'ridan-to'g'ri bazadan keladi, model chaqirilmaydi. Savolga
boshqa so'z qo'shsangiz (masalan ism: «Bugun Akmalga nima va'da bergandim?»), tezkor javob
ishlamaydi — xabar odatdagi yo'l bilan ishlanadi (kerak bo'lsa model chaqiriladi).

### 3.6. Ertalabki va kechki xulosa

- Ertalabki xulosa — 09:00 da (yoki `/ertalab`).
- Kechki xulosa — 20:00 da (`REPORT_TIME`, yoki `/hisobot`).
- Jarvis yoqilganda ularda Jarvis bo'limlari ham bo'ladi (6-bo'limga qarang).

---

## 4. Google Sheets ulash (ixtiyoriy)

MIYA uchta varaqni sizning Google jadvalingizga nusxalaydi: **Tasks_Promises** (va'dalar va
vazifalar), **Client_FollowUps** (kuzatish kerak bo'lgan mijozlar), **Receipts** (cheklar).
Bu **bir tomonlama**: jadvalda nimanidir o'zgartirsangiz, MIYA'ga qaytmaydi va yozuv
o'zgarganda ustidan yoziladi. Haqiqat — MIYA bazasi.

**A. Google Cloud'da (kompyuterda, brauzerda):**

1. console.cloud.google.com ga kiring → yuqorida loyiha tanlash → «New project» → nom
   bering (masalan «miya») → «Create».
2. «APIs & Services» → «Library» → «Google Sheets API» ni qidiring → «Enable».
3. «IAM & Admin» → «Service accounts» → «Create service account» → nom bering → «Done».
4. Yaratilgan hisobni oching → «Keys» → «Add key» → «Create new key» → «JSON» → «Create».
   Kompyuteringizga `.json` fayl yuklanadi. Uni hech kimga bermang.

**B. Kalitni serverga ko'chirish (kompyuterdan):**

5. Faylni serverdagi `miya/secrets/` papkasiga aynan shu nom bilan ko'chiring:

   ```
   scp yuklangan-kalit.json SERVER:~/miya/secrets/sheets_service_account.json
   ```

   (`SERVER` o'rniga serveringiz manzili; papka boshqa joyda bo'lsa, yo'lni moslang.)

6. Serverda fayl egasini to'g'rilang (MIYA uni o'qiy olishi uchun):

   ```
   cd miya
   sudo chown 10001:10001 secrets/sheets_service_account.json
   ```

   Buni qilmasangiz, `make doctor` «⚠️ … faylini MIYA o'qiy olmayapti (egasi root)» deb
   ogohlantiradi, `/holat` esa «📊 Google Sheets yangilanmayapti: PermissionError» deydi.
   `make up` ham egasini o'zi to'g'rilaydi.

**C. Jadval:**

7. sheets.google.com da yangi bo'sh jadval oching. Brauzer manzilidan uzun id ni oling:
   `docs.google.com/spreadsheets/d/`**`<id>`**`/edit`.

**D. `.env` va tekshirish (serverda):**

8. `.env` ga yozing:

   ```
   SHEETS_ENABLED=true
   SHEETS_SPREADSHEET_ID=<7-qadamdagi id>
   ```

9. Tekshiring:

   ```
   make doctor
   ```

   Doctor kalitni o'qib, «ℹ️ Google jadvalni shu manzilga «Muharrir» qilib ulashing: …» deb
   xizmat hisobining e-mail manzilini chiqaradi.

10. Jadvalda «Share» (Ulashish) → shu e-mail manzil → «Editor» (Muharrir) → «Send».
11. Ishga tushiring:

    ```
    make reload
    ```

12. Bir-ikki daqiqadan keyin jadvalda uchta varaq paydo bo'ladi (har 60 soniyada yangilanadi,
    `SHEETS_SYNC_SECONDS`). Botda `/holat` da «📊 Sheets: oxirgi yangilanish …» qatori
    chiqadi.

Ishlamasa, 10 daqiqadan keyin `/holat` sababini aytadi: «📊 Google Sheets yangilanmayapti:
…». Sabablar va nima qilish — 10-bo'limda.

---

## 5. Jarvis'ni tayyorlash (yoqishdan oldin)

Bularni Jarvis'ni yoqishdan **oldin** qiling. Hammasi botda.

### 5.1. Shaxsiy chatlar — eng birinchi

Oila va do'stlaringiz chatiga MIYA hech qachon o'zi yozmasligi kerak.

1. `/shaxsiylar` — belgilangan shaxsiy chatlar va MIYA bazadan topgan «💡 Oila yoki do'st
   bo'lishi mumkin» takliflari. Har birining yonida 🔒 — bir bosishda belgilanadi. Siz
   bosmaguningizcha hech kim belgilanmaydi.
2. Ro'yxatda yo'qlarini qo'lda belgilang:

   ```
   /shaxsiy Onam
   /shaxsiy GS367
   /shaxsiy Oila guruhi
   ```

   (ism, mijoz kodi yoki oilaviy guruh nomi). Javob: «🔒 … shaxsiy deb belgilandi. MIYA bu
   chatga hech qachon o'zi yozmaydi (o'qish davom etadi).»
3. Shaxsiydan chiqarish: allaqachon shaxsiy bo'lgan odamga yana `/shaxsiy Ism` yozing yoki
   `/shaxsiylar` da 🔓 bosing → MIYA so'raydi → «✅ Ha, chiqar».

### 5.2. Guruhlar

Hozirgi versiyada Jarvis **faqat shaxsiy (1:1) chatlarga** javob tayyorlaydi; guruhlarga
javob tayyorlamaydi. `/guruhai Guruh nomi` — guruh uchun ruxsat sozlamasi (standartda
o'chiq). O'chirish darhol bo'ladi, yoqish tasdiq tugmasini so'raydi. Uni o'chiq qoldiring.
`/guruhai` (nomsiz) — ruxsat yoqilgan guruhlar ro'yxati.

### 5.3. Yozish uslubingiz

MIYA sizning yozish uslubingizni (salomlashish, murojaat, uzunlik, emoji) eski shaxsiy
yozishmalaringizdan o'rganadi. Faqat o'qiydi, hech kimga hech narsa yozmaydi. Shaxsiy deb
belgilangan chatlar o'qilmaydi.

1. Userbot ishlayotgan va ulangan bo'lsin (`/holat` da «✅ Telegram o'quvchi — ulangan»).
2. Botda: `/uslub yukla`.
3. Kuting. Jarvis yoqilgan bo'lsa, «✅ Uslub tayyor: N ta xabar, M ta misol. /uslub» keladi.
   Jarvis hali o'chiq bo'lsa, bir necha daqiqadan keyin `/uslub` ni o'zingiz oching.
4. `/uslub` → uslub kartasi chiqadi. To'g'ri bo'lsa **«✅ Faollashtirish»** bosing. Qayta
   tuzish: «🔄 Qayta tuzish».

Faollashtirmaguningizcha qoralamalar sizning uslubingizda yozilmaydi. Xabarlar 50 tadan
kam bo'lsa (`STYLE_MIN_SAMPLES`), MIYA uslub tuzmaydi va buni aytadi.

### 5.4. Bilimlar bazasi (`/bilim`)

Bu — Jarvis mijozga **o'zi** ayta oladigan yagona ma'lumotlar. Bu yerda yo'q narsani Jarvis
o'zicha da'vo qilmaydi.

Qo'shish (savol | javob):

```
/bilim qo'sh Ish vaqtingiz qanday? | Dushanbadan shanbagacha, 9:00 dan 18:00 gacha.
/bilim qo'sh Ofisingiz qayerda? | Ofisimiz Toshkentda, Chilonzor tumanida.
/bilim qo'sh Qanday yuklarni olasiz? | Kiyim-kechak, maishiy texnika va qurilish mollari.
/bilim qo'sh Qanday hujjatlar kerak? | Pasport nusxasi va yuk ro'yxati (invoys).
```

Savolsiz yozsangiz (faqat javob), u «fakt» bo'lib saqlanadi:
`/bilim qo'sh Biz Xitoydan Toshkentga yuk tashiymiz.`

**Rad etiladi** (bunday savollar doim sizga keladi):

- narx, chegirma, to'lov sharti, to'lov tasdig'i, muddat, va'da, kafolat;
- mijozlarning shaxsiy ma'lumotlari: telefon, karta raqami, e-mail, @username, mijoz kodi
  yoki yuk xati, tanish odamning ismi.

Ish vaqti kabi pul bilan bog'liq bo'lmagan raqamlar mumkin.

Boshqa buyruqlar:

```
/bilim
/bilim tuzat b12 Yangi savol | Yangi javob
/bilim o'chir b12
/bilim taklif
```

- `/bilim` — ro'yxat (har yozuvning raqami bor: `b12`).
- `/bilim taklif` — MIYA sizning eski javoblaringizdan tuzgan takliflar. Har biri ostida
  «✅ Qo'shish» / «✖️ Kerak emas». Siz bosmaguningizcha taklif ishlatilmaydi. Takliflar
  haftasiga bir marta, faqat Jarvis yoqilganda tuziladi.
- Eslatma: yozuv qo'shilganda MIYA uni darhol ma'no bo'yicha qidirishga ham tayyorlaydi.
  Bu o'sha payt ishlamasa (masalan, qidiruv modeli band bo'lsa), yozuv vaqtincha faqat so'z
  bo'yicha topiladi; ma'no bo'yicha qidiruv Jarvis yoqilgan bo'lsa soatlik ishda
  tayyorlanadi.

### 5.5. Savollar banki (`/savolbank`)

Jarvis mijozga **o'zi** (sizning tugmangizsiz) faqat shu savollarni bera oladi. Boshlang'ich
ro'yxatda yuk bo'yicha savollar bor (qayerdan, qayerga, qanday yuk, necha kilo, necha kub,
necha joy, GS kod, kim qabul qiladi, hujjatlar, olib ketish manzili) — lotin, kirill va rus
tilida.

```
/savolbank
/savolbank o'chir 3
/savolbank qo'sh Yuk qachon tayyor bo'ladi?
```

- `/savolbank` — raqamlangan ro'yxat; ostida har savol uchun «🗑 1», «🗑 2» … tugmalari.
- `o'chir 3` — 3-savolni darhol o'chiradi. Boshlang'ich savollardan biri bo'lsa, uning
  lotin, kirill va rus tilidagi nusxalari birga o'chadi.
- `qo'sh …` — MIYA tekshiradi va so'raydi → «✅ Qo'shish» (yoki «✖️ Kerak emas»). Bitta
  savol bo'lishi kerak (savol so'zi yoki «-mi» bilan, 120 belgigacha); narx, muddat, va'da,
  raqam, havola yoki ism bo'lmasin.

Bankda yo'q savol ham qoralamada bo'lishi mumkin — u faqat siz tasdiqlaganda ketadi.

### 5.6. Model narxi

Jarvis kunlik xarajatni hisoblaydi. Buning uchun modellar narxi ma'lum bo'lishi kerak.
`.env` dagi `EXTRACT_MODEL` va `REASON_MODEL` ga **tegmagan** bo'lsangiz — hammasi joyida
(standart `claude-haiku-4-5` va `claude-sonnet-5`). O'zgartirgan bo'lsangiz va model
`claude-haiku-4-5`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-opus-5` dan biri bo'lmasa,
narxini yozing (USD, 1 million token uchun, «kirish,chiqish»):

```
EXTRACT_MODEL_PRICE=1.00,5.00
REASON_MODEL_PRICE=3.00,15.00
```

Narx noma'lum bo'lsa, Jarvis xarajat chegarasiga yetgan deb hisoblaydi va faqat sizga
yo'naltiradi. `make doctor` bu haqda ⚠️ bilan aytadi.

---

## 6. Jarvis'ni bosqichma-bosqich yoqish

Tartib: **kuzatuv → qoralama → Telegram'dan yuborish → avtomatik**. Har bir bosqichda bir
necha kun turing.

### 6.1. 1-qadam: kuzatuv (1–2 kun)

Bu rejimda Jarvis javob tayyorlaydi, lekin sizga **ko'rsatmaydi** va **yubormaydi**.

1. `.env` da:

   ```
   JARVIS_ENABLED=true
   JARVIS_MODE=draft
   TELEGRAM_SEND_ENABLED=false
   ```

   - `JARVIS_ENABLED=true` — Jarvis'ning asosiy kaliti.
   - `JARVIS_MODE` — **yuqori chegara**. Jarvis bundan yuqori rejimga hech qachon chiqmaydi.
     `draft` qo'yib qo'ysangiz, keyingi bosqichga (qoralama) botdagi bitta buyruq bilan
     o'tasiz — serverga qayta kirish shart bo'lmaydi. Bu kuzatuvni buzmaydi: Jarvis
     birinchi marta baribir **kuzatuv** rejimida boshlanadi va siz `/jarvis qoralama`
     bosmaguningizcha shu rejimda qoladi.
   - `USERBOT_ENABLED=true` va `TELETHON_SESSION=…` to'ldirilgan bo'lishi kerak (o'rnatishdagi
     `make userbot-login`). Aks holda Jarvis mijoz xabarlarini ko'rmaydi — `make doctor`
     aytadi.

2. Serverda:

   ```
   make doctor
   make reload
   ```

3. Botda tekshiring:
   - `/jarvis` → «🤖 Jarvis: kuzatuv (faqat yozib boradi)» va «👀 Hozir kuzatuv rejimi:
     qoralamalar sizga ko'rsatilmaydi va hech narsa yuborilmaydi. Qoralama rejimi:
     /jarvis qoralama». Shu yerda: «Kuzatuv: bugun N ta javob tayyorlanardi (M tasi sizga
     yo'naltirilardi).»
   - `/holat` → «🤖 Jarvis: kuzatuv …» qatorlari va «✅ Javob beruvchi — oxirgi ishga
     tushish: …».
4. Har kuni `/qoralamalar` ni oching. «🧪 Kuzatuv qoralamalari:» ostida: mijoz nima yozdi →
   MIYA nima deb javob bergan bo'lardi [javob turi]. Hech biri yuborilmagan. Javoblar
   sizga yoqyaptimi — shunga qarang.

**Xarajat.** Kuzatuv bepul emas: har bir mos mijoz xabari uchun modellar chaqiriladi (javob
turini aniqlash, javob yozish, tekshirish), kuniga `JARVIS_DAILY_COST_CAP_USD` gacha
(standart 3 $). Xohlasangiz kuzatuv kunlarida pastroq qo'ying, masalan
`JARVIS_DAILY_COST_CAP_USD=1.00`, keyin `make reload`.

Jarvis qachon javob tayyorlaydi (barcha rejimlarda bir xil):

- faqat Telegram'dagi shaxsiy (1:1) chatda, shaxsiy deb belgilanmagan odamga;
- mijoz bir nechta xabar ketma-ket yozsa, oxirgisidan 45 soniya keyin bitta javob
  (`JARVIS_DEBOUNCE_SECONDS`);
- avval **sizga 2 daqiqa** beriladi (`JARVIS_OWNER_FIRST_SECONDS`): shu vaqtda o'zingiz javob
  bersangiz, Jarvis jim turadi;
- 15 daqiqadan eski xabarga javob tayyorlanmaydi (`JARVIS_MAX_MESSAGE_AGE_SECONDS`);
- bir chatda siz yozmasdan ketma-ket 2 tadan ortiq AI javob bo'lmaydi
  (`JARVIS_MAX_AI_TURNS`), keyin chat faqat sizga yo'naltiriladi;
- siz chatga o'zingiz yozsangiz, Jarvis o'sha chatda 2 soat jim turadi
  (`JARVIS_TAKEOVER_MINUTES`).

### 6.2. 2-qadam: qoralama

Endi har javob sizga **karta** bo'lib keladi va siz hal qilasiz.

0. Avval `/shaxsiylar` ni yana bir bor oching. Kuzatuv kunlarida `/qoralamalar` da oila
   yoki do'st chati ko'ringan bo'lsa, uni hozir `/shaxsiy Ism` bilan belgilang.
1. Botda yozing:

   ```
   /jarvis qoralama
   ```

2. MIYA so'raydi: «✍️ Qoralama rejimini yoqaymi? …» → **«✅ Ha, qoralama rejimi»** bosing.
   Javob: «✅ Qoralama rejimi yoqildi: endi har javob sizga karta bo'lib keladi, siz
   tasdiqlaysiz. Orqaga: /jarvis kuzatuv».
3. `/jarvis` → «🤖 Jarvis: qoralama (har javobni siz tasdiqlaysiz)».
4. Qayta ishga tushirish shart emas — o'zgarish darhol ishlaydi.

Javobda «⚠️ Serverdagi .env da JARVIS_MODE=shadow …» yoki «⚠️ Serverda Jarvis o'chiq …» deb
yozilsa, `.env` da aytilgan qatorni tuzating (`JARVIS_MODE=draft`, `JARVIS_ENABLED=true`),
keyin `make reload`.

Kuzatuvga qaytish (darhol): `/jarvis kuzatuv`.

**Qoralama kartasi** shunday ko'rinadi:

```
✍️ Qoralama j12 · Akmal · Telegram
💬 Mijoz (14:05): «Assalomu alaykum, yuk qabul qilasizmi?»
🤖 Javob: «Va alaykum assalom, Akmal aka. Ha, qabul qilamiz. Yuk qaysi shahardan?»
📋 Yuborish hali ulanmagan — matnni nusxa olib o'zingiz yuboring.
```

Tugmalar:

- **«✅ Yuborish»** — faqat Telegram'dan yuborish yoqilganda (6.3) bor. MIYA matnni
  mijozga yuboradi. Karta ostida «⏳ Navbatga qo'yildi — Telegram orqali yuboriladi».
- **«✏️ Tahrirlash»** — matnni o'zgartirish. Kartaga **reply** qilib (javob qilib) yangi
  matnni yozing yoki ovozli xabar yuboring. Karta sizning so'zlaringiz bilan qayta keladi —
  keyin «✅ Yuborish» bosasiz.
- **«🙋 O'zim yozaman»** — o'zingiz yozasiz. «🙋 Mayli, o'zingiz yozasiz. Akmal chatida MIYA
  2 soat jim turadi.»
- **«✖️ Bekor»** — qoralama yuborilmaydi.

**Nusxa rejimi.** `TELEGRAM_SEND_ENABLED=false` bo'lsa, «✅ Yuborish» tugmasi yo'q va kartada
«📋 Yuborish hali ulanmagan — matnni nusxa olib o'zingiz yuboring.» turadi. Matn ustiga
bossangiz nusxa olinadi. Mijoz chatiga o'zingiz yuborasiz. Shunda MIYA siz yozganingizni
ko'radi: «🙋 Siz o'zingiz javob berdingiz — qoralama bekor qilindi.» Bu rejimda MIYA
Telegram'ga **hech narsa yozmaydi**.

**Eskalatsiya kartasi.** Narx, chegirma, to'lov sharti, muddat, va'da yoki kafolat so'ralsa,
mijoz «to'lov qildim» desa, «bot emasmisiz?» deb so'rasa, shikoyat qilsa, huquqiy masala
yoki boshqa odamlar haqida yozsa — Jarvis o'zi javob bermaydi, sizga darhol yozadi: mijoz
yozib bo'lgach 45 soniyadan keyin (`JARVIS_DEBOUNCE_SECONDS`), sizga beriladigan 2
daqiqani kutmasdan. Mijozga ketadigan «kutib turing» javobi esa o'sha 2 daqiqani kutadi va
baribir faqat siz tasdiqlasangiz ketadi:

```
⚠️ Akmal narx so'rayapti · Telegram
👤 Akmal · GS367
💬 «Toshkentgacha 1 kub qancha turadi?» (14:05)
💰 Hisob: Ochiq qarz yo'q
🤖 Yuborishga tayyor: «Aka, narx bo'yicha hozir aniqlashtirib, o'zim sizga yozaman.»
O'z javobingizni shu xabarga reply qilib yozsangiz, «Tasdiqlash va yuborish» aynan shuni yuboradi.
```

«🤖 Yuborishga tayyor» — mijozga «kutib turing» degan tayyor javob (narx, vaqt va va'da
yo'q). Tugmalar:

- **«✅ Tasdiqlash va yuborish»** — shu tayyor javobni yuboradi. Kartaga reply qilib o'z
  javobingizni yozgan (yoki aytgan) bo'lsangiz — **aynan sizning matningizni** yuboradi.
  Faqat Telegram'dan yuborish yoqilganda (6.3) bor.
- **«✍️ O'zim javob yozaman»** — mijozga Telegram'da o'zingiz yozasiz.
- **«⏰ 1 soatdan keyin eslat»** — 60 daqiqadan keyin «⏰ Eslatma (1-marta): Akmal hali javob
  kutyapti» bilan qaytadi. 3-marta qoldirsangiz, eslatma ertalabki xulosaga o'tadi.

Matningizda narx yoki muddat bo'lsa, karta ogohlantiradi: «⚠️ Matningizda narx yoki muddat bor
— bu sizning so'zingiz sifatida yuboriladi.» Bu to'xtatmaydi, faqat eslatadi.

Mijoz chatiga o'zingiz javob yozsangiz, eskalatsiya o'zi yopiladi: «✅ Akmalga o'zingiz javob
berdingiz — eskalatsiya yopildi.»

**Chegaralar:**

- kuniga 40 tagacha qoralama kartasi (`JARVIS_DRAFT_DAILY_CAP`). Qolganlari: «Bugungi
  qoralama limiti tugadi (40). Qolganlari ertalabki xulosada.» — ularni `/qoralamalar` da
  ko'rasiz;
- kuniga 30 tagacha eskalatsiya (`JARVIS_ESCALATION_DAILY_CAP`), qolganlari bitta xabarga
  yig'iladi;
- 12 soatdan eski qoralama hech qachon yuborilmaydi (`JARVIS_DRAFT_EXPIRY_HOURS`);
- tunda kartalar ovozsiz keladi.

**Xulosalarda:** ertalab «🤖 Jarvis (kecha): …», «✍️ Kutayotgan qoralamalar: N — /qoralamalar»,
«⚠️ Ochiq eskalatsiyalar: …», «📨 Javobsiz chatlar: …»; kechqurun «🤖 Bugun Jarvis: …».

### 6.3. 3-qadam: Telegram'dan yuborish

Endi «✅ Yuborish» bosilganda MIYA javobni **sizning Telegram akkauntingizdan** yuboradi.

1. `.env` da:

   ```
   TELEGRAM_SEND_ENABLED=true
   ```

   (`JARVIS_ENABLED=true`, `USERBOT_ENABLED=true` va `TELETHON_SESSION` to'ldirilgan bo'lishi
   kerak.)

2. Serverda:

   ```
   make doctor
   make reload
   ```

3. Botda `/holat` → «📤 Telegram yuborish: ✅ ishlayapti». Kartalarda «✅ Yuborish» va
   «✅ Tasdiqlash va yuborish» tugmalari paydo bo'ladi.

**Yuborish qanday bo'ladi.** Siz bosgandan keyin MIYA odamga o'xshab ishlaydi: 3–7 soniya
kutadi, chatni o'qilgan qiladi, matn uzunligiga qarab «yozmoqda…» ko'rsatadi (15 soniyagacha),
keyin oddiy matn yuboradi (rasm, havola ko'rinishi, tugma yo'q). Yuborilgach: «✅ Yuborildi
(14:07)».

**Cheklovlar (akkauntingizni himoya qilish uchun):**

- bitta akkauntdan ikki xabar orasida kamida 20 soniya (`JARVIS_SEND_MIN_GAP_SECONDS`) —
  sizning tasdiqlagan xabarlaringiz ham hisoblanadi;
- bitta chatga kuniga 6 tadan ortiq AI javob yo'q (`JARVIS_PER_CHAT_DAILY_CAP`);
- bir vaqtda faqat bitta xabar yuboriladi.

**Akkaunt cheklanishi xavfi.** Telegram bot kabi ko'p yozadigan akkauntlarni cheklashi
mumkin. MIYA buning uchun sekin va odamga o'xshab yuboradi. Agar Telegram baribir cheklasa:

- MIYA Telegram'dan yozishni **darhol to'xtatadi** (o'qish davom etadi) va sizga yozadi:
  «⛔ Telegram yuborishni cheklab qo'ydi. MIYA Telegramdan yozishni to'xtatdi; o'qish davom
  etadi. @SpamBot orqali holatni tekshiring. Qayta yoqish: /jarvis yoq telegram»;
- avtomatik javob turlari qoralamaga qaytadi;
- Telegram qisqa kutishni so'rasa: «⏳ Telegram N daqiqa kutishni so'radi — xabarlar keyinroq
  yuboriladi.» — hech narsa qilish shart emas.

**Yuborilmagan xabarlar:**

- «✖️ Akmalga yuborib bo'lmadi: u sizni bloklagan» (yoki «akkaunt o'chirilgan», «bu chatga
  yozish taqiqlangan», «faqat pullik xabar qabul qiladi») — qayta urinilmaydi.
- «❓ j12 yuborilganmi — aniq emas. Chatni tekshiring; MIYA qayta yubormaydi.» — aloqa
  uzilgan paytda bo'ladi. Chatni ochib qarang; ketmagan bo'lsa, o'zingiz yuboring.

Tunda (jim soatlarda) siz tasdiqlagan xabarlar baribir yuboriladi.

### 6.4. 4-qadam: avtomatik (faqat ruxsat berilgan javob turlari)

Faqat uchta javob turi o'zi yuborilishi mumkin: **salomlashish**, **ma'lumot so'rash** va
**umumiy savol** (bilimlar bazasidagi tayyor javob bilan). Qolgani **hech qachon** avtomatik
emas: narx, chegirma, to'lov sharti, muddat, va'da, kafolat, to'lov, kimligi, shikoyat,
huquqiy, boshqa odamlar, boshqa, va «kutib turish javobi».

**Qachon taklif qilinadi.** Har kuni soat 10:17 da MIYA tekshiradi. Bir javob turi qoralama
rejimida:

- kamida 7 kun (`JARVIS_GRADUATION_MIN_DAYS`),
- kamida 30 ta qoralamani siz hal qilgan bo'lsangiz (`JARVIS_GRADUATION_MIN_DRAFTS`),
- ularning kamida 90% ini **o'zgartirmasdan «✅ Yuborish» bilan** tasdiqlagan bo'lsangiz
  (`JARVIS_GRADUATION_MIN_APPROVAL_PCT`),
- shu vaqt ichida «Bu xato edi», qizil chiziq hodisasi va kanal cheklovi bo'lmagan bo'lsa,
  xarajat chegarasi bir haftada ikki marta oshmagan bo'lsa,

MIYA so'raydi:

> 🎓 «salomlashish» javoblari tayyor: 9 kunda 34 ta qoralama, 94% tini o'zgartirmasdan
> tasdiqladingiz, qizil chiziq buzilmadi. Endi bunday javoblarni MIYA o'zi yuborsinmi?
> (kuniga 20 tagacha)

Muhim: nusxa rejimida «✅ Yuborish» tugmasi yo'q, shuning uchun u yerdagi qoralamalar
«o'zgartirmasdan tasdiqlangan» deb hisoblanmaydi. Avtomatikka o'tish uchun 6.3-qadam
yoqilgan bo'lishi kerak.

**Ruxsat berish:**

1. Taklif ostida **«✅ Ha, avtomatik»** bosing (yoki «⏳ Yana kuzataylik» — 7 kundan keyin
   yana so'raydi).
2. `.env` da `JARVIS_MODE=auto` bo'lmasa, MIYA aytadi: «Avtomatik rejim uchun serverdagi .env
   da JARVIS_MODE=auto qilish kerak.» Unda `.env` da:

   ```
   JARVIS_MODE=auto
   ```

   va serverda:

   ```
   make doctor
   make reload
   ```

   `JARVIS_MODE=auto` faqat yuqori chegara: siz «✅ Ha, avtomatik» bosmagan javob turi o'zi
   yuborilmaydi.

3. Holat: `/jarvis rejim` — har bir javob turi qaysi rejimda va qancha qolgani («— 5 kun,
   21 ta qoralama, 95% o'zgartirmasdan»), «🔒 Hech qachon avtomatik emas: …» ro'yxati va
   yuqori chegara.

**Avtomatik javoblar qanday ishlaydi:**

- kuniga 20 tagacha (`JARVIS_AUTO_DAILY_CAP`);
- har bir gap — salomlashish/xushmuomalalik, savollar bankidagi savol, «kutib turing» javobi
  yoki `/bilim` dagi tasdiqlangan javob bo'lishi kerak. Boshqa gap bo'lsa, javob o'zi
  ketmaydi — sizga karta bo'lib keladi;
- jim soatlarda kutadi va ertalab ketadi (`JARVIS_AUTO_IN_QUIET_HOURS=false`);
- har biridan keyin ovozsiz xabar: «🤖 Avtomatik yuborildi · Akmal: «…»» va ostida
  **«↩️ Bu xato edi»**. Bossangiz, o'sha javob turi darhol qoralamaga qaytadi.

MIYA o'zi ham qoralamaga qaytaradi va bir marta aytadi («↩️ «salomlashish» yana qoralama
rejimiga qaytdi: …»): siz «Bu xato edi» bosganingizda, kanalda cheklov ishlaganda, yuborilgan
javobda qizil chiziq topilganda yoki xarajat chegarasi bir haftada ikki marta oshganda.

Hamma avtomatik javoblarni o'chirish: `.env` da `JARVIS_MODE=draft`, keyin `make reload`.
(`/jarvis kuzatuv` umumiy rejimni tushiradi, lekin o'z rejimiga ega javob turlarini nomma-nom
aytadi — ular uchun shu `.env` qatori yoki `/jarvis toxtat`.)

---

## 7. Buyruq bilan yuborish

Botga oddiy matn yoki ovozli xabar bilan buyruq bering (Jarvis yoqilgan bo'lishi kerak):

- «Sardorga Telegramdan yozib yubor, kechki 6 da gaplashamiz»
- «Akmalga yoz: yuk yo'lga chiqdi»

MIYA hech narsani darhol yubormaydi — avval karta ko'rsatadi:

```
📤 Yuborishni tasdiqlang j15
👤 Kimga: Sardor · GS367 · Telegram
🕕 Qachon: hozir
💬 Matn: «Kechki 6 da gaplashamiz»
```

Tugmalar: **«✅ Yuborish»** (Telegram'dan yuborish yoqilganda), **«✏️ Tahrirlash»** (kartaga
reply qilib yangi matn yozing), **«✖️ Bekor»**. Nusxa rejimida kartada «📋 Yuborish hali
ulanmagan …» bo'ladi.

Qoidalar:

- **Kimga** — faqat siz aytgan ism yoki kod. Ikki Sardor bo'lsa, MIYA «Qaysi Sardor?» deb
  so'raydi va har biri uchun tugma beradi.
- MIYA yangi suhbat boshlamaydi: chat avvaldan bo'lishi kerak. Bo'lmasa: «Sardor uchun Telegram
  chati topilmadi.»
- **Vaqt.** Yuborish vaqtini aytsangiz (masalan «ertaga soat 9 da yubor»), kartada
  «🕕 Qachon: ertaga 09:00» bo'ladi va xabar o'sha vaqtgacha kutadi. MIYA vaqtni sizning
  so'zlaringiz bilan tekshiradi; noaniq bo'lsa so'raydi.
- Matndagi pul summasi faqat siz aytgan summa bo'ladi.
- Bu sizning so'zingiz, shuning uchun kuzatuv rejimida ham ishlaydi. Faqat `JARVIS_ENABLED=false`
  yoki `JARVIS_MODE=off` bo'lsa ishlamaydi.
- Shaxsiy chatga yozsangiz, kartada «🔒 Bu shaxsiy chat — matn sizning nomingizdan aynan
  shunday ketadi.» bo'ladi.

Buyruq bilan sozlamalarni ham o'zgartirish mumkin (masalan, kimnidir shaxsiy qilish, guruhni
o'qishni boshlash yoki to'xtatish, Google jadvalni qaytadan yozish). Har biri uchun
«✅ Ha, …» / «✖️ Yo'q» kartasi chiqadi, siz bosmaguningizcha hech narsa o'zgarmaydi.

---

## 8. Barcha buyruqlar jadvali

| Buyruq | Nima qiladi | Jarvis yoqilishi shartmi |
|---|---|---|
| `/jarvis` | Holat: rejim, bugungi qoralamalar, yuborilganlar, eskalatsiyalar, limit, Telegram holati | Yo'q (o'chiq bo'lsa «o'chiq» deydi) |
| `/jarvis qoralama` | Kuzatuvdan qoralama rejimiga o'tish («✅ Ha, qoralama rejimi» tugmasi bilan) | Jarvis o'chiq bo'lsa ta'sir qilmaydi — MIYA qaysi `.env` qatorini o'zgartirishni aytadi |
| `/jarvis kuzatuv` | Kuzatuv rejimiga darhol qaytish | Yo'q |
| `/jarvis toxtat` | **Hamma yuborishni darhol to'xtatadi** | Yo'q |
| `/jarvis yoq` | To'xtatishni bekor qiladi (rejimni o'zgartirmaydi) | Yo'q |
| `/jarvis yoq telegram` | Faqat Telegram to'xtatishini yoki cheklovini bekor qiladi | Yo'q |
| `/jarvis tarix` | Oxirgi 20 ta Jarvis amali (kim, qachon, nima) | Yo'q |
| `/jarvis rejim` | Har javob turining rejimi va avtomatikka qancha qolgani | Yo'q |
| `/qoralamalar` | Tasdiq kutayotgan qoralamalar va kuzatuv rejimidagi javoblar | Yo'q |
| `/shaxsiy <ism \| GS kod \| guruh nomi>` | Shaxsiy deb belgilash (MIYA u yerga o'zi yozmaydi) | Yo'q |
| `/shaxsiylar` | Shaxsiy chatlar va oila/do'st takliflari (🔒 / 🔓) | Yo'q |
| `/guruhai <guruh nomi>` | Guruh uchun AI javob ruxsati (hozircha Jarvis guruhlarga yozmaydi) | Yo'q |
| `/uslub` | Yozish uslubingiz kartasi, «✅ Faollashtirish» | Yo'q |
| `/uslub yukla` | Eski yozishmalardan uslubni o'rganish | Yo'q |
| `/bilim` | Bilimlar bazasi ro'yxati | Yo'q |
| `/bilim qo'sh Savol \| Javob` | Yozuv qo'shish | Yo'q |
| `/bilim tuzat b12 Savol \| Javob` | Yozuvni tuzatish | Yo'q |
| `/bilim o'chir b12` | Yozuvni o'chirish | Yo'q |
| `/bilim taklif` | MIYA takliflari (✅ Qo'shish / ✖️ Kerak emas) | Takliflar faqat Jarvis yoqilganda tuziladi |
| `/savolbank` | Savollar banki ro'yxati | Yo'q |
| `/savolbank qo'sh Savol?` | Savol qo'shish (✅ Qo'shish bilan) | Yo'q |
| `/savolbank o'chir 3` | 3-savolni o'chirish | Yo'q |
| `/holat` | MIYA'ning ahvoli; Jarvis qatorlari faqat Jarvis yoqilganda | Jarvis qatorlari uchun ha |
| `/xarajat` | MIYA'ning API xarajati (shu oy; `/xarajat 2026-10` — boshqa oy) | Yo'q |
| `/ertalab`, `/hisobot` | Ertalabki va kechki xulosa (Jarvis bo'limlari Jarvis yoqilganda) | Bo'limlar uchun ha |
| `/chats` | Qaysi chatlar o'qiladi, 👁 rasm o'qish, 📄 hujjat o'qish | Yo'q |
| Oddiy matn yoki ovoz: «Sardorga yozib yubor …» | Buyruq bilan yuborish (7-bo'lim) | Ha |
| «Bugun kimga nima va'da bergandim?», «Kecha kimlar yozdi?» | Bazadan tezkor javob | Yo'q |

Tugmalar:

| Qayerda | Tugmalar |
|---|---|
| Qoralama kartasi | ✅ Yuborish · ✏️ Tahrirlash · 🙋 O'zim yozaman · ✖️ Bekor |
| Eskalatsiya kartasi | ✅ Tasdiqlash va yuborish · ✍️ O'zim javob yozaman · ⏰ 1 soatdan keyin eslat |
| Buyruq kartasi | ✅ Yuborish · ✏️ Tahrirlash · ✖️ Bekor |
| Avtomatikka o'tish taklifi | ✅ Ha, avtomatik · ⏳ Yana kuzataylik |
| Avtomatik yuborilgan xabar | ↩️ Bu xato edi |
| `/jarvis qoralama` | ✅ Ha, qoralama rejimi · ✖️ Yo'q |
| Chek kartasi | ✅ Pul keldi · 🚩 Soxta |

Kartaga **reply** qilib yozilgan matn yoki ovozli xabar — kartadagi matnning o'rniga
o'tadi.

Serverdagi buyruqlar:

| Buyruq | Nima qiladi |
|---|---|
| `make update` | Zaxira → yangi kod → bazani yangilash → qayta ishga tushirish |
| `make doctor` | `.env` va serverni tekshirish |
| `make reload` | `.env` dan keyin bot, worker va userbot'ni qayta yaratish |
| `make ps` | Qaysi qism ishlayapti |
| `make logs` | Hamma loglar (chiqish: `Ctrl+C`) |
| `make bot`, `make worker`, `make userbot` | Bitta qismning logi |
| `make userbot-login` | Telegram'ga qayta ulanish (yangi `TELETHON_SESSION`) |

---

## 9. Xavfsizlik va to'xtatish

**Favqulodda to'xtatish** (botda, darhol, qayta ishga tushirish shart emas):

```
/jarvis toxtat
```

«⛔ Jarvis to'xtatildi. Hech qanday xabar yuborilmaydi. Qayta yoqish: /jarvis yoq». Siz
tasdiqlagan xabarlar ham kutib turadi. MIYA'ning qolgan qismi (o'qish, xulosalar, eslatmalar)
ishlayveradi.

**Davom ettirish:** `/jarvis yoq`. Bu faqat to'xtatishni bekor qiladi, rejimni
o'zgartirmaydi. Kuzatuv rejimida bo'lsangiz, MIYA «👀 Hozir kuzatuv rejimi …» deb eslatadi.

**Butunlay o'chirish:** `.env` da `JARVIS_ENABLED=false`, keyin `make reload`. Shunda
Jarvis'ning hech bir ishi ishlamaydi.

**Rejimni pasaytirish:**

- `/jarvis kuzatuv` — darhol kuzatuvga;
- `.env` da `JARVIS_MODE=draft` (avtomatikni o'chiradi) yoki `JARVIS_MODE=shadow`
  (kartalarni ham o'chiradi), keyin `make reload`;
- `.env` da `TELEGRAM_SEND_ENABLED=false`, keyin `make reload` — nusxa rejimiga qaytish.

**Kunlik chegaralar (standart):**

| Nima | Chegara | Sozlama |
|---|---|---|
| Qoralama kartalari | 40 | `JARVIS_DRAFT_DAILY_CAP` |
| Avtomatik javoblar | 20 | `JARVIS_AUTO_DAILY_CAP` |
| Bitta chatga AI javoblar | 6 | `JARVIS_PER_CHAT_DAILY_CAP` |
| Eskalatsiya xabarlari | 30 | `JARVIS_ESCALATION_DAILY_CAP` |
| Jarvis model xarajati | 3.00 $ | `JARVIS_DAILY_COST_CAP_USD` |
| Ikki yuborish orasida | 20 soniya | `JARVIS_SEND_MIN_GAP_SECONDS` |

**Xarajat chegarasi.** Kunlik xarajat chegaraga yetsa: «💸 Jarvis bugungi xarajat chegarasiga
yetdi — ertagacha faqat sizga yo'naltiradi.» O'sha kuni Jarvis model chaqirmaydi, mijozlar
xabarini faqat tayyor «kutib turing» javobi bilan sizga yuboradi.

**Jim soatlar** (`QUIET_HOURS`, standart 23:30–07:30): kartalar ovozsiz keladi, avtomatik
javoblar ertalabgacha kutadi, siz tasdiqlaganlar yuboriladi.

**Jarvis hech qachon:**

- shaxsiy deb belgilangan chatga, guruhga, kanalga, botga, «Saqlangan xabarlar»ga yoki
  Telegram xizmat chatiga o'zi yozmaydi;
- narx, chegirma, to'lov, muddat, va'da yoki kafolatni o'zi aytmaydi — bular doim sizga keladi;
- spam, firibgar yoki buzilgan akkauntdan kelgan xabarga javob bermaydi;
- yangi suhbat boshlamaydi;
- «yuborilganmi — aniq emas» xabarni qayta yubormaydi;
- rasm, fayl, havola ko'rinishi yoki tugma yubormaydi — faqat oddiy matn;
- chekdan pulni hisobga yozmaydi;
- siz «✅ Ha, avtomatik» bosmagan javob turini o'zi yubormaydi.

---

## 10. Muammo bo'lsa

Avval botda `/holat` ni oching. Jarvis qatorlari:

```
🤖 Jarvis: qoralama · bugun 3/40 yuborildi · navbatda 0
🧾 Jarvis bugun: 5 ta qoralama · 1 ta eskalatsiya · xarajat $0.42 / $3.00
📤 Telegram yuborish: ✅ ishlayapti · oxirgi yuborish: …
✅ Javob beruvchi — oxirgi ishga tushish: …
```

| `/holat` yoki xabar | Nima qilish kerak |
|---|---|
| `/jarvis` «o'chiq» deydi | `.env` da `JARVIS_ENABLED=true`, `make reload`. |
| `/jarvis` hali ham «kuzatuv» | Botda `/jarvis qoralama` → «✅ Ha, qoralama rejimi». MIYA `.env` qatorini ko'rsatsa, uni tuzating va `make reload`. |
| Hech qanday karta kelmayapti | `/holat` da «✅ Telegram o'quvchi — ulangan» bo'lsin. Mijoz 1:1 chatda yozganmi, chat shaxsiy emasmi, 2 daqiqa ichida o'zingiz javob bermadingizmi, xabar 15 daqiqadan eski emasmi — tekshiring. `make doctor` Jarvis haqida ⚠️ chiqarmasin. |
| «⏸ Telegram o'quvchi — o'chirilgan (USERBOT_ENABLED=false)» | `.env` da `USERBOT_ENABLED=true`, `make reload`. |
| «❌ Telegram o'quvchi — hali ulanmagan» yoki «… dan beri jim» | `make userbot` (log) ko'ring, `make reload`. Session eskirgan bo'lsa: `make userbot-login`, chiqqan `TELETHON_SESSION=…` ni `.env` ga yozing, `make reload`. |
| «📤 Telegram yuborish: ⏸ o'chirilgan (TELEGRAM_SEND_ENABLED=false)» | Bu nusxa rejimi — normal. Yuborish kerak bo'lsa: 6.3-qadam. |
| «📤 Telegram yuborish: ⏸ to'xtatilgan (/jarvis yoq)» | Siz `/jarvis toxtat` qilgansiz. Davom ettirish: `/jarvis yoq`. |
| «⛔ Telegram yuborishni cheklab qo'ydi …» yoki «📤 Telegram yuborish: ⛔ cheklangan (…)» | 1) Telegram'da @SpamBot ni oching va `/start` bosing — u akkauntingiz cheklanganmi, qachongacha, aytadi. 2) Cheklov tugaguncha kuting; vaqti ko'rsatilgan bo'lsa, o'zi tugaydi. 3) Keyin `/jarvis yoq telegram`. Qayta-qayta yoqmang. Avtomatik javob turlari qoralamaga qaytgan bo'ladi. |
| «📤 Telegram yuborish: ⏳ Telegram kutishni so'radi (…)» | Hech narsa qilmang — vaqt o'tgach o'zi davom etadi. |
| «📤 Telegram yuborish: ⚠️ yuboruvchi ishlamayapti (…)» | `make userbot` (log) ko'ring, keyin `make reload`. «Telegram o'quvchi jim» bo'lsa — yuqoridagi o'quvchi qatoriga qarang. |
| «📤 N ta xabar M daqiqadan beri yuborilmay turibdi. Userbot ishlayotganini tekshiring.» | `make ps` — userbot «running» bo'lsin. `/holat` dagi «📤 Telegram yuborish» qatoriga qarang. `make reload`. Xabarlar ketgach «✅ Xabar yuborish tiklandi» keladi. |
| «⚠️ Jarvis: N ta xabar yuborilish holatida qotib qoldi» | Bir necha daqiqa kuting: MIYA chatni tekshiradi. Aniqlanmasa, «aniq emas» bo'lib qoladi (keyingi qator). |
| «❓ Jarvis: N ta xabar yuborilganmi — aniq emas …» yoki «❓ j12 yuborilganmi — aniq emas» | Mijoz chatini oching. Xabar yo'q bo'lsa, o'zingiz yuboring. MIYA uni qayta yubormaydi. |
| «💸 Jarvis bugungi xarajat chegarasiga yetdi …» | Ertagacha Jarvis faqat sizga yo'naltiradi. Kerak bo'lsa `.env` da `JARVIS_DAILY_COST_CAP_USD` ni oshiring, `make reload`. Haftada ikki marta bo'lsa, avtomatik turlar qoralamaga qaytadi. |
| «… narxi noma'lum: N ta chaqiruv» | 5.6-bo'lim: `EXTRACT_MODEL_PRICE` / `REASON_MODEL_PRICE` ni yozing, `make reload`. |
| «⚠️ Javob beruvchi — hali ishga tushmagan» yoki «❌ Javob beruvchi — … dan beri ishlamagan» | `make worker` (log) ko'ring, `make reload`. |
| «❌ Javob beruvchi — xato: …» | `make worker` logini ko'ring; xato takrorlansa, dasturchiga shu qatorni yuboring. |
| «⚠️ Jarvis — holatini o'qib bo'lmadi» | `make bot` logini ko'ring, `make reload`. |
| «✖️ Akmalga yuborib bo'lmadi: …» | Mijoz sizni bloklagan yoki chatga yozib bo'lmaydi. Qayta urinilmaydi — boshqa yo'l bilan bog'laning. |
| «⚠️ Jarvis buyrug'ini bajarib bo'lmadi — hech narsa o'zgarmadi.» | Birozdan keyin qayta urinib ko'ring. Takrorlansa — `make bot` logi. |

**Google Sheets** («📊 Google Sheets yangilanmayapti: …»):

| Sabab | Nima qilish kerak |
|---|---|
| «xizmat hisobi kaliti topilmadi» | Kalit `secrets/sheets_service_account.json` da yo'q — 4-bo'lim, 5-qadam. |
| «xizmat hisobi kaliti o'qilmadi» | Fayl xizmat hisobining JSON kaliti emas yoki buzilgan. Google Cloud'dan JSON kalitni qaytadan yuklang (4-bo'lim, 4–6-qadamlar), keyin `make reload`. |
| «PermissionError» | MIYA kalit faylini o'qiy olmayapti: `sudo chown 10001:10001 secrets/sheets_service_account.json`, keyin `make reload`. |
| «SHEETS_SPREADSHEET_ID bo'sh» | `.env` ga id ni yozing, `make reload`. |
| «jadvalga ruxsat yo'q (403)» | Jadvalni xizmat hisobi e-mailiga «Editor» qilib ulashing (`make doctor` manzilni ko'rsatadi). |
| «jadval topilmadi (404)» | `SHEETS_SPREADSHEET_ID` noto'g'ri — manzildan qayta ko'chiring. |
| «Google kalitni qabul qilmadi» | Google Cloud'da yangi JSON kalit yarating va almashtiring. |
| «Google javob bermadi», «Google bilan aloqa yo'q», «(429)» | Odatda o'zi o'tib ketadi. Kuting. |

**Loglarni o'qish:** `make logs` (hammasi), `make worker`, `make bot`, `make userbot`.
O'qish qiyin bo'lsa, `.env` da `LOG_FORMAT=text`, keyin `make reload`.

---

## 11. Xarajat

**Qayerda ko'rinadi:**

- `/holat` → «🧾 Jarvis bugun: … xarajat $0.42 / $3.00» (oxirida «— chegaraga yetdi» bo'lishi
  mumkin);
- kechki xulosada → «💸 Jarvis xarajati: … / …»;
- `/xarajat` → shu oy MIYA'ning butun API xarajati, turlar bo'yicha. Jarvis qatorlari
  `jarvis_intent`, `jarvis_draft`, `jarvis_guard`, `jarvis_summary`, `jarvis_command`,
  `jarvis_style`, `jarvis_kb` nomi bilan chiqadi.

**Kunlik chegara:** `JARVIS_DAILY_COST_CAP_USD=3.00` (standart). Bu faqat Jarvis'ning o'z
xarajati. MIYA'ning umumiy ogohlantirishlari alohida: `SPEND_ALERT_DAILY_USD=5` va
`SPEND_ALERT_MONTHLY_USD=60`.

**Narxlar** (USD, 1 million token uchun, kirish / chiqish; MIYA'ga o'rnatilgan jadval):

| Model | Kirish | Chiqish | MIYA'da nima uchun |
|---|---|---|---|
| `claude-haiku-4-5` (standart `EXTRACT_MODEL`) | 1.00 | 5.00 | javob turini aniqlash, tekshirish, eskalatsiya qisqachasi, bilim takliflari |
| `claude-sonnet-5` (standart `REASON_MODEL`) | 3.00 | 15.00 | javob qoralamasi, uslub, buyruq bilan yuborish |
| `claude-sonnet-4-6` | 3.00 | 15.00 | — |
| `claude-opus-5` | 5.00 | 25.00 | — |

Boshqa model qo'ysangiz, narxini `.env` ga yozing (5.6-bo'lim).

**Qachon pul ketadi:**

- har bir mos mijoz xabari uchun, **kuzatuv rejimida ham** (javob turi, qoralama,
  tekshiruv);
- eskalatsiya kartasining qisqachasi uchun;
- buyruq bilan yuborishda;
- uslubni tuzishda va haftalik bilim takliflarida.

Salomlashish modelsiz aniqlanadi (lekin unga javobni yozish va tekshirish baribir model
bilan). Narx, muddat va boshqa «qizil chiziq» savollari modelsiz sizga yo'naltiriladi. Tezkor savollar («Bugun kimga nima va'da bergandim?») ham modelsiz.

---

## Ilova: Jarvis sozlamalari (`.env`)

Hammasi ixtiyoriy — yozilmasa, standart qiymat ishlaydi. O'zgartirgandan keyin
`make doctor` va `make reload`.

| Sozlama | Standart | Nima |
|---|---|---|
| `JARVIS_ENABLED` | `false` | Jarvis'ning asosiy kaliti |
| `JARVIS_MODE` | `draft` | Yuqori chegara: `off`, `shadow`, `draft`, `auto`. Faqat pasaytiradi, hech qachon ko'tarmaydi |
| `TELEGRAM_SEND_ENABLED` | `false` | Telegram'dan yuborish. `false` — nusxa rejimi |
| `JARVIS_DRAFT_DAILY_CAP` | `40` | Kunlik qoralama kartalari |
| `JARVIS_AUTO_DAILY_CAP` | `20` | Kunlik avtomatik javoblar |
| `JARVIS_PER_CHAT_DAILY_CAP` | `6` | Bitta chatga kunlik AI javoblar |
| `JARVIS_SEND_MIN_GAP_SECONDS` | `20` | Ikki yuborish orasidagi soniyalar |
| `JARVIS_DRAFT_EXPIRY_HOURS` | `12` | Shundan eski qoralama yuborilmaydi |
| `JARVIS_DAILY_COST_CAP_USD` | `3.00` | Jarvis'ning kunlik xarajat chegarasi |
| `JARVIS_DEBOUNCE_SECONDS` | `45` | Ketma-ket xabarlardan keyin kutish |
| `JARVIS_OWNER_FIRST_SECONDS` | `120` | Avval sizga beriladigan vaqt |
| `JARVIS_MAX_MESSAGE_AGE_SECONDS` | `900` | Shundan eski xabarga javob yo'q |
| `JARVIS_MAX_AI_TURNS` | `2` | Siz yozmasdan ketma-ket AI javoblar |
| `JARVIS_HISTORY_TURNS` | `8` | Javob yozishda o'qiladigan oxirgi xabarlar |
| `JARVIS_TAKEOVER_MINUTES` | `120` | Siz yozgandan keyin jim turish |
| `JARVIS_ESCALATION_SNOOZE_MINUTES` | `60` | «⏰ eslat» kechiktirishi |
| `JARVIS_SNOOZE_MAX` | `3` | Shuncha kechiktirishdan keyin ertalabki xulosaga |
| `JARVIS_ESCALATION_DAILY_CAP` | `30` | Kunlik eskalatsiya xabarlari |
| `JARVIS_REPLY_DELAY_MIN_SECONDS` / `_MAX_SECONDS` | `3` / `7` | Yuborishdan oldingi tasodifiy pauza |
| `JARVIS_TYPING_CHARS_PER_SECOND` | `6` | «yozmoqda…» tezligi |
| `JARVIS_TYPING_MAX_SECONDS` | `15` | «yozmoqda…» eng uzog'i |
| `USERBOT_FLOOD_SLEEP_SECONDS` | `5` | Telegram'ning qisqa kutishini o'zi kutadi |
| `JARVIS_GRADUATION_MIN_DAYS` | `7` | Avtomatik taklifigacha kunlar |
| `JARVIS_GRADUATION_MIN_DRAFTS` | `30` | Avtomatik taklifigacha qoralamalar |
| `JARVIS_GRADUATION_MIN_APPROVAL_PCT` | `90` | O'zgartirmasdan tasdiqlangan foiz |
| `JARVIS_AUTO_IN_QUIET_HOURS` | `false` | Avtomatik javoblar tunda ham ketsinmi |
| `STYLE_SAMPLE_SIZE` | `300` | Uslub uchun o'qiladigan xabarlaringiz |
| `STYLE_MIN_SAMPLES` | `50` | Bundan kam bo'lsa uslub tuzilmaydi |
| `KB_MATCH_THRESHOLD` | `0.75` | Bilim yozuvi savolga qanchalik mos bo'lishi kerak (0–1) |
| `SHEETS_ENABLED` | `false` | Google Sheets nusxasi |
| `SHEETS_SERVICE_ACCOUNT_JSON` | `./secrets/sheets_service_account.json` | Xizmat hisobi kaliti |
| `SHEETS_SPREADSHEET_ID` | bo'sh | Jadval id si |
| `SHEETS_SYNC_SECONDS` | `60` | Jadval qanchada yangilanadi |
| `SAFETY_EXTRA_SAFE_DOMAINS` | bo'sh | O'zingiz tekshirgan saytlar |
| `RECEIPT_MATCH_MINUTES` | `30` | Chek va bank yozuvi orasidagi farq |
| `RECEIPT_BANK_WAIT_MINUTES` | `60` | Bankdan tasdiq kutish |
| `RECEIPT_STALE_HOURS` | `72` | Shundan eski chek — «eski» |
| `PROMISE_REMIND_LEAD_MINUTES` | `60` | Soatli muddatdan qancha oldin eslatish |
| `REPORT_TIME` | `20:00` | Kechki xulosa vaqti |
| `LOG_FORMAT` | `json` | `text` — o'qish oson loglar |
