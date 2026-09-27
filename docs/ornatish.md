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
   Qolganlariga tegma: EXTRACT_MODEL, REASON_MODEL, TIMEZONE=Asia/Tashkent, QUIET_HOURS=23:30-07:30, MORNING_BRIEF_TIME=09:00, REPORT_TIME=19:00.
4) Zaxira kaliti: make backup-key → chiqqan BACKUP_AGE_RECIPIENT=age1… qatorini .env'ga yoz. make backup-key-show → chiqqan qatorlarni parol menejeriga saqla (server tashqarisida!).
5) Tekshir: make doctor → ❌ qolmasin.
6) Ishga tushir: make up (birinchi marta 10–20 daqiqa), keyin make health → "status":"ok".
7) Telegram o'quvchi: make userbot-login → telefon raqam, kod, 2FA → chiqqan TELETHON_SESSION=… qatorini .env'ga yoz → docker compose up -d --force-recreate userbot.
8) Birinchi zaxira: make backup → Telegramga «🗄 Zaxira nusxa …» fayli kelsin.
9) Telegram'da: botga /start, /holat — hamma qator ✅. /chats — qaysi guruhlar o'qilishini tanla.
10) Birinchi kun tekshiruvi:
   - Botga ovozli xabar yubor («sinov, bugun Akmal bilan gaplashdim») → javob kelsin.
   - 30 daqiqadan keyin /qidir Akmal → topsin (model ~2 GB birinchi marta yuklanadi).
   - Serverda docker stats --no-stream → api 4 GB dan kam; free -h → swap deyarli ishlatilmagan.
   - 19:00 da kechki xulosa, ertasi 09:00 da ertalabki xulosa kelsin; 03:30 dan keyin Telegramda zaxira fayli bo'lsin.
   - Birinchi 10 kun har kechqurun /bugun'dagi xarajat jamini o'zing hisoblagan bilan solishtir.
   - Haftasiga 2 marta /xarajat'ni Anthropic Console'dagi summa bilan solishtir.
   - Bir marta: make restore FILE=/data/backups/<oxirgi fayl> DRY=1 → ichidagi jadvallar ro'yxati chiqsin.
10b) Tashqi kuzatuvchi: healthchecks.io'da «MIYA» tekshiruvini yarat (davr 5 daqiqa, kutish 15 daqiqa), Telegram integratsiyasini ulab, ping manzilini .env'dagi DEADMAN_PING_URL= ga yoz, keyin docker compose up -d --force-recreate worker.
11) Telefon ilovasi: faqat Telegramga kelgan yangi imzoli APK'ni, serverdagi telefon tuzatishlaridan keyin o'rnat (rotatsiya qadamlari pastda).

## Ishlab turgan serverni yangilash

1) Serverda: cd miya && git status → o'zgarish yo'q; git branch → * master.
2) make update → zaxira oladi, kodni tortadi, bot/worker/userbot'ni to'xtatib migratsiyalarni qo'llaydi va hammasini qayta yoqadi.
3) make doctor → ❌ qolmasin. Yangi sozlamalar .env'ga yozilmasa ham standart qiymat bilan ishlaydi; ro'yxati .env.example'da.
4) Botda /holat → hamma qator ✅. /ertalab → ertalabki xulosa chiqsin.
5) Standartda o'chiq, xohlasang .env'da yoq: CODE_PLACEHOLDERS, TRANSCRIBE_DIARIZE_CALLS, MEDIA_RETENTION_DAYS, VIDEO_RETENTION_DAYS.
6) Birinchi yakshanba 05:00 dan keyin /holat'da tiklash mashqi natijasi ✅ bo'lsin (kalit /app/secrets/backup-key.txt'da bo'lishi kerak).

## Telefon ilovasi: yangi kalitga bir martalik o'tish

Yangi APK boshqa kalit bilan imzolangan, shuning uchun eski ilova ustidan o'rnatilmaydi — eski ilovani bir marta o'chirish kerak.

```
1) Dasturchi serverdagi telefon tuzatishlari o'rnatilganini tasdiqlasin.
2) Eski ilovani och → «Navbat»: kutayotganlar 0 bo'lsin; bo'lmasa internet/Tailscale'ni yoq va kut.
3) Botda /holat → «📱 Telefon» qatori yaqin vaqtni ko'rsatsin.
4) Server manzilini yozib ol; token serverda: grep UPLOAD_TOKENS .env
5) Eski ilovani o'chir: Sozlamalar → Ilovalar → MIYA Companion → O'chirish.
6) Yangi APK'ni o'rnat:
   6.1) Telegram'da APK faylini bos. «Noma'lum ilovalarni o'rnatish» so'ralsa: Telegram → Ruxsat berish.
   6.2) Play Protect ogohlantirsa: «Batafsil» → «Baribir o'rnatish».
   6.3) Ilovani och va sozlashni qaytadan o't: server manzili, token, yozuvlar papkasi, SMS rejimi.
   6.4) Har bir ruxsatni yoq: qo'ng'iroqlar ro'yxati, SMS, «MIYA — to'lov xabarlari» (bildirishnomalar). Tugma kulrang bo'lsa yoki ruxsat berilmasa: Sozlamalar → Ilovalar → MIYA Companion → ⋮ → «Cheklangan sozlamalarga ruxsat berish», keyin qaytadan urinib ko'r.
   6.5) SMS ruxsati baribir berilmasa — bu normal: Payme xabarlari bildirishnoma orqali keladi. SMS rejimini «O'chiq» qil.
   6.6) Batareya cheklovini o'chir, avtostartni yoq (Xiaomi, Samsung).
   6.7) Sozlamalar → «To'lov ilovalari»: Payme ro'yxatda bo'lsa, belgisi qo'yilganini tekshir.
7) 15 daqiqadan keyin /holat'da telefon qatori yangilanganini va /bugun'da takror xarajat yo'qligini tekshir.
```
