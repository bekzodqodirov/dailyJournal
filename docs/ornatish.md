# MIYA'ni o'rnatish — qadamma-qadam

0) Boshlashdan oldin:
   - GitHub'da repozitoriyni yop: Settings → General → Danger Zone → Change visibility → Private.
   - console.anthropic.com'da MIYA uchun alohida kalit yarat va oylik xarajat chegarasini qo'y (masalan 60 $). Eski kalitlarni o'chir.
   - ElevenLabs'da tarifdan ortiq sarf (overage) yopiqligini tekshir.
1) Server: kamida 8 GB RAM, 2 vCPU, 80 GB SSD, Ubuntu 24.04. Kirgach:
   sudo apt update && sudo apt -y upgrade
   curl -fsSL https://get.docker.com | sudo sh
   sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   sudo timedatectl set-timezone Asia/Tashkent
2) Kodni olish (repo yopiq):
   ssh-keygen -t ed25519 -f ~/.ssh/miya_deploy -N ''
   ~/.ssh/miya_deploy.pub ni GitHub → repo Settings → Deploy keys → Add (faqat o'qish) ga qo'y.
   GIT_SSH_COMMAND='ssh -i ~/.ssh/miya_deploy' git clone git@github.com:bekzodqodirov/dailyJournal.git miya && cd miya
   Keyingi make update'dagi git pull ham shu kalitni ishlatsin:
   git config core.sshCommand "ssh -i $HOME/.ssh/miya_deploy -o IdentitiesOnly=yes"
3) .env: cp .env.example .env && chmod 600 .env — keyin to'ldir:
   POSTGRES_PASSWORD=<openssl rand -hex 24> va DATABASE_URL'dagi change-me o'rniga xuddi shu parol (birinchi make up'dan OLDIN)
   ANTHROPIC_API_KEY=<MIYA kaliti>
   ELEVENLABS_API_KEY=<kalit>
   ASSISTANT_BOT_TOKEN=<@BotFather>
   OWNER_TELEGRAM_ID=<@userinfobot raqami>
   TELETHON_API_ID=, TELETHON_API_HASH=<my.telegram.org>
   OWNER_ALIASES=Bekzod, Bekzod aka, bekzodaka, Begi, Begika, Bega, GSR Logistics, @<sizning_username>
   API_BEARER_TOKEN=<openssl rand -hex 32>
   UPLOAD_TOKENS=phone:<yana bir openssl rand -hex 32>
   Qolganlariga tegma: EXTRACT_MODEL, REASON_MODEL, TIMEZONE=Asia/Tashkent, QUIET_HOURS=23:30-07:30, MORNING_BRIEF_TIME=09:00, REPORT_TIME=20:00.
4) Zaxira kaliti: make backup-key → chiqqan BACKUP_AGE_RECIPIENT=age1… qatorini .env'ga yoz. make backup-key-show → chiqqan qatorlarni parol menejeriga saqla (server tashqarisida!).
5) Tekshir: make doctor → ❌ qolmasin.
6) Ishga tushir: make up (birinchi marta 10–20 daqiqa), keyin make health → "status":"ok".
7) Telegram o'quvchi: make userbot-login → telefon raqam, kod, 2FA → chiqqan TELETHON_SESSION=… qatorini .env'ga yoz → docker compose up -d --force-recreate userbot.
8) Birinchi zaxira: make backup → ekranda BackupResult(path=PosixPath('/data/backups/miya-…'), …, error=None) chiqsin (fayl serverda qoladi; Telegramga «🗄 Zaxira nusxa …» fayli har kecha 03:30 dagi zaxiradan keyin keladi).
9) Telegram'da: botga /start, /holat — hamma qator ✅. /chats — qaysi guruhlar o'qilishini tanla.
10) Birinchi kun tekshiruvi:
   - Botga ovozli xabar yubor («sinov, bugun Akmal bilan gaplashdim») → javob kelsin.
   - 30 daqiqadan keyin /qidir Akmal → topsin (model ~2 GB birinchi marta yuklanadi).
   - Serverda docker stats --no-stream → api 4 GB dan kam; free -h → swap deyarli ishlatilmagan.
   - 20:00 da kechki xulosa, ertasi 09:00 da ertalabki xulosa kelsin; 03:30 dan keyin Telegramda zaxira fayli bo'lsin.
   - Birinchi 10 kun har kechqurun /bugun'dagi xarajat jamini o'zing hisoblagan bilan solishtir.
   - Haftasiga 2 marta /xarajat'ni Anthropic Console'dagi summa bilan solishtir.
   - Bir marta: make restore FILE=/data/backups/<oxirgi fayl> DRY=1 → ichidagi jadvallar ro'yxati chiqsin.
10b) Tashqi kuzatuvchi: healthchecks.io'da «MIYA» tekshiruvini yarat (davr 5 daqiqa, kutish 15 daqiqa), Telegram integratsiyasini ulab, ping manzilini .env'dagi DEADMAN_PING_URL= ga yoz, keyin docker compose up -d --force-recreate worker.
11) Telefon ilovasi — APK o'rnatishning yagona istisnosi: MIYA Companion faqat MIYA botidan keladi, xabar ostida imzo sertifikatining SHA-256 izi yoziladi. Shu APK'ni serverdagi telefon tuzatishlaridan keyin o'rnat (rotatsiya qadamlari pastda).
    MIYA ilovasi faqat MIYA botidan keladi; boshqa hech kimdan kelgan APK faylni o'rnatmang.
    Boshqa har qanday APK, .exe yoki arxivni (tanish odamdan kelgan bo'lsa ham) MIYA yuklab olmaydi va ochmaydi — faqat ogohlantiradi.

## Ishlab turgan serverni yangilash

1) Serverda: cd miya && git status → o'zgarish yo'q; git branch → * master; git rev-parse --short HEAD → chiqqan kodni yozib ol (hozirgi versiya); git fetch → xatosiz. «Permission denied (publickey)» desa, bir marta: git config core.sshCommand "ssh -i $HOME/.ssh/miya_deploy -o IdentitiesOnly=yes", keyin yana git fetch.
1b) Avval zaxirani alohida: make backup → oxiri error=None bo'lsin. Bo'lmasa yangilashni boshlama: error='no_recipient' bo'lsa — yuqoridagi o'rnatishning 4-qadami (make backup-key → BACKUP_AGE_RECIPIENT=… qatorini .env'ga yoz), keyin yana make backup; boshqa xato bo'lsa, matnini dasturchiga yubor.
2) make update → zaxira oladi, kodni tortadi, bot/worker/userbot'ni to'xtatib migratsiyalarni qo'llaydi va hammasini qayta yoqadi. O'rtada doctor «✅ Hammasi joyida …» desa, hech narsa yozma — davom etadi. «Stopped» qatorlaridan oldin to'xtasa (zaxira yoki git pull xatosi), MIYA eski versiyada ishlayapti: xatoni tuzat va yana make update. «Stopped» qatorlaridan keyin istalgan xato bilan to'xtasa (doctor ❌, yig'ish yoki migratsiya xatosi), bot/worker/userbot to'xtagan holda qoladi: xatoni tuzat, keyin make up. Migratsiya xatosida baza eski holatida qoladi; eski versiyaga qaytish: git checkout <1-qadamdagi kod> && make up, xatoni dasturchiga yubor; tuzatilgandan keyin git checkout master && make update.
3) make doctor → ❌ qolmasin. Yangi sozlamalar .env'ga yozilmasa ham standart qiymat bilan ishlaydi; ro'yxati .env.example'da («.env faylida N ta yangi sozlama yo'q» degan ⚠️ qator — normal). Hech qanday SQL kerak emas: bazani make update o'zi yangilaydi. Loglar endi JSON qatorlar; o'qishga qulay bo'lsin desang .env'da LOG_FORMAT=text, keyin make reload.
4) .env'da REPORT_TIME=19:00 bo'lsa, REPORT_TIME=20:00 ga o'zgartir (yoki qatorni o'chir), keyin make reload → kechki xulosa 20:00 da keladi.
5) Botda /holat → hamma qator ✅. /ertalab → ertalabki xulosa chiqsin.
6) Standartda o'chiq, xohlasang .env'da yoq: CODE_PLACEHOLDERS, TRANSCRIBE_DIARIZE_CALLS, MEDIA_RETENTION_DAYS, VIDEO_RETENTION_DAYS.
7) .env'ni o'zgartirgan har safar: make doctor, keyin make reload (= docker compose up -d --force-recreate bot worker userbot). docker compose restart .env'ni qayta o'qimaydi.
8) Birinchi yakshanba 05:00 dan keyin /holat'da tiklash mashqi natijasi ✅ bo'lsin (kalit /app/secrets/backup-key.txt'da bo'lishi kerak).

## Jarvis (mijozlarga javob yozuvchi)

Standartda o'chiq (JARVIS_ENABLED=false). Yoqish, kuzatuv → qoralama → yuborish → avtomatik bosqichlari, shaxsiy chatlar, uslub, bilimlar bazasi va Google Sheets — hammasi alohida to'liq qo'llanmada: [docs/jarvis-qollanma.md](jarvis-qollanma.md) (yangilash, yangi himoyalar, cheklar, Google Sheets, Jarvis'ni bosqichma-bosqich yoqish, barcha buyruqlar, muammolar va xarajat). Qisqa varaq: [docs/jarvis-egasi.md](jarvis-egasi.md). Hozir faqat Telegram ishlaydi; Gmail va WhatsApp hali yo'q.

Qisqasi:
1) Botda /shaxsiylar va /shaxsiy — birinchi qoralamadan oldin; /uslub yukla → /uslub → «✅ Faollashtirish»; /bilim qo'sh Savol | Javob; /savolbank.
2) .env: JARVIS_ENABLED=true, JARVIS_MODE=draft, TELEGRAM_SEND_ENABLED=false → make doctor → make reload. Jarvis kuzatuv rejimida boshlanadi: hech narsa ko'rsatilmaydi va yuborilmaydi, /qoralamalar'da ko'rasiz (kuniga 3 $ gacha model xarajati bor).
3) Bir necha kundan keyin botda /jarvis qoralama → «✅ Ha, qoralama rejimi»: har javob karta bo'lib keladi.
4) Yuborish tugmasi uchun .env: TELEGRAM_SEND_ENABLED=true → make reload.
5) Avtomatik — faqat MIYA taklif qilgan javob turi uchun «✅ Ha, avtomatik» bosganingizda va .env'da JARVIS_MODE=auto bo'lsa.
To'xtatish: /jarvis toxtat. Kuzatuvga qaytish: /jarvis kuzatuv.

## Telefon ilovasi: yangi kalitga bir martalik o'tish

Yangi APK boshqa kalit bilan imzolangan, shuning uchun eski ilova ustidan o'rnatilmaydi — eski ilovani bir marta o'chirish kerak.

```
1) Dasturchi serverdagi telefon tuzatishlari o'rnatilganini tasdiqlasin.
2) Eski ilovani och → «Navbat»: kutayotganlar 0 bo'lsin; bo'lmasa internet/Tailscale'ni yoq va kut.
3) Botda /holat → «📱 Telefon» qatori yaqin vaqtni ko'rsatsin.
4) Server manzilini yozib ol; token serverda: grep UPLOAD_TOKENS .env
5) Eski ilovani o'chir: Sozlamalar → Ilovalar → MIYA Companion → O'chirish.
6) Yangi APK'ni o'rnat:
   6.1) Faqat MIYA botining chatidagi «📱 MIYA Companion» xabaridagi APK faylini bos — boshqa chatdan yoki boshqa odamdan kelganini emas. Xabardagi «Imzo SHA-256» izi dasturchi bergan iz bilan bir xil bo'lsin; farq qilsa, o'rnatma. «Noma'lum ilovalarni o'rnatish» so'ralsa: Telegram → Ruxsat berish.
   6.2) Play Protect ogohlantirsa: «Batafsil» → «Baribir o'rnatish».
   6.3) Ilovani och va sozlashni qaytadan o't: server manzili, token, yozuvlar papkasi, SMS rejimi.
   6.4) Har bir ruxsatni yoq: qo'ng'iroqlar ro'yxati, SMS, «MIYA — to'lov xabarlari» (bildirishnomalar). Tugma kulrang bo'lsa yoki ruxsat berilmasa: Sozlamalar → Ilovalar → MIYA Companion → ⋮ → «Cheklangan sozlamalarga ruxsat berish», keyin qaytadan urinib ko'r.
   6.5) SMS ruxsati baribir berilmasa — bu normal: Payme xabarlari bildirishnoma orqali keladi. SMS rejimini «O'chiq» qil.
   6.6) Batareya cheklovini o'chir, avtostartni yoq (Xiaomi, Samsung).
   6.7) Sozlamalar → «To'lov ilovalari»: Payme ro'yxatda bo'lsa, belgisi qo'yilganini tekshir.
7) 15 daqiqadan keyin /holat'da telefon qatori yangilanganini va /bugun'da takror xarajat yo'qligini tekshir.
```
