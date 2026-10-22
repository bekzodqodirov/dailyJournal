# MIYA Jarvis plan — the act layer: style-cloned replies, red lines, escalation, WhatsApp, Gmail and Sheets

Written 2026-10-20 against the current HEAD (Alembic head `0021_search_passages`). Sources: the
owner's Jarvis spec, the owner decisions of 2026-10-20, three competing architecture plans, six
area maps (userbot, data model, assistant bot and RAG, worker and ops, money and vision,
external channels, safety research). Every `file:line` below was read at this HEAD; re-read the
lines before editing, because earlier work packages move them.

This plan continues `docs/handoff-plan.md` (WP-01…85). Its work packages are numbered
**JV-01…JV-31**, and its migrations continue from **0022**.

---

## 0. Egasi uchun qisqacha

1. **Bu nima.** Bu hujjat — MIYA'ni «Jarvis»ga aylantirish rejasi: MIYA mijozlaringizga sizning
   uslubingizda javob yozadi, sizning nomingizdan Telegram, WhatsApp va Gmail'da xabar yuboradi,
   cheklarni o'qiydi, va'dalaringizni soati bilan yozib boradi va hammasini Google Sheets'ga
   ko'chirib turadi. Uni boshqa AI dasturchi 31 ta kichik ish paketi bo'yicha, tartib bilan
   bajaradi. Hozirgi MIYA (qarzlar, to'lovlar, eslatmalar, ertalabki va kechki xulosa) to'liq
   saqlanadi va Jarvis o'chib qolsa ham ishlayveradi.

2. **Jarvis nima qiladi.**
   - Salomlashadi, «qanday yordam bera olaman?» deb so'raydi, yuk haqida ma'lumot yig'adi (qayerdan,
     qayerga, necha kilo, GS kodi), siz tasdiqlagan bilimlar bazasidan umumiy savollarga javob
     beradi — sizning uslubingizda («aka», «xo'p bo'ladi», «ko'rib beraman»).
   - Mijoz narx, chegirma, to'lov sharti yoki muddat so'rasa yoki va'da talab qilsa — sizning
     uslubingizda muloyim «kutib turing» deydi («Aka, hozir buni aniqlashtirib, o'zim tez orada
     yozvoraman») va **darhol** sizga xabar beradi: kim, qaysi kanal, nima so'radi, hisobdagi
     raqamlar (faqat bazadan) va uchta tugma: **[✅ Tasdiqlash va yuborish] [✍️ O'zim javob
     yozaman] [⏰ 1 soatdan keyin eslat]**. Javobni shu xabarga reply qilib yozsangiz, «Tasdiqlash
     va yuborish» aynan sizning matningizni yuboradi.
   - Ovozli xabarlarni (Telegram, WhatsApp, bot) matnga aylantiradi; chek, skrinshot, hisob-faktura
     rasmlarini o'qiydi: summa, sana-vaqt, kimdan, qaysi karta. Chekni bank SMS'i yoki Payme bilan
     solishtiradi: «✅ tasdiqlandi», «⏳ kutilmoqda» yoki «⚠️ shubhali».
   - Sizning va mijozning va'dalarini soati bilan yozadi («Ertaga 14:00 gacha hisoblab beraman» →
     ertaga 14:00) va muddatdan oldin eslatadi.
   - Botga matn yoki ovoz bilan buyruq bersangiz bajaradi: «Bugun kimga nima va'da bergandim?»,
     «Kecha kimlar yozdi?», «Sardorga Telegramdan yozib yubor, kechki 6 da gaplashamiz». Har bir
     yuborishdan oldin kimga, qaysi kanal va aniq matnni ko'rsatib, tasdiq so'raydi.
   - Ertalab 09:00 da bugungi ishlar, muddatlar va javobsiz chatlar; kechqurun **20:00** da kun
     xulosasi keladi. Jadvalda uchta varaq yangilanib turadi: Tasks_Promises, Client_FollowUps,
     Receipts.

3. **Jarvis hech qachon nima qilmaydi.**
   - **Hech qachon va'da bermaydi, muddat aytmaydi, kafolat bermaydi** («Ertaga tayyor bo'ladi»,
     «Soat 5 da tashlab beraman», «100% hal qilamiz» — yo'q).
   - **Hech qachon narx, chegirma, to'lov sharti aytmaydi va «pul tushdi» deb tasdiqlamaydi.** Bu
     ikki qoida modelga emas, kodga yozilgan: javobda birorta raqam bo'lsa ham u yuborilmaydi va
     sizga yo'naltiriladi.
   - Siz «shaxsiy» deb belgilagan chatlarga (oila, do'stlar) hech qachon o'zi yozmaydi. Guruhlarda
     siz yoqmaguningizcha javob yozmaydi. Kanallar, botlar, «Saqlangan xabarlar» — hech qachon.
   - **APK, .exe, arxiv va boshqa o'rnatiladigan fayllarni hech qachon yuklab olmaydi va ochmaydi**,
     sizni ogohlantiradi. Spam va firibgarlik xabarlariga javob bermaydi. Tanish odamdan kelgan
     bo'lsa ham — chunki bunday fayllar ko'pincha buzilgan akkauntlardan keladi.
   - **O'zi (tugmasiz) yuborganda faqat ruxsat etilgan gaplarni yozadi:** salom-alik, bayram
     tabrigi, rahmat, «kuttirganim uchun uzr», «qanday yordam bera olaman?», mijozga savol, tayyor
     «kutib turing» shablonlari va siz tasdiqlagan bilimlar bazasidagi matn — so'zma-so'z. Boshqa
     har qanday erkin gap («mol bizda, aka», «hammasi joyida», «как штык») hech qachon o'zi
     ketmaydi: u qoralama bo'lib sizga keladi va siz o'qib tasdiqlamaguningizcha yuborilmaydi.
   - **O'zi beradigan savollar faqat «savollar banki»dan:** qayerdan, qayerga, qanday yuk, necha
     kilo, necha kub, necha joy, GS kodi, yukni kim qabul qiladi, hujjat yoki invoys bormi, yuk
     qaysi manzilda — o'zbekcha (lotin va kirill) va ruscha, vaqt, narx va va'dasiz. Bankni
     `/savolbank` bilan o'zingiz tahrirlaysiz (har yangi savol tekshiriladi va sizning
     «✅ Qo'shish» tugmangizdan keyingina qo'shiladi). Bankda yo'q har qanday savol qoralama bo'lib
     sizga keladi. Bu qoida faqat kodda emas, ma'lumotlar bazasining o'zida ham yozilgan: shakl
     tekshiruvidan o'tmagan javob hech qachon «avtomatik tasdiqlangan» bo'lib saqlanmaydi.
   - Hech kimga birinchi bo'lib yozmaydi (faqat sizga yozganlarga javob beradi). «Botmisiz?» deb
     so'rashsa, yolg'on gapirmaydi — sizga yo'naltiradi.

4. **Ehtiyotkor ishga tushirish (siz tanlagan «careful» rejim).**
   - 1–4-kunlar: Jarvis hech narsa yubormaydi. Fayl himoyasi, cheklar, soatli muddatlar, Sheets,
     20:00 xulosasi, spam ogohlantirishlari ishlay boshlaydi.
   - Keyin 1–2 kun «kuzatuv»: Jarvis javoblarni faqat tayyorlab qo'yadi, hech kimga ko'rsatmaydi —
     qoidalar qanchalik to'g'ri ishlashi o'lchanadi.
   - 1–2 hafta «qoralama»: har bir javob sizga keladi va siz tasdiqlaysiz. Birinchi kunlarda
     «nusxa rejimi»: matnni nusxalab o'zingiz yuborasiz, yuborish ulanganidan keyin esa
     [✅ Yuborish] tugmasi bilan. Kunlik chegaralar bor.
   - Shundan keyin har bir javob turi (salomlashish, ma'lumot so'rash, umumiy savol) alohida
     «avtomatik»ga o'tadi — faqat 7 kundan, 30 ta qoralamadan, 90% o'zgartirishsiz tasdiqdan keyin
     va **faqat siz «Ha» bosganingizda**. Har bir avtomatik javobdan keyin «↩️ Bu xato edi» tugmasi
     keladi — bossangiz, o'sha tur yana qoralamaga qaytadi. Narx, pul, muddat, va'da va «kutib
     turing» javobi — hech qachon avtomatik emas.
   - `/jarvis toxtat` — hammasini darhol to'xtatadi. MIYA'ning qolgan qismi ishlayveradi.

5. **Sizdan nima kerak bo'ladi.**
   - **Shaxsiy chatlarni belgilash** (`/shaxsiy Ism`) — birinchi qoralamadan oldin. MIYA oila yoki
     do'st bo'lishi mumkin bo'lganlarni o'zi taklif qiladi.
   - **Bilimlar bazasi**: ish vaqti, manzil, yo'nalishlar, qanday yuklar olinadi, qanday hujjatlar
     kerak (`/bilim qo'sh Savol | Javob`). Narx yozib bo'lmaydi.
   - **Google Sheets**: jadval ochib, uni MIYA'ning xizmat hisobiga «Muharrir» sifatida ulashish.
   - **Gmail** (4-bosqich, hali qurilmagan): `make gmail-auth` bilan bir marta ruxsat berish;
     Google Cloud'da ruxsat oynasini «In production» holatiga o'tkazish (aks holda ruxsat 7 kunda
     tugashi mumkin).
   - **WhatsApp** (4-bosqich, hali qurilmagan): botga kelgan 8 xonali kodni telefonda «Bog'langan
     qurilmalar → Telefon raqami bilan bog'lash» orqali kiritish.
   - 1–3-bosqichlarni (faqat Telegram) yoqish bo'yicha egasi uchun qo'llanma:
     [docs/jarvis-egasi.md](jarvis-egasi.md).
   - Pul tushadigan kartalaringiz ro'yxati (`PAYMENT_OWN_CARDS`) — chekni «shubhali» deyish uchun.
   - Kunlik Jarvis xarajat chegarasi (standart: 3 dollar).

6. **Asosiy xavf — akkaunt cheklanishi.** Telegram va WhatsApp avtomatik yozishni yoqtirmaydi.
   Xavf shunday kamaytiriladi: faqat sizga yozganlarga javob; kuniga kam xabar (chegaralar);
   har xabar orasida tanaffus; birinchi haftalar faqat sizning tasdig'ingiz bilan; Telegram yoki
   WhatsApp birinchi ogohlantirishdayoq Jarvis o'sha kanalda **o'zi to'xtaydi** va sizga aytadi —
   o'qish va MIYA'ning qolgan qismi ishlayveradi. WhatsApp eng xavfli kanal, shuning uchun u eng
   oxirida, 7 kun faqat o'qish rejimida ulanadi. Boshqa xavflar: noto'g'ri odamga yuborish (har
   yuborishda kimligini ko'rsatib tasdiq so'raladi), xarajat (kunlik chegara), soxta chek (pul
   faqat bank SMS'i bilan hisobga yoziladi).

7. **Spec'dan yaxshiroq qilingan narsalar.**
   - Nusxa rejimi — birinchi haftadanoq sizning uslubingizdagi javoblar, yuborish xavfisiz.
   - Narx savoliga eskalatsiya xabariga reply qilib javob berasiz — bitta tugma bilan yuboriladi.
   - Chek bank SMS'i bilan chekdagi vaqt bo'yicha solishtiriladi; qayta yuborilgan eski chek va
     bir xil rasm aniqlanadi.
   - Jarvis hech qachon birinchi bo'lib yozmaydi — eng katta bloklanish sababi yo'q.
   - Chat «o'qilgan» belgisi faqat javob yuborilganda qo'yiladi — telefoningizdagi o'qilmagan
     belgilar yo'qolmaydi.
   - Tunda (23:30–07:30) avtomatik javob yo'q — bot ekanligi bilinmaydi.
   - Siz tuzatgan qoralamalardan uslub o'rganiladi.
   - Har bir avtomatik javobda «↩️ Bu xato edi» tugmasi.
   - «Botmisiz?» savoliga yolg'on yo'q — har doim sizga yo'naltiriladi.
   - Tanish odam to'satdan fayl yoki qarz so'rasa — «akkaunti buzilgan bo'lishi mumkin»
     ogohlantirishi.
   - Siz javob yozayotgan yoki chatni o'qigan bo'lsangiz, Jarvis gapingizni bo'lmaydi.
   - Kunlik xarajat chegarasi — oshsa, faqat sizga yo'naltiradi.
   - «Bugun kimga va'da bergandim?», «Kecha kimlar yozdi?» — modelsiz, to'g'ridan-to'g'ri bazadan.
   - Oila/do'st chatlarini MIYA o'zi taklif qiladi — siz bitta tugma bilan belgilaysiz.
   - Bilimlar bazasini sizning eski javoblaringizdan taklif qiladi — siz tasdiqlaysiz.
   - O'rnatish qo'llanmasidagi APK qadamlari tuzatiladi: MIYA ilovasi faqat MIYA botidan keladi.

---

## 1. Judging the three plans, and what was grafted

Three plans were submitted. Each was scored 1-10 on five criteria and checked against the code.

| Plan | Safety enforceability | Fit to MIYA | Completeness | Buildability | Rollout realism | Total |
|---|---|---|---|---|---|---|
| A — safety-first chokepoint | 9 | 9 | 7 | 7 | 8 | 40 |
| **B — incremental value (winner)** | 9 | 9 | 9 | 9 | 9 | **45** |
| C — not delivered | 1 | 1 | 1 | 1 | 1 | 5 |

**Plan A.** A single gateway/dispatcher with the gate run twice, the zero-digit output guard,
lethal-trifecta containment, and a sender inside the userbot process. Its fit claims check out
against the code: `TelegramClient(...)` is built with defaults at `miya/userbot/main.py:959`, the
AST guard scans only `miya/userbot/*.py` with a non-recursive glob (`tests/test_userbot.py:60,70`),
the size check precedes the type check in `media_policy.plan_for` (`media_policy.py:141-144`), and
`TYPED_SOURCES` lacks `receipt_photo` (`money_events.py:86`). Weaknesses: it never addresses the
spec's structured-logging, backoff and typing rules, WhatsApp/Gmail rows are never windowed or
extracted (`conversation_windows.tg_chat_id` is NOT NULL), the first work packages ship nothing
the owner can see, and the Baileys auth state is put in PostgreSQL even though the nightly
`pg_dump` is sent to Telegram.

**Plan B.** The same chokepoint idea, implemented as one module that alone changes outbox state
(an AST test), a claim transaction that re-runs the full gate, a DB CHECK that AI text cannot be
approved without a passing guard, and an ordering that delivers safe value first: protect, then
help without speaking, then speak with approval, then speak alone. It covers every spec item
including logging/backoff/type checking, any-channel windows, per-intent graduation, copy mode,
cost caps and owner-first/takeover signals. It wins.

**Plan C** produced no design and is scored 1 on every criterion. Its only recorded fact (head is
`0021_search_passages`) is correct.

**Grafted into the final plan from Plan A:**
- Deterministic Telegram `random_id` derived from the outbox idempotency key (JV-19), so a crash
  retry is a no-op at Telegram's side as well as in the DB.
- The "model guard may block but never approve; the deterministic guard is authoritative" rule
  (JV-16).
- A per-chat "only live messages younger than JARVIS_MAX_MESSAGE_AGE_SECONDS" rule (JV-16).
- Explicit FloodWait handling around the startup `sync_from_client` (`main.py:991`), which today
  can crash-loop the userbot (JV-19).
- The responder's forbidden-input list made explicit and pinned by a test (JV-16).
- A static "no INSERT/UPDATE of `outbox_messages` outside `miya/jarvis/outbox.py`" check that also
  covers raw SQL text (JV-11).
- Telegram account flags (`User.scam`, `User.fake`) and `fwd_from` / `via_bot_id` as scam
  features (JV-09).

**Decided against Plan A:** the Baileys auth state stays in a transactional SQLite file under
`./data/whatsapp`, never in PostgreSQL, because the nightly `pg_dump` goes to Telegram and that
state is account-takeover-grade. A restore therefore needs a re-pair; the guide says so.

---

## 2. Binding rules

### 2.1 MIYA's rules (unchanged, never contradict)

1. **Money and debt figures come from SQL, never from a model.** A model may phrase, not produce
   figures.
2. Money is `NUMERIC(14,2)` plus a currency enum, `Decimal` end to end; currencies are **never**
   mixed or added together.
3. A failed or uncertain extraction sets `needs_review`. Nothing is dropped silently.
4. Owner-facing text is Uzbek (Latin). Code, comments, commit messages and this document are
   English.
5. Secrets are never committed. The owner's Telegram username is never in a tracked file; tests
   use `@owner_test123`.
6. **No vendor model identifiers** in docs, code comments or commits. Say EXTRACT_MODEL or
   REASON_MODEL. API prices come only from config (`EXTRACT_MODEL_PRICE`, `REASON_MODEL_PRICE`);
   never guess one.
7. Every new Settings key goes to `.env.example` (comment lines above the key, no inline
   comments), the README configuration table and `miya/tools/doctor.py` `EXPECTED_ENV_KEYS`.
8. Migrations continue from `0022`; revision ids are at most 32 characters; `alembic check` stays
   clean; migrations never import app code; a new enum label is never used in the same migration
   run (compare `::text`, use JSON-key or FK predicates).
9. Ruff line length 90. Tests run against real PostgreSQL.
10. Use they/them for people. Timezone Asia/Tashkent; quiet hours 23:30–07:30.
11. What a counterparty asserts is asked before it is written (the claims gate).

### 2.2 New Jarvis rules

12. **One outbound chokepoint.** Nothing reaches any of the owner's accounts except as a row in
    `outbox_messages`, whose state only `miya/jarvis/outbox.py` changes, and which an adapter gets
    only through `outbox.claim_ready()`. The model never sends; it proposes text.
13. **Red lines are code.** RED LINE 1 (no promise, deadline, time or guarantee) and RED LINE 2 (no
    price, discount, payment term, money figure or payment confirmation) are enforced by pure
    Python in `miya/jarvis/redlines.py`, before and independent of any model. The guard fails
    closed into a fixed stall template plus an escalation.
13a. **AI text goes out on its own only in approved shapes.** The lexicons cannot list every
    way to say a promise, so an autonomous send (`auto:policy`, no owner tap) needs the statement
    allowlist as well as the red lines: every sentence is a courtesy from a closed set, a question
    from the owner's question bank (JV-23, `qbank`; any other question is a `free_question`), a
    fixed stall template (or the owner's override that passed the stall role), or the verbatim
    text of an active owner-approved KB entry (`redlines.check_shapes`, `shapes_ok` in the bound
    verdict). Any other sentence is an `unapproved_statement`: it never blocks a draft card, but
    such a row is never approved for auto send, and the gate sends an auto row without a passing
    shape check back to the owner's card at the claim. The database enforces it as well: an
    autonomous AI row without `shapes_ok` true cannot be stored (`ck_outbox_messages_auto_shapes`,
    0028).
14. **AI-authored text contains no digits** except a GS code or waybill that appears verbatim in
    the same chat's inbound messages.
15. **The client responder has no tools and no private data.** Its inputs are this thread, the
    style profile, scrubbed examples and active KB entries only.
16. **Every send that is not AI-autonomous needs an owner tap** on a card that shows the exact text,
    the recipient's identity and the channel. A recipient comes only from the owner's own words,
    never from tool output. Act tools are never exposed on `POST /v1/ask`.
17. **Reply-only.** The AI never starts a conversation. Owner commands go only to chats that
    already exist on that channel.
18. **Never deny being an AI.** Identity questions always escalate.
19. **Dangerous files are never downloaded or opened**, on any channel, and the blocklists are code
    constants that config cannot loosen.
20. **Everything defaults off.** An unset Jarvis key never fails validation; core MIYA never imports
    or waits on Jarvis, a channel or the WhatsApp sidecar.
21. **MIYA-sent messages are never the owner's words.** Every consumer uses one authorship
    predicate (JV-10) before any sender exists.
22. **Caps are SQL.** Daily and per-chat caps are counted from `reminders.day_start` over outbox and
    escalation rows. Jarvis never writes `question_log` and never spends
    `QUESTION_BUDGET_PER_DAY`.
23. **Structured logs carry ids and counts only**, never message, SMS, notification or draft
    bodies.
24. **Platform limits are respected, not worked around.** A restriction, spam limit or long flood
    wait stops sending on that channel and alerts the owner; nothing retries into it.
25. **Failures are contained.** Every new loop, job, handler and sidecar event callback catches
    per item, logs the error class with ids, and continues; one bad row, message or API error
    never stops the reader, the worker or the sidecar. A long-lived task that dies is restarted
    with `retry.with_backoff` (JV-02), never left dead.
26. **No placeholders.** No `TODO`/`FIXME`/`XXX`, no stub bodies and no `NotImplementedError`
    outside an abstract base class in new code (a JV-02 test enforces it on `miya/jarvis/**`,
    every new `miya/services/*.py` module and `whatsapp/src/**`).
27. **Outbound is plain text only.** An outbox row carries a text body and nothing else: no
    attachments, no forwards, no media, no links rendered as previews. A send path that needs a
    file does not exist.

---

## 3. Architecture

### 3.1 Processes

No new Python process. One new optional Node process.

```
                         +-------------------------- PostgreSQL 16 (the only bus) --------------------------+
                         |  interactions  outbox_messages  escalations  reply_rules  jarvis_policy          |
                         |  jarvis_chat_state  receipts  sheet_rows  owner_style_*  knowledge_entries ...   |
                         +-----^--------------^----------------^-----------------^-----------------^--------+
                               |              |                |                 |                 |
  Telegram (owner account)     |              |                |                 |                 |
  <-------> userbot -----------+  ingest      |                |                 |                 |
            | (one Telethon client)           |                |                 |                 |
            | handlers: NewMessage in/out, MessageEdited, MessageRead, UpdateDraftMessage, Deleted |
            | task: approved_media_loop (read path, unchanged)                    |                 |
            | task: sender.py  --claim_ready()--> [GATE] --> pace --> send --> complete()          |
                                              |                |                 |                 |
  worker (APScheduler) ----------------------+                 |                 |                 |
     jarvis_respond (15 s)  -> responder: inbound signals -> draft -> guard -> outbox.create       |
     jarvis_cards (1 m)     -> cards, escalation alerts, snooze re-alerts, expiry -> owner bot     |
     gmail_pull / gmail_send -> Gmail API  (gmail_send also uses claim_ready)   |                 |
     sheets_sync (60 s)     -> Google Sheets (one-way mirror)                   |                 |
     receipt_*, safety_notices, style_refresh, kb_mining, jarvis_rollout        |                 |
                                                               |                 |                 |
  bot (aiogram, owner only) -----------------------------------+  records decisions only:        |
     /jarvis /shaxsiy /guruhai /uslub /bilim /qoralamalar       |  approve, edit, snooze, mark    |
     /savolbank                                                 |                                 |
     command mode -> ACT_TOOLS -> outbox.create (proposal)      |  personal (row lock, re-check)  |
                                                                                 |                 |
  api (FastAPI) ---------------------------------------------------------------- +                 |
     /v1/bridge/whatsapp/*  (BRIDGE token family; events, media-policy, media,                   |
                             outbox/claim -> claim_ready(), outbox/{id}/result, heartbeat, pairing)
                                                                                                   |
  whatsapp sidecar (Node 20, compose profile 'whatsapp', optional) --HTTP only--> api             |
     Baileys pinned; auth in ./data/whatsapp/auth.sqlite (never in PG, never in backups)
```

### 3.2 The outbound chokepoint

```
 responder / command tool / escalation reply
                 |
                 v
     outbox.create(...)  -- runs policy + caps + guard, binds guard to body_sha256,
                 |          state shadow | draft | approved (auto intents, shapes_ok only)
                 v
     owner card (jv:/je:/ja:)  -- bot: approve() under row lock, re-validate, state=approved
                 |
                 v
     outbox.claim_ready(channel, account, now, limit)   <-- the ONLY way an adapter gets a row
        one SKIP LOCKED transaction re-runs the FULL gate:
          kill switches (JARVIS_ENABLED, <CHANNEL>_SEND_ENABLED), pauses, breakers
          rollout mode: env ceiling JARVIS_MODE, and for auto rows the effective
            channel x intent mode re-resolved (a demotion after approval blocks the row
            back to draft)
          reply_rules + hard exclusions (Saved Messages, 777000, bots, channels, groups-off):
            for authored_by='ai' a personal 'never' rule blocks; an owner-authored row to a
            personal chat passes only if its card carried the personal warning (history)
          safety shield: the trigger, or any inbound in this chat since it, has a
            meta.safety verdict spam/scam/hijack or a media.risk -> AI rows blocked,
            owner rows re-warned on the card
          chat state: owner replied/composing/read after trigger, takeover pause
          trigger edited/deleted, expired, not_before
          caps (daily drafts/auto, per chat, min gap per account, channel daily send cap
            GMAIL_/WHATSAPP_DAILY_SEND_CAP), cost cap; owner-authored rows count towards
            the per-account gap and channel caps (anti-ban), not the AI caps
          guard binding: body_sha256 unchanged, else re-guard (red lines, for every row);
            an auto row also needs shapes_ok (statement allowlist, questions from the
            owner's question bank, JV-23), else back to draft
            (unapproved_statement), never sent
          payload shape: text body only (rule 27)
          quiet hours for AI auto rows
        -> state=sending, lease_until, claimed_by; COMMIT
        -> returns frozen ClaimedSend + pacing plan (pre-delay, typing ms, mark-read flag)
                 |
                 v
     adapter does network I/O with NO transaction held
       Telegram: userbot/sender.py   WhatsApp: sidecar   Gmail: worker gmail_send
                 |
                 v
     outbox.complete(id, external_id, sent_at)  | outbox.fail(id, code, retry_at)
       complete also writes or tags the echo Interaction:
       meta {sent_by:'miya', authored_by, outbox_id, tg_message_id|wa_message_id|gmail_message_id}
     expired 'sending' lease -> reconcile(): echo found -> sent; none -> 'unknown', owner told,
     never resent blindly
```

### 3.3 Inbound to reply

1. Ingest as today: interaction, media policy with the shield (JV-01), transcript or vision text.
2. `meta.safety` verdict (JV-09). Spam, scam or a dangerous file: an owner warning, never a reply.
3. After `ingest_message` returns True (after transaction 2, so voice transcripts exist),
   `responder.enqueue_candidate` upserts `jarvis_chat_state.respond_after`. No model call in a
   Telethon handler. Never from catch-up, backfill or edits.
4. `jarvis_respond` claims due chat states (SKIP LOCKED) after the debounce and the owner-first
   window, and checks eligibility, freshness, caps, cost cap and the AI turn limit.
5. Deterministic inbound signals (JV-13). A red-line, identity, complaint or other-people hit opens
   an escalation and queues a stall template; no drafting call is made.
6. Otherwise intent (deterministic first, then EXTRACT_MODEL JSON), KB retrieval, and a
   REASON_MODEL draft with `tools=[]` and strict JSON output.
7. Output guard: deterministic, then the model check. Any failure becomes a stall plus an
   escalation.
8. An outbox row: `shadow`, `draft`, or `approved` (only for a graduated auto intent).
9. Card or alert to the owner; copy mode until the channel adapter exists.
10. Owner tap, adapter claim, send, complete. Any genuine owner message in that chat cancels pending
    drafts, resolves the escalation as `auto:own` and pauses the AI there.

### 3.4 Owner command flow

Text or voice, routed by `rag.classify` → `'command'` (imperative cues, JV-20) →
`answer_full(allow_actions=True)` with `TOOLS + ACT_TOOLS`. Act tools create outbox drafts with
`origin='owner_command'`, `authored_by='owner'` and a confirmation card. `send_at` is cross-checked
by the deterministic `when.py`. Nothing leaves without a tap. `POST /v1/ask` stays read-only.

### 3.5 Package layout

`miya/jarvis/` (mypy strict): `chatkey`, `policy`, `caps`, `gate`, `outbox`, `costs`, `redlines`,
`stalls`, `envelope`, `intent`, `style`, `style_prompt`, `kb`, `draft`, `guard`, `responder`,
`chat_state`, `cards`, `commands`, `rollout`, `qbank`.
Shared helpers in `miya/services/`: `safety.py` (pure), `when.py` (pure), `receipts.py`,
`sheets.py`, `sheets_sync.py`, `gmail.py`, `authorship.py`, `retry.py`, `wa_ingest.py`; and
`miya/logs.py`. The only Telethon write module is `miya/userbot/sender.py`.

Chat keys are channel-agnostic text: `'tg:<marked id>'`, `'wa:<jid>'`, `'mail:<thread id>'`.

Ref letters (verify against `miya/bot/formatting.py:201` and the question-log refs before use):
`j` outbox row, `e` escalation, `r` receipt, `b` knowledge entry.

Callback prefixes (all new; taken today: `birl ch cl kod md mx ng rec rv sv unut vq`):
`jv:` drafts and auto-send notices (`jv:x:<id>` «↩️ Bu xato edi»), `je:` escalations, `ja:`
owner-command confirmations, `jr:` personal rules, `js:` style profile, `kb:` knowledge base,
`jg:` graduation, `qb:` the question bank (JV-23), `rcp:` receipts.
Payloads are id-only and at most 64 bytes. **None of them is added to `_button_handlers`**
(`handlers.py:2378-2384`), so a typed «hammasi ha» can never approve a send.

---

## 4. Data model

Conventions for every table: state columns are `String(N)` plus a named CHECK (only two new
native enum labels, in 0029); `created_at`/`updated_at` via `created_at_column`; `history JSONB
NOT NULL DEFAULT '[]'::jsonb` where a row has a lifecycle, entries `{at, field, old, new, by}`;
money `Numeric(14,2)` + `CURRENCY` + `CHECK > 0`; every CASCADE/SET NULL FK is the first column of
an index; each new model goes into `models.__all__`, `tests/conftest.py::_truncate` (FK order),
`people.merge_into._fk_columns` and the purge planners **in the WP that adds it**;
`server_default` text is byte-identical in model and migration; `by` values fit `String(16)`
(`button`, `reply`, `command`, `auto:own`, `auto:policy`, `auto:expiry`, `auto:spam`,
`rule:group`).

### 4.1 Migration chain

| Revision | WP | Content |
|---|---|---|
| `0022_promise_due_time` | JV-03 | `promises.due_time`, `tasks.due_time` |
| `0023_receipts` | JV-04 | `receipts` |
| `0024_sheet_rows` | JV-07 | `sheet_rows` |
| `0025_jarvis_core` | JV-11 | `jarvis_policy`, `reply_rules`, `jarvis_chat_state`, `escalations`, `outbox_messages` |
| `0026_owner_style` | JV-14 | `owner_style_profiles`, `owner_style_examples`, `owner_style_people`, `chat_monitors.style_import_*` |
| `0027_knowledge_base` | JV-15 | `knowledge_entries` |
| `0028_auto_shapes` | JV-23 | `ck_outbox_messages_auto_shapes`; `jarvis_policy` scope `question_bank` |
| `0029_source_labels` | JV-24 | `interaction_source` + `'whatsapp'`, `'email'` |
| `0030_channel_accounts` | JV-24 | `channel_accounts`, `wa_chats`, `email_threads`, `contact_handles`, interaction FKs |
| `0031_windows_any_chat` | JV-25 | windows and passages for any chat kind |

### 4.2 Tables

**0022 — promises / tasks.** `due_time TIME NULL` on both, with `ck_promises_due_time` /
`ck_tasks_due_time` `CHECK (due_time IS NULL OR due_date IS NOT NULL)`. A `due_at` hybrid property
combines them in `settings.tz`; `due_at_expr()` is the SQL helper. `due_date` stays the single
source of the day, so the ~40 existing readers keep working.

**0023 — `receipts`.**
- `id`; `interaction_id` FK interactions CASCADE NOT NULL, `ux_receipts_interaction`.
- `person_id` FK people SET NULL, indexed.
- `direction String(8)` CHECK `('in','out','unknown')`.
- `amount Numeric(14,2) NULL`, `currency CURRENCY NULL`; CHECK `amount > 0`; CHECK
  `((amount IS NULL) = (currency IS NULL))`.
- `paid_at timestamptz` (keeps the printed date and time).
- `payer_name`, `payee_name`, `issuer`, `reference`.
- `payer_card_last4`, `payee_card_last4` `String(4)` CHECK `^[0-9]{4}$`.
- `image_sha256 CHAR(64)` indexed.
- `reading JSONB '{}'` (audit only, never a source of truth).
- `state String(16)` CHECK `('read','needs_review','rejected')`.
- `verdict String(16) DEFAULT 'waiting'` CHECK
  `('waiting','confirmed','unconfirmed','suspicious','owner_ok','owner_fake')`.
- `verdict_reasons text[] '{}'` CHECK `<@ ARRAY['duplicate_image','duplicate_reference',
  'stale_date','future_date','own_card_no_bank','amount_near_miss']`.
- `transaction_id` FK transactions SET NULL, indexed by `ux_receipts_transaction (transaction_id)
  WHERE transaction_id IS NOT NULL`.
- `claim_id` FK claims SET NULL, indexed.
- `notified_at`, `notice_tg_message_id BIGINT`, `verdict_notified_at`, `history`.
- `ix_receipts_waiting (created_at) WHERE verdict='waiting'`;
  `ix_receipts_reference (issuer, reference) WHERE reference IS NOT NULL`.

**0024 — `sheet_rows`.** `tab String(32)` CHECK `('Tasks_Promises','Client_FollowUps',
'Receipts')`; `entity_kind String(16)` CHECK `('promise','task','receipt','person')`; `entity_id
int` (polymorphic, no FK); `row_number int NULL`; `content_hash CHAR(64)`; `state String(16)`
CHECK `('pending','synced','failed','tombstone','cleared')`; `attempts smallint`;
`next_attempt_at`; `last_error String(64)`; `synced_at`; `uq (tab, entity_kind, entity_id)`;
`ux (tab, row_number) WHERE row_number IS NOT NULL`; `ix_sheet_rows_due (next_attempt_at) WHERE
state IN ('pending','failed','tombstone')`. Also `created_at`/`updated_at` and
`ck_sheet_rows_row_number CHECK (row_number IS NULL OR row_number >= 2)` (row 1 is the header).
No `history`: the rows are sync bookkeeping, not owner data, and change every minute. No person FK,
so `merge_into._fk_columns` does not list it: a merged-away person's follow-up row becomes a
tombstone and is cleared.

**0025 — `jarvis_policy`.** `scope text PK`: `'global' | 'channel:<c>' | 'intent:<i>' |
'channel:<c>:intent:<i>'` (0028 adds `'question_bank'`, the row whose `history` is the owner's
question bank); `mode String(8) NULL` (NULL inherits) CHECK
`('off','shadow','draft','auto')`; `paused_until`, `pause_reason String(32)`, `breaker_until`,
`breaker_reason String(32)`, `history`. The data step seeds `('global','shadow')`.

**0025 — `reply_rules`** (personal deny-list and group policy).
- `subject String(16)` CHECK `('person','chat','address')`.
- `person_id` FK people SET NULL, indexed; `chat_key text`; `address text` (channel-qualified
  `tg:<user id>` / `wa:<jid>` / `mail:<addr>`, snapshotted when the rule is set, so `/unut` or a
  merge cannot reopen a personal chat).
- `mode String(8)` CHECK `('never','draft','auto')`.
- `reason String(16)` CHECK `('personal','group','spam','bot','owner')`.
- `state String(16) DEFAULT 'active'` CHECK `('active','proposed','revoked')`.
- `set_by String(16)`, `note`, `history`.
- CHECK `num_nonnulls(person_id, chat_key, address) >= 1`.
- Partial unique indexes per `chat_key`, per `address`, per `person_id`, each `WHERE
  state='active'`.

**0025 — `jarvis_chat_state`.** `chat_key PK`; `channel String(16)` CHECK; `person_id` FK SET
NULL, indexed; `last_inbound_interaction_id` FK interactions SET NULL, indexed;
`last_inbound_at`, `respond_after`, `owner_seen_at`, `owner_composing_at`, `owner_replied_at`,
`ai_paused_until`, `last_ai_sent_at`; `ai_turns_since_client smallint DEFAULT 0`;
`ix_jarvis_chat_state_due (respond_after) WHERE respond_after IS NOT NULL`.

**0025 — `escalations`.**
- `interaction_id` FK CASCADE NOT NULL, indexed; `person_id` FK SET NULL, indexed.
- `channel String(16)`, `chat_key text NOT NULL`.
- `reasons text[] NOT NULL '{}'` CHECK `<@ ARRAY['price','discount','payment_terms','deal',
  'deadline','promise','guarantee','payment_claim','identity','complaint','legal',
  'other_people','other']`.
- `summary text`: digit-free prose, refused like `recaps.clean_prose`; figures on the card come
  from SQL at render time.
- `state String(16)` CHECK `('open','snoozed','owner_replying','resolved','expired','cancelled')`.
- `snoozed_until`, `snooze_count smallint`, `alerted_at`, `alert_count smallint`,
  `alert_tg_message_id BIGINT`, `answered_at`, `answered_by String(16)`.
- `resolution String(16)` CHECK `('sent','owner_replied','expired','cancelled')`; `history`.
- `ux_escalations_open_chat (chat_key) WHERE state IN ('open','snoozed','owner_replying')`
  (repeats append to history); `ix_escalations_snoozed (snoozed_until) WHERE state='snoozed'`;
  `ix_escalations_unalerted (created_at) WHERE alerted_at IS NULL`.

**0025 — `outbox_messages`.**
- `channel String(16)` CHECK `('telegram','whatsapp','gmail')`; `chat_key NOT NULL`;
  `tg_chat_id BIGINT`; `to_address text`.
- FKs, each indexed: `person_id` SET NULL; `trigger_interaction_id` CASCADE (purging the client
  message takes the draft); `command_interaction_id` SET NULL; `sent_interaction_id` SET NULL;
  `escalation_id` SET NULL. `account_id` is added in 0030.
- `reply_to_external_id text`.
- `origin String(16)` CHECK `('auto_reply','stall','owner_command','escalation')`;
  `intent String(24)`; `authored_by String(8)` CHECK `('ai','owner')`.
- `body text NOT NULL`, `ai_body text` (original before owner edits), `body_sha256 CHAR(64)`.
- `guard JSONB NOT NULL '{}'`: `{verdict, flags, det_hits, model_verdict, body_sha256,
  checker_version}`.
- `state String(16)` CHECK `('shadow','draft','approved','sending','sent','failed','unknown',
  'cancelled','expired','blocked')`.
- `approved_by String(16)` (`button`, `reply`, `command`, `auto:policy`), `approved_at`.
- `not_before` (scheduled sends), `expires_at`, `card_tg_message_id BIGINT`.
- `lease_until`, `claimed_by String(32)`, `attempts smallint DEFAULT 0`, `next_attempt_at`,
  `last_error String(64)` (a code, never text).
- `external_message_id text`, `read_ack_at`, `sent_at`.
- `idempotency_key text NOT NULL`, `ux_outbox_messages_idempotency`.
- `history`.
- `ck_outbox_messages_sent_approved CHECK (state <> 'sent' OR approved_by IS NOT NULL)`.
- `ck_outbox_messages_guarded CHECK (state NOT IN ('approved','sending','sent') OR
  authored_by='owner' OR guard->>'verdict'='pass')`.
- `ck_outbox_messages_auto_shapes` (0028, JV-23): an AI row with `approved_by='auto:policy'`
  carries `guard->'shapes_ok' = true` (see 0028 below).
- `ix_outbox_messages_ready (channel, next_attempt_at) WHERE state='approved'`;
  `ix_outbox_messages_sent_at (sent_at) WHERE sent_at IS NOT NULL`;
  `ux_outbox_messages_external (channel, external_message_id) WHERE external_message_id IS NOT
  NULL`; `ix_outbox_messages_chat_state (chat_key, state)`;
  `ix_outbox_messages_lease (lease_until) WHERE state='sending'`.

**0026 — style.**
- `owner_style_profiles`: `version int unique`; `state` CHECK `('draft','active','retired')`;
  `traits JSONB` (deterministic counts: greetings, closings, honorifics, signature phrases, emoji
  rate, average words, punctuation, Latin/Cyrillic and uz/ru mix); `prompt_text` (digit-free);
  `stalls JSONB '{}'` (guarded owner overrides); `sample_count`, `window_start`, `window_end`,
  `input_hash CHAR(64)`; `model String(64)` (the role value, like `RecapDigest.model`);
  `approved_at`, `approved_by`, `history`; `ux_owner_style_profiles_active ON ((true)) WHERE
  state='active'`.
- `owner_style_examples`: `profile_id` FK CASCADE; `interaction_id` FK interactions CASCADE NOT
  NULL (text is joined, never copied, so `/unut` and edits apply); `context_interaction_id` FK
  CASCADE; `intent`, `script`; `source` CHECK `('history','owner_edit')`; `embedding
  Vector(embed_dim)` HNSW cosine; `uq (profile_id, interaction_id)`.
- `owner_style_people`: `person_id PK` FK CASCADE; `honorific String(16)`; `address_form` CHECK
  `('siz','sen','unknown')`; `script` CHECK `('latin','cyrillic','mixed')`; `lang` CHECK
  `('uz','ru','mix')`; `greeting`; `sample_count`; `updated_at`. `merge_into` recomputes it.
- `chat_monitors` gains `style_import_requested_at timestamptz NULL`, `style_import_done_at
  timestamptz NULL` and `style_import_attempts smallint NOT NULL DEFAULT 0`, the same
  request-on-the-row shape as the existing backfill request that `chats.pending_backfills` reads.

**0027 — `knowledge_entries`.** `kind` CHECK `('fact','faq')`; `title`, `question`, `answer NOT
NULL`, `lang String(4)`, `tags text[]` GIN; `state` CHECK `('suggested','active','retired')`;
`source` CHECK `('owner','suggested','import')`; `source_interaction_id` FK CASCADE, indexed;
`guard JSONB`; the Memory search columns copied exactly (embedding + HNSW + unembedded partial
index, `search_norm` + computed `search_tsv` GIN + trgm GIN); `history`.

**0028 — autonomy, defense in depth (JV-23).** No new table. `ck_outbox_messages_auto_shapes
CHECK (authored_by <> 'ai' OR approved_by IS DISTINCT FROM 'auto:policy' OR
COALESCE(guard->'shapes_ok' = 'true'::jsonb, false))`: an AI-authored row approved by the policy
itself must carry a passing shape check (rule 13a with the question bank), in every state; the
JSON boolean `true` only, so neither a missing key (NULL) nor the string `"true"` passes (the
binding decision named `guard->>'shapes_ok' = 'true'`; the jsonb comparison is the same line
without the string loophole). The data step first takes the policy approver off rows that would
break it (an `approved` one goes back to `draft` with `unapproved_statement`, a finished one keeps
its state), each in its history; a row on the wire without a passing check cannot exist and stops
the upgrade if it does. `ck_jarvis_policy_scope` gains `question_bank`. Downgrade deletes that row
and restores the old pattern.

**0029.** `ALTER TYPE interaction_source ADD VALUE IF NOT EXISTS 'whatsapp'`, then `'email'`.
Nothing else in the same run uses them. Downgrade leaves the labels (PostgreSQL cannot drop them).

**0030 — channels.**
- `channel_accounts`: `channel` CHECK `('telegram','whatsapp','gmail')`, `label`, `address`,
  `uq (channel, address)`; `state` CHECK `('pending_link','linked','logged_out','restricted',
  'banned','paused','error')`; `read_enabled DEFAULT true`, `send_enabled DEFAULT false`;
  `sync_cursor` (Gmail historyId); `last_connected_at`; `last_error String(64)`; `detail JSONB
  '{}'`; `history`. Credentials are never stored here.
- `wa_chats`: `account_id` FK CASCADE, `jid`, `uq (account_id, jid)`; `chat_type` (reuses
  `CHAT_TYPE`); `title`; `person_id` FK SET NULL, indexed; `monitor_enabled`, `vision_enabled`,
  `docs_enabled` with server defaults mirroring `ChatMonitor` (private on, groups off; defaults
  only in the schema, per the `chats.py:9-16` rule); `decided_by`, `last_seen_at`.
- `email_threads`: `account_id` FK CASCADE, `gmail_thread_id`, `uq (account_id,
  gmail_thread_id)`; `subject`, `counterpart_address`; `person_id` FK SET NULL, indexed;
  `labels text[]`, `last_message_at`, `monitor_enabled`.
- `contact_handles`: `person_id` FK CASCADE, indexed; `channel` CHECK
  `('telegram','whatsapp','email','phone')`; `address` (normalised), `ux (channel, address)`;
  `alt_address` (the phone-number JID for a LID); `source`.
- `interactions` gains `wa_chat_id` and `email_thread_id` (FK SET NULL, indexed);
  `ux_interactions_wa_message (wa_chat_id, (metadata->>'wa_message_id')) WHERE metadata ?
  'wa_message_id'`; `ux_interactions_gmail_message ((metadata->>'gmail_message_id')) WHERE
  metadata ? 'gmail_message_id'`.
- `outbox_messages` gains `account_id` FK SET NULL, indexed.

**0031 — any-chat windows.** `conversation_windows.tg_chat_id` and `passages.tg_chat_id` become
nullable; both gain `wa_chat_id` and `email_thread_id` (FK CASCADE, indexed); CHECK
`num_nonnulls(tg_chat_id, wa_chat_id, email_thread_id) = 1`; the `ix_interactions_unwindowed`
predicate is rebuilt with JSON and FK conditions (no `source = 'whatsapp'` literal).

### 4.3 JSON keys (no migration)

- `interactions.meta.safety` `{verdict, score, reasons}`; `safety_notice` / `safety_notified`.
- `interactions.media.risk` `{kind: exec|archive|macro|html, name}`.
- `interactions.meta.receipt` (the structured reading, mirrored into `receipts`).
- `interactions.meta.receipt_booked` (JV-05): the owner's own words in this bot message booked
  the payment, so the receipt card and every later edit of it leave out the bank-only footer.
- `interactions.meta.receipt_card_alone` (JV-05): this bot message's receipt card is a message of
  its own, so the bank verdict edits it in place; a bot card without it (sent before JV-05, when
  the card shared its message with the reply and its buttons) is never edited, a fresh card is
  sent instead.
- `interactions.meta.sql` (JV-06): `promises_made` | `who_wrote`, set on a bot question answered
  straight from SQL with no model call.
- Echoes of MIYA-sent messages: `meta.sent_by='miya'`, `meta.authored_by ('ai'|'owner')`,
  `meta.outbox_id`.

---

## 5. Safety model

### 5.1 Red lines

AI-authored text (`authored_by='ai'`) must not contain:
- **RL1**: a promise, a commitment, a deadline or time, or a guarantee.
- **RL2**: a price, a discount, a payment term, a money figure, or a confirmation that a payment
  arrived (`pul tushdi`, `qabul qilindi`, `получил`).

Text the owner authored (`owner_command`, or an edit by reply to a card) is their own words. The
guard flags it on the card; it does not block it. Confirmation is still required.

What AI text may say *without* the owner's tap is narrower still: only approved shapes (§5.2
step 3). A sentence that is neither a red line nor an approved shape is a draft for the owner,
never an autonomous send.

### 5.2 Enforcement, layered and fail-closed

1. **Deterministic inbound signals** (`redlines.classify_inbound`). A hit opens an escalation and
   queues a fixed stall template. No draft is made.
2. **Deterministic output guard** (`redlines.check_outbound`), the binding check:
   - No digit at all, except GS codes or waybills seen verbatim in this chat's inbound messages.
   - Surface view (NFKC, `fold_apostrophes`, casefold, `to_latin`, symbols kept): currency symbols
     `[$€¥₽₸₩£]`, `\d+\s?%`, the clock pattern, dates, and `amounts.money_mentions` must be empty.
   - Word view (`text.normalise_for_search`): GUARANTEE, TIME/DEADLINE, PROMISE and MONEY lexicons
     (Uzbek Latin, Uzbek Cyrillic via transliteration, Russian), written naturally and normalised
     at import like `STOPWORDS` (`text.py:251-259`). The stall-verb allowlist (`aniqlashtirib`,
     `bilib`, `yozaman`, `xabar beraman`…) is subtracted from the generic commitment backstop and
     passes only without a TIME hit.
   - No URLs, emails, @handles or phone shapes; no person name other than the counterparty's
     display name and the owner's aliases; no AI self-disclosure or prompt leakage; the script
     matches the client's; at most 400 characters; no HTML or Markdown.
3. **Statement allowlist (auto only)** (`redlines.check_shapes`, run inside `check_outbound` for
   the `reply` and `stall` roles). Three red-team rounds showed the blocklist alone cannot be
   complete (slang and implication: «mol bizda aka», «С вас ничего», «Как штык»). An AI text is
   split into sentences (a «.», «!», «?», «…» or line break always ends one, so a «?» ends its
   own sentence) and every sentence must have one approved shape:
   - **courtesy**: only phrases from a closed, normalised set (greetings including holiday
     greetings such as «hayit ayyomi muborak», «navro'z bayrami bilan», «С Днём независимости!»;
     thanks; an apology for the wait with no time in it; «qanday yordam bera olaman»; farewells),
     in Uzbek Latin, Uzbek Cyrillic and Russian, plus words of address and, once, the first word
     of the counterparty's name (never a name word that says something: the client sets the
     display name, «Всё Отлично» or «Mol Bor»); or only the emoji 🙂 🙏 👋 (never 👍 ✅ 🤝, never
     punctuation alone). Phrases that agree («рады помочь», «xizmatingizdamiz», «eshitaman»)
     are not in the set;
   - **question**: a closed grammar, not the blocklist read once more. Every segment (split at
     commas, semicolons, colons and dashes) asks or is a courtesy, so a statement glued to a
     question («Yuk omborda, yana savol bormi?») is still a statement. A segment asks only with
     an explicit interrogative: a question word (Russian «что»/«как» only as the segment's first
     word, «как» only before «вас/вам/с/называется/зовут…»), a Russian «ли» after the first word,
     or an Uzbek «-mi» predicate as the last word (not a noun such as «qismi»). A «?» alone asks
     nothing («Мы квиты?», «С вас ничего?», «Yuk Xitoydan?» are statements). Nothing in the
     question may: name a time or «now» («Hozir bo'ladimi?», «Bayram arafasida kelsa
     qulaymi?»); argue or vouch («Nega …», «Разве …», «Kim sizni aldaydi?», «Yuk qayerga ham
     ketardi?», «Nima bo'lsa ham …»); tag an assertion on («shundaymi», «to'g'rimi», «xo'pmi»,
     «emasmi», «-ku», «axir», «ведь», «же», «не так ли»); carry a clause in a verbal noun or
     participle («Yuk kelganini bildingizmi?», «Куда везти прибывший груз?»); report with a
     verb of knowing («bilasizmi», «Знаете, что …»); say what happened or what someone else
     does («… груз пришёл», «… доедет»); offer («… kelsa qulaymi?», «Yuborsakmi?»); or state
     the cargo's status or place. The words before and after the question word must pass the
     red lines read as a statement (their verb forms are read by the grammar). A refused
     question is a statement: a draft card, never a block. **The question bank (JV-23):** the
     grammar cannot list every way a question carries an offer, so for an autonomous send a
     question must also be one of the owner's question bank (`miya/jarvis/qbank.py`): a closed,
     owner-editable list seeded with the questions a cargo business asks (origin city,
     destination, cargo type, weight, volume or cubes, number of places, GS code, contact
     person, documents or invoice, pickup address) in Uzbek Latin, Uzbek Cyrillic and Russian,
     with no time, price or promise in them; every seed passes the red-line guard in the reply
     role and this shape check. A question is matched after normalisation
     (`redlines.bank_key`), and the only slot fillers are the counterparty's name, words of
     address, a courtesy segment and the GS codes and waybills the client wrote in the chat.
     A grammatical question outside the bank is a `free_question` (flag `free_question`): it
     never blocks and changes nothing on a draft card, but `shapes_ok` is false, so it is never
     sent without the owner's tap. The owner edits the bank with `/savolbank` (list, add,
     remove); each addition is guarded (one question, the reply role, the grammar) and enters
     the bank only on the owner's «✅ Qo'shish» (`qb:` buttons, not in `_button_handlers`); a
     removal is at once. The bank lives in the history of the `jarvis_policy` row
     `question_bank` (0028), so no table is added and the audit trail is the bank. The outbox
     loads it itself, like the KB entries, and the claim guards an autonomous row that asked a
     bank question again with the bank in force then;
   - **stall**: a fixed template of §5.3 or a sentence of one, or the owner's override that
     passed the `stall` role;
   - **kb**: the verbatim text (after normalisation) of an active owner-approved KB entry, a
     whole entry or a whole sentence of one (JV-15). The outbox resolves the entries itself; it
     takes no approved texts from its caller (`outbox.create` refuses them until JV-15 and
     JV-14 load the KB entries and the stall overrides from the owner's records), and the claim
     guards a `kb` row again with the entries active then, so a retired entry approves nothing.

   Any other sentence makes the text `unapproved_statement` (a flag that never blocks; its
   escalation reason is `other`, so no migration). The verdict carries `shapes_ok` (true only when
   the red lines passed and every sentence has a shape, every question one of the bank's) and the
   shapes, bound to `body_sha256` like the rest; the database refuses an autonomous row without
   it (`ck_outbox_messages_auto_shapes`, 0028). In draft mode the flag changes nothing: the
   owner reads every text. `outbox.create` approves an auto-intent row on its own only with
   `shapes_ok`, else it becomes a draft card (`last_error=unapproved_statement`, for a statement
   and for a `free_question` alike); `outbox.approve` is the owner's tap only and refuses
   `by=auto:policy`; `gate.evaluate` redrafts an auto row whose bound verdict lacks a passing
   shape check, and one whose re-guarded body fails it; a gate decision that binds a verdict
   without one also takes the policy approver off the row. The deterministic blocklist stays
   binding for every row.
4. **Model check** (EXTRACT_MODEL, JSON). It can block or escalate; it can never approve or
   override a deterministic block. An error, refusal or parse failure counts as a block.

The verdict is stored in `outbox_messages.guard` and bound to `body_sha256`; `claim_ready`
re-guards if the body changed. The lexicons and expected verdicts live in one versioned fixture
corpus, `tests/fixtures/redline_corpus.py`, which the owner can extend. Every spec example must
block; the owner's stall must pass.

### 5.3 Stalls

Stalls are fixed templates keyed by reason × script (Latin, Cyrillic, Russian) × honorific, never
generated. The owner may override a template; the override passes the guard in the `stall` role.
At most one stall per open escalation; a second push from the client gets silence and a higher
escalation priority. No stall invents a fact ("I'm in a meeting"). Every template, and an override
that passed the `stall` role, is an approved shape for the statement allowlist (§5.2 step 3). A
holding reply itself is never sent on its own (JV-23 binding decision: the `stall` intent never
graduates, since it answers a price, deadline, promise or payment message); a stall sentence in a
graduated intent's text is still an approved shape.

### 5.4 Who the AI may write to

Precedence: any active `never` rule wins → hard exclusions → chat rule → person rule → channel
default → the `jarvis_policy` mode (most specific scope first) → the env ceiling `JARVIS_MODE`.
Hard exclusions, regardless of rules: Saved Messages (`tg_chat_id == me.id`), Telegram service
chat 777000, bots, channels, the owner's own aliases, WhatsApp `status@broadcast` / `@newsletter`
/ broadcast lists, Gmail SPAM/PROMOTIONS/SOCIAL/UPDATES/FORUMS and auto-submitted, bulk, list or
no-reply mail (RFC 3834). Groups never get an AI reply unless the owner enables that group, and
then only for messages addressed to them (`meta.to_me`, never `to_me_maybe`). Restricting is
immediate; loosening needs a confirm tap.

### 5.5 File and spam shield

- Pure classifiers in `miya/services/safety.py` run before any download, ask card or model call,
  on every channel (one `channel` parameter; the sidecar asks `/media-policy` before
  `downloadMediaMessage`; Gmail checks before `attachments.get`).
- Filename normalisation (NFKC, dot look-alikes, bidi and zero-width stripping), the EXEC and
  ARCHIVE suffix and MIME blocklists (seeded from the Gmail blocked-types list plus mobile types),
  double-extension detection, magic-byte sniffing (OOXML only with `[Content_Types].xml` and no
  `AndroidManifest.xml`/`classes.dex`), a ZIP central-directory size and ratio check.
- Executables, installers and archives are never fetched and never offered for approval; the
  owner is warned once.
- Hard scam rules quarantine (no reply, no download, no model call, owner warned); soft signals
  score and block auto-reply. A known contact is not a safety signal.

### 5.6 Prompt-injection isolation

- The responder gets no tools and no private data. Its input is this chat's last
  `JARVIS_HISTORY_TURNS` turns in an untrusted envelope (`windows.quote_body`, bidi and zero-width
  stripped, truncated, hidden HTML and quoted mail chains removed), the style prompt, scrubbed
  examples, active KB entries and, in an auto-eligible intent, the owner's question bank (JV-23:
  owner-approved questions, quoted as JSON strings like the KB entries; the prompt allows only
  these, word for word). Forbidden inputs (pinned by a test): `Person.notes`, debts,
  promises, claims, transactions, memories, the RAG tools, other chats, codes not in this thread,
  the owner's phone or card numbers.
- Code fixes the recipient and channel; output is strict JSON `{reply, needs_owner, reason_code}`.
- Admin act tools exist only in command mode, only from the bot handler, and only propose. The
  recipient comes from the owner's words. Injection tripwires ("ignore previous", `[ME]`, fenced
  code) only raise alerts.

### 5.7 Kill switches, breakers, caps

- `JARVIS_ENABLED=false`: nothing is created or claimed. `/jarvis toxtat`: global pause, checked
  at claim time. Per-channel pause in `jarvis_policy`. Per-account `send_enabled`. Per-channel
  `TELEGRAM_SEND_ENABLED`, `GMAIL_SEND_ENABLED`, `WHATSAPP_SEND_ENABLED`.
- Automatic breakers (channel `breaker_until`, owner alerted at once): Telegram `PeerFloodError`,
  a flood wait above the threshold, `AuthKeyDuplicated`/`SessionRevoked`/`UserDeactivatedBan`;
  WhatsApp 401/403/440, `reachoutTimeLock.isActive`, a falling new-chat cap; Gmail
  `invalid_grant`; the cost cap.
- Caps counted in SQL from `reminders.day_start`: daily draft cards, daily auto sends, per chat
  per day, minimum gap between sends per account, consecutive AI turns without the owner, daily
  escalation alerts, daily cost (spend summed from `usage_log`, whose prices come from config).

### 5.8 Owner first and takeover

The responder waits `JARVIS_OWNER_FIRST_SECONDS` and skips a chat the owner has read, is
composing in, or has answered. Any genuine owner message cancels pending drafts, resolves the
escalation as `auto:own` and pauses the AI in that chat for `JARVIS_TAKEOVER_MINUTES`. A chat is
marked read only when a reply is actually sent.

### 5.9 Audit

`history` JSONB on every Jarvis row; `usage_log` operations `jarvis_intent`, `jarvis_draft`,
`jarvis_guard`, `jarvis_command`, `jarvis_style`, `jarvis_kb`, `jarvis_summary` (JV-18),
`receipt_vision`; structured logs
with ids and counts; `/jarvis tarix` shows the last 20 actions.

---

## 6. Rollout and graduation

### 6.1 Modes

- `off`: nothing runs.
- `shadow`: drafts and escalations are stored (`state='shadow'`), not shown; counts appear in
  `/jarvis` and the evening report. Used to measure guard false positives and negatives.
- `draft`: each reply is a card the owner approves (copy mode until the adapter exists).
- `auto`: within caps, a graduated intent is sent; each send is followed by a silent notice with
  «↩️ Bu xato edi».

The effective mode resolves from the most specific `jarvis_policy` scope (channel+intent, intent,
channel, global) and never exceeds the env ceiling `JARVIS_MODE` (default `draft`, so moving any
intent to `auto` needs the owner to raise the ceiling in `.env` as well as tap «Ha»).

The global mode starts at the migration's seed, `shadow`, and moves only on the owner's word:
`/jarvis qoralama` (or `/jarvis rejim qoralama`) asks first and the «✅ Ha, qoralama rejimi»
tap (`jm:d`, not in `_button_handlers`) stores `draft`, a loosening change like JV-12's;
`/jarvis kuzatuv` stores `shadow` at once and names the channel or intent scopes that keep a
mode of their own above it (a graduated intent). Neither ever stores `auto`
(`policy.set_global_mode` refuses it); autonomy is only the per-intent graduation of §6.4. Both
replies name the `.env` line (`JARVIS_ENABLED`, `JARVIS_MODE`) that still holds the mode back.
`/jarvis yoq` only lifts a pause or a breaker; in `shadow` its reply says nothing will be shown
and points at `/jarvis qoralama`.

### 6.2 Intents

- May ever be auto: `greeting`, `info_gathering`, `general_question` (only when a KB entry
  matched). `policy.AUTO_INTENTS` holds them; `policy.set_mode` refuses `auto` for any other
  intent scope.
- Always escalate: `price`, `discount`, `payment_terms`, `deadline`, `promise`, `guarantee`,
  `payment_claim`, `identity`, `complaint`, `legal`, `other_people`.
- Never auto either (JV-23): `stall`, the holding reply those escalations queue (it waits for the
  owner's tap like every reply to a red-line message), and `other`. So a message with a payment
  claim or a price, deadline or promise ask never gets an autonomous reply of any kind.

### 6.3 Timeline

| When | What runs | Outbound |
|---|---|---|
| Day 0-4 | JV-01, JV-02, JV-10, JV-03…JV-09 | none |
| Day 4-6 | JV-11…JV-16 in `shadow` | none |
| Day 6-10 | JV-17, JV-18: `draft`, copy mode | owner copies by hand |
| Week 2 | JV-19, JV-20: Telegram sender; drafts approved with [Yuborish] | owner-approved only |
| Week 3+ | JV-23: per-intent graduation on Telegram | auto for graduated intents |
| Week 3-4 | JV-24…JV-27: Gmail read-only, then drafts, own graduation | owner-approved |
| Week 4-6 | JV-28…JV-30: WhatsApp read-only 7 days, then drafts, own graduation | owner-approved |

### 6.4 Graduation (per channel × intent)

All must hold: at least `JARVIS_GRADUATION_MIN_DAYS` (7) days in `draft`; at least
`JARVIS_GRADUATION_MIN_DRAFTS` (30) drafts; at least `JARVIS_GRADUATION_MIN_APPROVAL_PCT` (90) %
approved without an edit; zero «Bu xato edi» flags; no channel breaker trip in the last 7 days;
the cost cap not reached on two days of the last week (JV-23: the next daily job would demote it
again). Then the bot offers promotion with a button. Nothing promotes silently. Graduation only lets an
intent go out on its own; each row of it still needs, at creation and at the claim, the
deterministic blocklist, the model guard (JV-16, block-only) **and** `shapes_ok` from the statement
allowlist (§5.2 step 3), where every question must come from the owner's question bank
(`/savolbank`). A graduated intent's text with a free-form statement or a question outside the bank
is a draft card. The approval-rate metric counts those drafts like any other. The database holds
the same line (`ck_outbox_messages_auto_shapes`, 0028): an autonomous row without a passing shape
check cannot be stored, whatever code path tried.

Metrics are counted per channel × intent from `outbox_messages` since the window began: its last
mode change or incident (an owner's «Bu xato edi», an edit that took out a red-line sentence, a
red line found after a send). Drafts the owner decided on count (sent, edited, cancelled, expired);
ones still waiting count neither way; «approved without an edit» is the owner's tap on the AI's
own words. Percentages round down. An offer is made at most once every
`JARVIS_GRADUATION_MIN_DAYS` days; the tap re-checks the criteria under the scope's row lock. The
promotion needs the env ceiling too: with `JARVIS_MODE` below `auto` the tap stores `auto` and the
owner is shown «Avtomatik rejim uchun serverdagi .env da JARVIS_MODE=auto qilish kerak.».

**Demotion (automatic, back to draft):** a «Bu xato edi» tap, a guard incident found after a send
(the daily job reads every autonomous send of the week again with that day's guard), a breaker trip, or
the cost cap hit twice in a week. Each is told to the owner once. The sent text of a «Bu xato
edi», of an incident, and of a draft whose red-line or free-form sentence the owner took out in an
edit is kept on its outbox row as a review case for the red-line corpus.

### 6.5 Caps by stage

`JARVIS_DRAFT_DAILY_CAP` 40; `JARVIS_AUTO_DAILY_CAP` 20 (the rollout job may propose +10 a week
with no incidents; owner-confirmed, never above the env value); `JARVIS_PER_CHAT_DAILY_CAP` 6;
`JARVIS_MAX_AI_TURNS` 2; `JARVIS_SEND_MIN_GAP_SECONDS` 20; `JARVIS_ESCALATION_DAILY_CAP` 30
(over the cap, escalations fold into one summary message); auto replies held during quiet hours
(`JARVIS_AUTO_IN_QUIET_HOURS=false`); escalation alerts delivered **silently** at night instead of
held; nothing touches `QUESTION_BUDGET_PER_DAY`.

---

## 7. Configuration keys

Every key below goes to `miya/config.py` (with `Field` bounds or `Literal`), `.env.example`
(comment lines above, no inline comments, credentials blank), the README configuration table and
`miya/tools/doctor.py` `EXPECTED_ENV_KEYS`, in the WP named. Conditional doctor hints mirror
`TELETHON_HINTS` (`doctor.py:188-191`). Defaults keep core MIYA unchanged.

| Key | Default | WP | Meaning |
|---|---|---|---|
| `LOG_FORMAT` | `json` | JV-02 | `json` or `text` log lines |
| `PROMISE_REMIND_LEAD_MINUTES` | `60` | JV-03 | Remind timed items this long before `due_at` |
| `RECEIPT_MATCH_MINUTES` | `30` | JV-05 | Bank row must be within this of `paid_at` |
| `RECEIPT_BANK_WAIT_MINUTES` | `60` | JV-05 | Wait this long before an unconfirmed verdict |
| `RECEIPT_STALE_HOURS` | `72` | JV-05 | A `paid_at` older than this is `stale_date` |
| `SHEETS_ENABLED` | `false` | JV-07 | Mirror switch |
| `SHEETS_SERVICE_ACCOUNT_JSON` | `./secrets/sheets_service_account.json` | JV-07 | Key file |
| `SHEETS_SPREADSHEET_ID` | (blank) | JV-07 | Target spreadsheet |
| `SHEETS_SYNC_SECONDS` | `60` | JV-07 | Flush interval |
| `REPORT_TIME` | `20:00` (was `19:00`) | JV-08 | Evening summary |
| `SAFETY_EXTRA_SAFE_DOMAINS` | (blank) | JV-09 | Owner-verified domains added to the look-alike allowlist; never loosens file rules |
| `JARVIS_ENABLED` | `false` | JV-11 | Master switch |
| `JARVIS_MODE` | `draft` | JV-11 | Ceiling: `off`, `shadow`, `draft`, `auto` |
| `JARVIS_DRAFT_DAILY_CAP` | `40` | JV-11 | Draft cards per day |
| `JARVIS_AUTO_DAILY_CAP` | `20` | JV-11 | Autonomous sends per day |
| `JARVIS_PER_CHAT_DAILY_CAP` | `6` | JV-11 | AI sends per chat per day |
| `JARVIS_SEND_MIN_GAP_SECONDS` | `20` | JV-11 | Minimum gap between sends per account |
| `JARVIS_DRAFT_EXPIRY_HOURS` | `12` | JV-11 | A draft older than this is never sent |
| `JARVIS_DAILY_COST_CAP_USD` | `3.00` | JV-11 | Above it: escalate-only, no model calls |
| `STYLE_SAMPLE_SIZE` | `300` | JV-14 | Owner messages sampled |
| `STYLE_MIN_SAMPLES` | `50` | JV-14 | Below it, no profile is built |
| `KB_MATCH_THRESHOLD` | `0.75` | JV-15 | Hybrid retrieval threshold |
| `JARVIS_DEBOUNCE_SECONDS` | `45` | JV-16 | Bursts collapse to one draft |
| `JARVIS_OWNER_FIRST_SECONDS` | `120` | JV-16 | Owner gets this long to answer first |
| `JARVIS_MAX_MESSAGE_AGE_SECONDS` | `900` | JV-16 | Older inbound never gets an AI reply |
| `JARVIS_MAX_AI_TURNS` | `2` | JV-16 | Consecutive AI turns without the owner |
| `JARVIS_HISTORY_TURNS` | `8` | JV-16 | Thread turns the responder sees |
| `JARVIS_TAKEOVER_MINUTES` | `120` | JV-17 | AI silence after the owner writes |
| `JARVIS_ESCALATION_SNOOZE_MINUTES` | `60` | JV-18 | «1 soatdan keyin eslat» |
| `JARVIS_SNOOZE_MAX` | `3` | JV-18 | Then the escalation goes to the brief |
| `JARVIS_ESCALATION_DAILY_CAP` | `30` | JV-18 | Then alerts fold into one message |
| `TELEGRAM_SEND_ENABLED` | `false` | JV-19 | Starts the sender task |
| `USERBOT_FLOOD_SLEEP_SECONDS` | `5` | JV-19 | Client `flood_sleep_threshold` while the sender runs (60, Telethon's default, when it does not) |
| `JARVIS_REPLY_DELAY_MIN_SECONDS` | `3` | JV-19 | Pre-send delay lower bound |
| `JARVIS_REPLY_DELAY_MAX_SECONDS` | `7` | JV-19 | Upper bound; validator min ≤ max |
| `JARVIS_TYPING_CHARS_PER_SECOND` | `6` | JV-19 | Typing time per character |
| `JARVIS_TYPING_MAX_SECONDS` | `15` | JV-19 | Typing time cap |
| `JARVIS_GRADUATION_MIN_DAYS` | `7` | JV-23 | Days in draft before an intent is offered; also the gap between two offers |
| `JARVIS_GRADUATION_MIN_DRAFTS` | `30` | JV-23 | Drafts the owner decided on before an offer |
| `JARVIS_GRADUATION_MIN_APPROVAL_PCT` | `90` | JV-23 | Share of them approved without an edit, rounded down |
| `JARVIS_AUTO_IN_QUIET_HOURS` | `false` | JV-23 | Autonomous rows go out in the quiet hours too |
| `GMAIL_ENABLED` | `false` | JV-26 | Read switch |
| `GMAIL_TOKEN_JSON` | `./secrets/gmail_token.json` | JV-26 | Separate from the calendar token |
| `GMAIL_PULL_MINUTES` | `2` | JV-26 | `history.list` poll |
| `GMAIL_SEND_ENABLED` | `false` | JV-27 | Send switch |
| `GMAIL_DAILY_SEND_CAP` | `30` | JV-27 | |
| `WHATSAPP_ENABLED` | `false` | JV-28 | Sidecar and health gating |
| `WHATSAPP_BRIDGE_TOKEN` | (blank) | JV-28 | ≥ 32 chars, distinct from other tokens; required when enabled |
| `WHATSAPP_PHONE` | (blank) | JV-28 | Digits only, for the pairing code |
| `WHATSAPP_STALE_MINUTES` | `10` | JV-28 | Heartbeat staleness |
| `WHATSAPP_SEND_ENABLED` | `false` | JV-30 | Send switch |
| `WHATSAPP_DAILY_SEND_CAP` | `20` | JV-30 | |

The sidecar's compose `environment:` gets only `MIYA_API_URL`, `WHATSAPP_BRIDGE_TOKEN` and
`WHATSAPP_PHONE`, never `env_file: .env`. `MIYA_API_URL` is a literal in `docker-compose.yml`
(`http://api:8000`), not a Settings key and not a `.env` line, so it needs no `.env.example`,
README or doctor entry; the other two are interpolated from `.env` and are in the table above.
The EXEC/ARCHIVE blocklists are code constants.

No key outside this table is introduced by any WP. `PAYMENT_OWN_CARDS`, `EXTRACT_MODEL_PRICE`,
`REASON_MODEL_PRICE` and `REPORT_TIME` already exist; `MATCH_THRESHOLD` is the code constant
`people.MATCH_THRESHOLD`, not a setting.

---

## 8. Work package index

| Id | Title | Priority | Depends on |
|---|---|---|---|
| JV-01 | Dangerous-file shield on every ingest path | P0 | — |
| JV-02 | Structured logging, backoff helper, type-check gate | P1 | — |
| JV-03 | Promise and task deadlines keep the time of day | P0 | JV-02 |
| JV-04 | Structured receipt reading and owner receipt card | P0 | JV-01, JV-02 |
| JV-05 | Receipt bank cross-check and fake-receipt flags | P1 | JV-04 |
| JV-06 | Owner questions from SQL: promises made, who wrote | P1 | JV-03, JV-10 |
| JV-07 | Google Sheets one-way mirror | P1 | JV-02, JV-03, JV-04 |
| JV-08 | Evening summary at 20:00; timed deadlines and receipts in reports | P1 | JV-03, JV-05 |
| JV-09 | Spam and scam message shield | P1 | JV-01 |
| JV-10 | Authorship tagging: MIYA-sent messages are not the owner's words | P0 | — |
| JV-11 | Jarvis core: outbox chokepoint, escalations, policy, caps, kill switch | P0 | JV-02, JV-09, JV-10 |
| JV-12 | Personal deny-list and group reply policy | P0 | JV-11 |
| JV-13 | Red-line guard and stall templates | P0 | JV-03 |
| JV-14 | Owner style profile, examples, per-person tone | P0 | JV-10, JV-13 |
| JV-15 | Business knowledge base | P1 | JV-13 |
| JV-16 | Responder pipeline in shadow mode | P0 | JV-09, JV-11…JV-15 |
| JV-17 | Owner-first window, takeover, draft invalidation | P0 | JV-16 |
| JV-18 | Draft and escalation cards, three buttons, 1-hour snooze | P0 | JV-17 |
| JV-19 | Telegram sender: the narrow exception to read-only | P0 | JV-02, JV-18 |
| JV-20 | Admin-bot command mode with confirmed and scheduled sends | P1 | JV-06, JV-19 |
| JV-21 | Jarvis health, /holat lines, breaker alerts | P1 | JV-19 |
| JV-22 | Jarvis sections in the 09:00 and 20:00 reports | P1 | JV-07, JV-08, JV-18 |
| JV-23 | Rollout controller: per-intent graduation and demotion | P1 | JV-19, JV-21 |
| JV-24 | Multi-channel identity schema | P2 | JV-11 |
| JV-25 | Windows, extraction, recall, purge for any chat kind | P2 | JV-24 |
| JV-26 | Gmail read and extract | P2 | JV-02, JV-04, JV-09, JV-25 |
| JV-27 | Gmail reply through the outbox | P2 | JV-18, JV-23, JV-26 |
| JV-28 | WhatsApp sidecar skeleton: pairing, reconnect, heartbeat, bridge auth | P2 | JV-21, JV-24 |
| JV-29 | WhatsApp ingest (read-only) | P2 | JV-01, JV-04, JV-09, JV-25, JV-28 |
| JV-30 | WhatsApp send through the outbox | P2 | JV-23, JV-29 |
| JV-31 | Owner guide and decision record | P2 | JV-19 |

Make one commit per work package, first line `JV-NN: <imperative summary>`, in numeric order
with one exception: **JV-10 lands right after JV-02**, because JV-06 is its first consumer. The
commit order is therefore JV-01, JV-02, JV-10, JV-03 … JV-09, JV-11 … JV-31; every WP's
dependencies precede it in that order (the graph is acyclic). The migration chain follows the
same order without gaps: 0022 (JV-03), 0023 (JV-04), 0024 (JV-07), 0025 (JV-11), 0026 (JV-14),
0027 (JV-15), 0028 (JV-23), 0029 and 0030 (JV-24), 0031 (JV-25), each `down_revision` the
previous one. JV-10
must be merged before JV-19; JV-12 before the first draft card is ever shown.

### 8.1 Coverage: every spec requirement and owner decision has a WP

| Requirement (spec or decision) | Work packages |
|---|---|
| Telegram userbot reads all incoming and outgoing messages in real time | existing; JV-10 (authorship), JV-17 (read/composing/delete signals) |
| WhatsApp Web (Baileys sidecar), read and write | JV-24, JV-25, JV-28, JV-29, JV-30 |
| Gmail read and write (incoming and the owner's sent mail) | JV-24, JV-25, JV-26 (3a), JV-27 |
| StyleCloner: last 300 outgoing private messages → profile + few-shot examples in the prompt | JV-10, JV-14, JV-16 |
| Autonomous replies to new and existing contacts: greet, ask how to help, gather details, general questions | JV-09 (new contacts not blocked), JV-15, JV-16, JV-23 |
| RED LINE 1 (no promise, deadline, guarantee) and RED LINE 2 (no price, discount, terms, payment confirmation) | JV-13, JV-16, JV-11 gate (re-guard at claim) |
| Escalation: stall in the owner's style, immediate alert with summary, client info, three buttons | JV-13 (stalls), JV-18 (1a immediate alert, cards, buttons, 1-hour snooze) |
| Voice notes from Telegram, WhatsApp and the admin bot transcribed | existing (Telegram, bot); JV-18 (voice reply to a card); JV-20 (voice commands); JV-29 (WhatsApp) |
| Images: receipts → amount, date, sender; owner notified; Sheets Receipts tab | JV-04, JV-05, JV-07, JV-26 (mail images), JV-29 (WhatsApp images) |
| Promise/task extraction from incoming and the owner's outgoing text and voice, with time of day, client, platform, context | existing; JV-03 (time), JV-10, JV-25 (other channels), JV-26 3a (sent mail) |
| Real-time Google Sheets: Tasks_Promises, Client_FollowUps, Receipts | JV-07, JV-22 |
| Anti-ban: mark read, random 3-7 s delay, typing proportional to length | JV-19 (Telegram), JV-30 (WhatsApp) |
| Noise filter: ignore channels, bots, unapproved groups; DMs and whitelisted groups only | existing group monitors; JV-12 (§5.4), JV-20 (`set_group_monitor`), JV-26 (mail filters), JV-29 (WhatsApp groups off) |
| Admin bot as conversational Jarvis by text or voice, with tools: query DB, send on Telegram/WhatsApp/Gmail, update Sheets, manage whitelists | JV-06, JV-20 |
| Morning brief 09:00 (tasks, due promises, unanswered chats); evening summary 20:00 | JV-08, JV-22 |
| Coding rules: no placeholders, strong typing, try/except, exponential backoff, structured logs, never crash | JV-02, rules 25-26, JV-19 (sender containment), JV-28 5a (Node) |
| Decision 1: auto-reply to greetings, info-gathering, general questions; stall and escalate on money/deadline/promise | JV-13, JV-16, JV-18, JV-23 |
| Decision 2: write to everyone except owner-marked personal chats; groups off unless enabled | JV-12, JV-11 gate, JV-20 |
| Decision 3: Telegram sending, WhatsApp read/write, Gmail read/write, Sheets mirror | JV-19, JV-26…JV-30, JV-07 |
| Decision 4: never download or open APK/executables/installers/archives; warn; never answer spam/scam | JV-01, JV-09, JV-11 gate (safety line), JV-26 4, JV-29 |
| Decision 5: careful rollout, drafts with daily caps for 1-2 weeks, gradual autonomy, core MIYA independent of Jarvis and bans | §6, JV-11 (caps, kill switch, 5b), JV-16 (shadow), JV-18 (copy mode), JV-21 (breakers), JV-23 |

**Every send path goes through the one chokepoint.** There are exactly three adapters: the
Telegram sender (JV-19), the WhatsApp bridge claim (JV-30) and `gmail_send` (JV-27). Their only
producers are `outbox.create` calls from the responder (JV-16), escalation replies (JV-18) and
owner commands (JV-20). All three adapters obtain rows only from `outbox.claim_ready`, which
runs the §3.2 gate (kill switches, rollout mode, personal deny-list and hard exclusions,
safety shield, red-line re-guard, caps, cost cap); JV-11 5, JV-19 4 and JV-29's Node test make
any other write call fail CI.

---

## 9. Work packages

### Phase 1 — protect and help, no outbound (days 0-4)

#### JV-01 — Dangerous-file shield on every ingest path

**Priority** P0 · **Depends on** — · **Migration** none

**Goal.** Owner rule 4 now: never download, open or offer to fetch an executable, installer or
archive; warn the owner once.

**Files.** `miya/services/safety.py` (new, pure), `miya/services/media_policy.py`,
`miya/userbot/main.py`, `miya/bot/handlers.py`, `miya/services/documents.py`,
`miya/worker/main.py`, `miya/bot/replies.py`, `docs/ornatish.md`, `docs/owner-decisions.md`,
`tests/test_safety_files.py`, `tests/test_media_policy.py`, `tests/test_media_approval.py`.

**Behaviour.**
1. `safety.normalise_filename(name)`: NFKC; dot look-alikes (U+FF0E, U+2024, U+FE52, U+3002,
   U+2E33) → `.`; strip bidi and zero-width characters; casefold; collapse spaces around dots;
   strip trailing dots and spaces.
2. `safety.classify_attachment(name, mime, size) -> Verdict` returning `exec`, `archive`, `macro`,
   `html` or `ok`. EXEC and ARCHIVE suffix sets and the MIME blocklist are module constants seeded
   from the Gmail blocked-types list plus mobile types (`.apk .xapk .apks .apkm .aab .dex .ipa
   .mobileconfig .exe .msi .scr .js .bat .cmd .vbs .jar .lnk .iso .dmg …`; archives `.zip .rar .7z
   .tar .gz .tgz .xz .zst .\d{3} .r\d{2} …`). Double extensions anywhere in the name count.
3. `safety.sniff_magic(path, claimed_suffix)` for allowed types: the signature must match the
   suffix; a `PK` container is accepted only for `.docx/.xlsx/.xlsm` with `[Content_Types].xml`
   and no `AndroidManifest.xml`, `classes.dex` or `META-INF/MANIFEST.MF`; MZ, ELF, dex, Mach-O,
   OLE posing as OOXML are rejected. ZIP central-directory check: total uncompressed ≤ 100 MB,
   ratio ≤ 100.
4. `media_policy.plan_for`: the check runs **first** for documents, before the size branch at
   `media_policy.py:141-142`, returning `skip_reason='dangerous_file'`, `ask=False`.
   `forced_plan` (`:61-79`) refuses the same types. Add a `channel` parameter so the same policy
   serves Telegram, WhatsApp and Gmail.
5. Userbot `ingest_message`: store `media.risk` and `meta.safety`, set `meta.safety_notice`.
6. Bot `on_document` (`handlers.py:2620`), `/process` (`:1668-1712`) and `/kodlar` import
   (`:2196`): check **before** `_download` (`:168`). A dangerous file is never written to
   `MEDIA_DIR`.
7. Allowed documents get `sniff_magic` before parsing; macro documents are read as text only, with
   a warning.
8. Worker job `safety_notices` (1 min, `max_instances=1`, `coalesce=True`): sends each warning
   once, silently in quiet hours, never counted in the question budget, stamps
   `safety_notified` after delivery.
9. `docs/ornatish.md:42,63-66`: the APK install steps become one exception — MIYA Companion comes
   only from the MIYA bot, with its signing-certificate SHA-256 printed. Record owner rule 4 in
   `docs/owner-decisions.md`.

**Owner strings.**
- Chat warning: "⚠️ {ism} sizga «{fayl}» faylini yubordi ({chat}). Bu o'rnatiladigan dastur yoki
  arxiv — xavfli bo'lishi mumkin. Ochmang va o'rnatmang. MIYA uni yuklab olmadi."
- Second line: "Tanish odamdan kelgan bo'lsa ham ehtiyot bo'ling. Shubha bo'lsa, unga qo'ng'iroq
  qilib so'rang."
- Bot reply: "⚠️ «{fayl}» — o'rnatiladigan dastur yoki arxiv. MIYA bunday fayllarni yuklab olmaydi
  va ochmaydi. Ichidagi ma'lumot kerak bo'lsa, PDF yoki rasm qilib yuboring."
- Macro: "ℹ️ «{fayl}» makrosli hujjat. MIYA uni faqat matn sifatida o'qidi. Kompyuterda ochsangiz,
  makrosni yoqmang."
- Rule line: "MIYA ilovasi faqat MIYA botidan keladi; boshqa hech kimdan kelgan APK faylni
  o'rnatmang."

**Tests.** `test_small_apk_is_dangerous_not_unsupported`, `test_large_apk_never_creates_an_ask`,
`test_double_extension_and_rlo_names_blocked` (Rasmlar.jpg.apk, an RLO-spoofed name, `foto．apk`),
`test_forced_plan_refuses_dangerous`, `test_bot_document_handler_never_downloads_dangerous`
(assert `_download` not called), `test_ooxml_with_android_entries_rejected`,
`test_warning_sent_once`, `test_pdf_docx_xlsx_still_read`; update the `arxiv.zip` expectation in
`tests/test_media_policy.py:80-86` and `tests/test_media_approval.py:117-121`.

**Acceptance.** Tests green against real PostgreSQL; nothing lands in `MEDIA_DIR` for a blocked
type. Owner check: forwarding an `.apk` to the bot gives the warning; a large `.apk` in a chat
produces no download question.

---

#### JV-02 — Structured logging, a shared backoff helper and a type-check gate

**Priority** P1 · **Depends on** — · **Migration** none

**Goal.** Meet the spec's coding rules before any Jarvis code lands.

**Files.** `miya/logs.py` (new), `miya/services/retry.py` (new), `miya/worker/main.py`,
`miya/api/main.py`, `miya/bot/main.py`, `miya/userbot/main.py`, `miya/tools/backfill.py`,
`miya/services/extraction.py`, `miya/services/transcription.py`, `miya/services/embeddings.py`,
`miya/services/gcal.py`, `miya/config.py`, `pyproject.toml`, `requirements-dev.txt`, `Makefile`,
`.github/workflows/python-ci.yml`, `.env.example`, `README.md`, `miya/tools/doctor.py`,
`tests/test_logs.py`, `tests/test_retry.py`.

**Behaviour.**
1. `setup_logging(component)` replaces the six `basicConfig` sites (`worker/main.py:1311`,
   `api/main.py:63,101`, `bot/main.py:166`, `userbot/main.py:911`, `tools/backfill.py:294`). JSON
   lines when `LOG_FORMAT=json`, the current text format otherwise; each line carries `ts`,
   `level`, `logger`, `component`, `msg` and extras. A redaction filter drops extras named
   `text`, `body`, `raw_text`, `transcript`, `draft`.
2. `retry.with_backoff(fn, *, attempts, base, cap, retry_on)`: full-jitter exponential backoff,
   logs only the attempt number and the error class. Google calls use `execute(num_retries=N)`.
3. Backoff is applied to today's API calls, not only to new ones, without retrying twice: the
   shared model client (`extraction.get_client`, `extraction.py:54`) gets an explicit
   `max_retries=3` so its built-in exponential retry is visible and tested, and its call sites
   (`extraction.py:511,525`, `vision.py:62`, `rag.py:1268`, `recaps.py:730`,
   `profiles.py:262`, `planner.py:132`) are not wrapped again; the plain-HTTP calls
   (`transcription.py:115`, `embeddings.py:96`) are wrapped in `with_backoff` retrying only
   timeouts, connection errors, 429 and 5xx; `gcal.py:89,98` use `execute(num_retries=5)`. A
   final failure keeps each caller's current fallback (`needs_review`, deterministic report).
4. mypy strict for `miya/jarvis/**`, `miya/logs.py`, `miya/services/retry.py`, `safety.py`,
   `when.py`; `make typecheck`; a CI step after ruff; a pinned released mypy in
   `requirements-dev.txt`.
5. The rule 26 check: a test that scans the paths named there for placeholder markers and stub
   bodies; it runs from the first Jarvis commit on, so each later WP is held to it.

**Owner strings.** None.

**Tests.** `test_json_line_per_record`, `test_redaction_drops_body_extras`,
`test_backoff_retries_transient_then_succeeds` (time faked), `test_backoff_gives_up_on_permanent`,
`test_no_basicconfig_outside_logs` (AST), `test_model_client_max_retries_explicit`,
`test_transcription_retries_429_not_400`, `test_no_placeholders_in_new_code`.

**Acceptance.** CI green with the typecheck step; existing caplog tests pass; `LOG_FORMAT` in
`.env.example`, README and doctor.

---

#### JV-03 — Promise and task deadlines keep the time of day

**Priority** P0 · **Depends on** JV-02 · **Migration** `0022_promise_due_time` (down `0021_search_passages`)

**Goal.** «Ertaga 14:00 gacha hisoblab beraman» is stored as tomorrow 14:00, shown with its time
and reminded before the time.

**Files.** `alembic/versions/0022_promise_due_time.py`, `miya/db/models.py`,
`miya/services/extraction.py`, `miya/services/prompts.py`, `miya/services/persistence.py`,
`miya/services/records.py`, `miya/services/when.py` (new, pure), `miya/bot/formatting.py`,
`miya/services/reminders.py`, `miya/services/queries.py`, `miya/services/rag.py`,
`miya/config.py`, `pyproject.toml` (`when.py` joins the mypy `files`), `.env.example`,
`README.md`, `miya/tools/doctor.py`, `tests/test_due_time.py`, `tests/test_when.py`.

**Schema.** See §4.2 (0022).

**Behaviour.**
1. `ExtractedPromise` / `ExtractedTask` (`extraction.py:135-147, 182-189`) accept `YYYY-MM-DD` or
   `YYYY-MM-DDTHH:MM`, split into `due_date` and `due_time` instead of `_parse_iso_date`'s
   `text[:10]` truncation (`:73-84`). The prompt gains one line: when a time of day is said,
   output it.
2. `when.py` parses Uzbek and Russian phrases deterministically: `soat N`, `HH:MM`, `N gacha`,
   `kechki/ertalab N`, `ertaga`, weekdays, `завтра в 10`. Ambiguous input returns None; it never
   guesses.
3. `persistence.write_promise` (`:601-630`) and the inline task writer (`:919-929`) set
   `due_time`. `records.parse_date` (`:990-1018`) accepts a time, so `/tuzat p12 ertaga 14:00`
   works; each change writes a `due_time` history entry.
4. Timed items are reminded `PROMISE_REMIND_LEAD_MINUTES` before `due_at`, outside quiet hours.
   Date-only items keep their current rules. `due_items` and the promise renderers show the time.

**Owner strings.** Render "bugun 14:00", "ertaga 14:00", "dushanba 10:00". Reminder: "⏰ {vaqt} da
muddat: {ism}ga va'da — {matn}". List line: "⏰ {muddat} — {ism}: {matn}".

**Tests.** `test_extraction_keeps_time`, `test_date_only_unchanged`,
`test_due_time_requires_date_check`, `test_timed_reminder_fires_before_due`,
`test_parse_date_with_time`, `test_when_table` (kechki 6, soat 14:00, 14:00 gacha, ertalab 9,
завтра в 10, ambiguous → None).

**Acceptance.** `alembic check` clean; round trip; all `due_date` readers' tests pass unchanged.
Owner check: a voice note «ertaga soat 3 da tashlab beraman» shows «ertaga 15:00».

---

#### JV-04 — Structured receipt reading with an owner receipt card

**Priority** P0 · **Depends on** JV-01, JV-02 · **Migration** `0023_receipts`

**Goal.** Receipts from the bot, chats and image files give amount, date and time, payer, payee
card, issuer and reference, and the owner sees a receipt card.

**Files.** `alembic/versions/0023_receipts.py`, `miya/db/models.py`, `miya/services/vision.py`,
`miya/services/prompts.py`, `miya/services/receipts.py` (new), `miya/services/ingest.py`,
`miya/userbot/main.py`, `miya/services/media_policy.py`, `miya/bot/handlers.py`,
`miya/services/money_events.py`, `miya/services/claims.py`, `miya/services/people.py`,
`miya/services/purge.py`, `miya/worker/main.py`, `miya/bot/replies.py`, `tests/conftest.py`,
`tests/test_receipts_read.py`.

**Schema.** See §4.2 (0023).

**Behaviour.**
1. `vision.read_image` makes one EXTRACT_MODEL call with structured output and the prompted-JSON
   fallback (the `extraction.py:501-533` pattern), two attempts via `with_backoff`, never raises,
   and returns `VisionReading {description, is_receipt, receipt}`. `meta.vision` keeps the
   description, so existing extraction is unchanged. If `is_receipt`, a `receipts` row is written
   with the amount via `extraction.to_money` (Decimal).
2. A failed read: `state='needs_review'` plus `interaction.needs_review`. Never dropped.
3. Images sent as files (`.jpg .jpeg .png .webp`, after `sniff_magic`) route to vision in both the
   bot (`on_document`) and the userbot (`plan_for`). HEIC gets a hint.
4. A counterparty's money still goes through claims; the claim payload carries `receipt_at` and
   `claims._anchor` (`claims.py:1025`) prefers it. `receipt_photo` is added to
   `money_events.TYPED_SOURCES` (`money_events.py:86`), closing the photo-before-SMS double-book.
5. Bot photos get the card as its own message, sent before the reply (JV-05 edits it in place).
   Chat receipts get it from the worker job `receipt_notices` (1 min), deduped on `notified_at`.
6. `merge_into._fk_columns` and the purge planners cover `receipts`.

**Owner strings.**
- Card: "🧾 Chek · {kimdan} · {kanal}\nChekda: {summa} · {sana vaqt}\nTo'lovchi: {to'lovchi} →
  {qabul qiluvchi} ****{karta}\n{bank/ilova} · № {raqam}\nBank tasdig'i: ⏳ kutilmoqda"
- Footer: "Summa rasmdan o'qildi; hisobga faqat bank SMS yoki Payme tasdig'i bilan yoziladi."
- Failure: "🧾 Chek o'qilmadi — rasm aniq emas. /tekshir ro'yxatiga qo'ydim."
- HEIC: "Bu rasm formati (HEIC) o'qilmaydi — JPG qilib yuboring."

**Tests.** `test_receipt_reading_persisted_decimal`, `test_failed_read_needs_review`,
`test_image_document_goes_to_vision`, `test_counterparty_receipt_is_claim_with_receipt_at`,
`test_owner_photo_then_sms_not_double_booked`, `test_receipt_card_sent_once`,
`test_purge_and_merge_cover_receipts`.

**Acceptance.** Tests pass with fixture model payloads and no network. Owner check: a Payme
screenshot sent to the bot returns a card with date and time.

---

#### JV-05 — Receipt bank cross-check and fake-receipt flags

**Priority** P1 · **Depends on** JV-04 · **Migration** none

**Goal.** Each receipt becomes confirmed, waiting, unconfirmed or suspicious against bank SMS and
Payme; the card updates in place.

**Files.** `miya/services/receipts.py`, `miya/services/money_events.py`,
`miya/services/questions.py`, `miya/worker/main.py`, `miya/bot/keyboards.py`,
`miya/bot/handlers.py`, `miya/bot/replies.py`, `miya/config.py`, `.env.example`, `README.md`,
`miya/tools/doctor.py`, `tests/test_receipts_match.py`.

**Behaviour.**
1. `receipts.match_bank`: an income `Transaction` with `channel IS NOT NULL`, `voided_at IS NULL`
   (filter through `queries.ACTIVE_TXN`), exact amount and currency, within
   `RECEIPT_MATCH_MINUTES` of `paid_at`, not linked to another receipt. Called after each bank
   booking (`money_events.apply_reading`, next to `_match_bank_evidence`), on receipt creation and
   in `questions.auto_resolve`.
2. Job `receipt_check` (1 min) for waiting receipts older than `RECEIPT_BANK_WAIT_MINUTES`:
   `suspicious` only when the payee card is one of `money_events.own_cards` **and** the phone
   heartbeat is fresh, or for a duplicate image hash or reference, or a `paid_at` older than
   `RECEIPT_STALE_HOURS` or in the future. Otherwise `unconfirmed` — never "fake". A receipt of
   the owner's own payment (`direction='out'`) is never matched (only income confirms) or
   flagged (every reason and the suspicious line speak of money reaching the owner): it becomes
   `unconfirmed`, and its card carries no buttons. Another read of the same receipt that the bank
   already confirmed (the same image or issuer + reference, where either copy is the owner's own
   bot image) confirms it too; the bank row stays linked to the first copy only.
3. The card is edited in place via `notice_tg_message_id`. Buttons `rcp:ok:<id>` and `rcp:f:<id>`
   set owner verdicts under a row lock; neither books money; both write history.

**Owner strings.** "✅ Chek bankda tasdiqlandi: {kanal} {vaqt}." · "⏳ Bankdan tasdiq hali yo'q (SMS
kelmaydigan hisob bo'lishi mumkin)." · "⚠️ Chek shubhali: {sabab}. Pul kartangizga tushganini
ilovadan tekshiring." Reasons: "bu chek avval ham yuborilgan ({sana})", "chekdagi sana eski
({sana})", "chekdagi sana kelajakda", "karta sizniki, lekin bankdan xabar kelmadi". Buttons: "✅ Pul
keldi" / "🚩 Soxta".

**Tests.** `test_match_by_paid_at_not_message_time`, `test_old_resent_receipt_not_matched`,
`test_duplicate_image_flagged`, `test_unmatched_without_own_card_is_unconfirmed`,
`test_phone_stale_never_suspicious`, `test_owner_verdict_buttons_idempotent`.

**Acceptance.** Matching is SQL with Decimal and never mixes currencies. Owner check: a genuine
receipt turns ✅ when the SMS arrives.

---

#### JV-06 — Owner questions answered from SQL: promises made and who wrote

**Priority** P1 · **Depends on** JV-03, JV-10 · **Migration** none

**Goal.** «Bugun kimga nima va'da bergandim?» and «Kecha kimlar yozdi?» get exact answers by text
or voice.

**Files.** `miya/services/queries.py`, `miya/services/rag.py`, `miya/bot/handlers.py`,
`miya/bot/replies.py`, `tests/test_jarvis_reads.py`, `tests/test_person_rag.py`,
`tests/test_code_lookup.py`.

**Behaviour.**
1. `queries.promises_made(window, made_by='me')`: by the source interaction's `occurred_at` in the
   local day window, with person, text, `due_at` and channel.
2. `queries.who_wrote(window)`: incoming messages in private chats plus groups where `meta.to_me`,
   grouped per person with count, last time and an answered flag that uses the JV-10 predicate.
3. Both added to `rag.TOOLS` and `SQL_TOOLS` (`rag.py:1025`); the pinned tool-name tests are
   updated.
4. `rag.date_hints` gains exact local-day windows (bugun, kecha, weekdays).
5. A deterministic fast path in `on_text` and `_spoken_mode` renders these two canonical questions
   with no model call. `rag.answer_full` wraps the model call in `with_backoff`.

**Owner strings.** "📌 {kun} bergan va'dalaringiz ({n}):\n{i}. {ism} — {matn} · muddat: {muddat} ·
{kanal}" · empty: "{kun} hech kimga va'da bermagansiz (yozuvlarda topilmadi)." · "📨 {kun} sizga
yozganlar ({n}):\n{i}. {ism} — {soni} ta xabar, oxirgisi {vaqt} · {javob berilgan|javobsiz}" ·
empty: "{kun} sizga hech kim yozmagan (yozuvlarda topilmadi)." · rows left out (by the query's
limit or the message size): "<i>… va yana {k} ta</i>", with `{n}` always the full count. `{kun}`
is Bugun, Kecha or "{Hafta kuni} kuni ({sana})".

**Tests.** `test_promises_made_day_window_tashkent`, `test_who_wrote_private_and_addressed_only`,
`test_fast_path_no_model_call`, `test_voice_routes_to_fast_path`, `test_rag_backoff_retries`.

**Acceptance.** Answers match SQL fixtures exactly. Owner check: the voice question returns the
list in under 3 s.

---

#### JV-07 — Google Sheets one-way mirror

**Priority** P1 · **Depends on** JV-02, JV-03, JV-04 · **Migration** `0024_sheet_rows`

**Goal.** The owner's spreadsheet stays at most a minute behind SQL. It is never read back.

**Files.** `alembic/versions/0024_sheet_rows.py`, `miya/db/models.py`, `miya/services/sheets.py`
(`SheetsAPI` ABC + Google implementation, like `gcal.CalendarAPI`), `miya/services/sheets_sync.py`,
`miya/services/purge.py`, `miya/worker/main.py`, `miya/services/health.py`, `miya/bot/replies.py`,
`miya/config.py`, `.env.example`, `README.md` (config and egress), `miya/tools/doctor.py`,
`tests/conftest.py`, `tests/test_sheets_sync.py`, `tests/test_health.py`.

**Behaviour.**
1. Service-account auth (`SHEETS_SERVICE_ACCOUNT_JSON`, `spreadsheets` scope); the spreadsheet is
   shared with that account as Editor. The sync client runs via `asyncio.to_thread`.
2. Job `sheets_sync` every `SHEETS_SYNC_SECONDS`: build rows per tab from SQL (amounts as strings
   plus a separate currency column), hash each, write changed rows with one `values.batchUpdate`
   per tab at explicit ranges (idempotent), new rows take the next `row_number`,
   `valueInputOption=RAW` (no formula injection). Missing tabs and headers are created by one
   `spreadsheets.batchUpdate`. Purged entities become tombstones and their rows are cleared.
   Failures retry with backoff (`execute(num_retries=5)`), recording the error code.
3. A no-op when `SHEETS_ENABLED=false` or the key file is missing. Beats `sheets`. Problem
   `sheets_failing` only when enabled (with a recovery text and a test case). `/holat` line.
4. README "Documented egress" gains Google Sheets.

As built: rows are planned and committed before any network call and recorded after it, with no
transaction held during the call; a row's outcome is written only if nothing changed it since it
was planned. `Tasks_Promises` holds every promise and task; `Client_FollowUps` every person with
an active client code, an open promise or an unanswered question (`loops.unanswered_questions`),
whose row is cleared when none of those holds any more and reused if one does again; `Receipts`
every receipt not `rejected`, with the bank's own record (`x<id> · amount currency · time ·
bank`) in `Bank yozuvi` only when a bank row confirms it. Cell IDs are the bot's refs (`p`, `t`)
plus `r<id>` and `k<id>`; times are `YYYY-MM-DD HH:MM` local; amounts `1250000.00`. `Ochiq
eskalatsiya` stays blank until JV-22 fills it. Purge marks the rows of deleted entities as
tombstones in its own transaction (`sheets_sync.tombstone_missing`, never raising). Missing tabs
are added with a frozen header row; a grid too small is grown in the same
`spreadsheets.batchUpdate`; each `values.batchUpdate` also rewrites the tab's header, and the
first run in a process writes the header of a tab that exists but has nothing due. A failed
row waits `SHEETS_SYNC_SECONDS`, doubling, up to an hour, counted from the end of the write; while
any row waits out such a backoff a run with nothing due still beats failing. A tombstone keeps no
digest of the cells it held (`content_hash` is blanked). The `sheets` beat carries `ok`,
`error`, `last_ok_at` and `failing_since`; `sheets_failing` fires once `failing_since` is 10
minutes old, and `{sabab}` words the error code. `make doctor` requires `SHEETS_SPREADSHEET_ID`
when `SHEETS_ENABLED=true` (`SHEETS_HINTS`, like `TELETHON_HINTS`).
With `SHEETS_ENABLED=true` but no key file or spreadsheet id, the job makes no network call and
writes no rows; it writes only a failing `sheets` beat (`key_file_missing` / `no_spreadsheet_id`),
so `sheets_failing` names the cause after 10 minutes. A tab that cannot be built from SQL keeps
its rows as they are while the other tabs still go out, and the run reports `plan_failed`.

**Owner strings.**
- Tasks_Promises: "ID | Tur | Kim | Kimga | Matn | Muddat | Holat | Kanal | Manba vaqti | Kontekst"
- Client_FollowUps: "ID | Mijoz | Kod | Kanal | Oxirgi xabar | Javobsiz | Ochiq eskalatsiya |
  Ochiq va'dalar | Keyingi muddat"
- Receipts: "ID | Chekdagi vaqt | Summa | Valyuta | To'lovchi | Qabul qiluvchi karta | Bank/ilova |
  Chek № | Kimdan | Kanal | Bank tasdig'i | Bank yozuvi"
- `/holat`: "📊 Sheets: oxirgi yangilanish {vaqt}"
- Problem: "📊 Google Sheets yangilanmayapti: {sabab}. Jadval xizmat hisobiga «Muharrir» sifatida
  ulashilganini tekshiring." Recovery: "✅ Google Sheets bilan sinxronlash tiklandi"

**Tests.** `test_rows_written_once_with_fake_api`, `test_change_detected_by_hash`,
`test_retry_after_timeout_no_duplicate`, `test_purge_clears_row`, `test_raw_value_option`,
`test_disabled_is_noop`, `test_receipt_said_and_bank_columns_separate`, a `sheets_failing` health
case.

**Acceptance.** Fake-API tests green; `alembic check` clean. Owner check: a new promise appears
in Tasks_Promises within a minute.

---

#### JV-08 — Evening summary at 20:00; timed deadlines and receipts in reports

**Priority** P1 · **Depends on** JV-03, JV-05 · **Migration** none

**Goal.** The spec's 20:00 evening summary; the 09:00 brief shows today's timed deadlines and
the 20:00 recap shows the day's receipt status.

**Files.** `miya/config.py`, `.env.example`, `README.md`, `miya/services/brief.py`,
`miya/services/recaps.py`, `miya/bot/replies.py`, `tests/test_recap_morning.py`,
`tests/test_brief_jarvis_lines.py`.

**Behaviour.** `REPORT_TIME` default `20:00` (`config.py:401`, `.env.example`, README row and the
README diagram line 45); validators (`config.py:450-480`) unchanged; the pinned «19:00 dan keyin»
text (`tests/test_recap_morning.py:145`) updated. Brief gains «Bugungi muddatlar» ordered by
`due_at`; the recap gains a receipts line counted in SQL.

**Owner strings.** "⏰ Bugungi muddatlar:\n{vaqt} — {ism}: {matn}" · "🧾 Bugun {n} ta chek: {k} tasi
bankda tasdiqlandi, {m} tasi kutilmoqda, {s} tasi shubhali."

**Tests.** `test_report_default_2000`, `test_brief_orders_timed_items`,
`test_recap_receipt_counts_from_sql`.

**Acceptance.** Validator tests pass; the evening report arrives at 20:00 Tashkent.

---

#### JV-09 — Spam and scam message shield with owner warnings

**Priority** P1 · **Depends on** JV-01 · **Migration** none

**Goal.** Spam and scam messages are flagged, never answered and kept out of «javobsiz» lists;
the owner is warned about dangerous ones.

**Files.** `miya/services/safety.py`, `miya/userbot/main.py`, `miya/services/ingest.py`,
`miya/services/loops.py`, `miya/services/questions.py`, `miya/worker/main.py`,
`miya/bot/replies.py`, `miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`,
`tests/fixtures/scam_corpus.py`, `tests/test_safety_messages.py`.

**Behaviour.**
1. `safety.extract_urls` (raw text, Telegram `MessageEntityTextUrl` where the visible text names a
   different domain is a hard hit, URL buttons) and `safety.link_verdict` (brand token on a
   non-allowlisted domain, punycode, mixed-script label via `codes.LOOKALIKE`, bare IP, userinfo
   tricks, rapidfuzz distance ≤ 2 from an allowlisted domain). The allowlist is a code constant
   plus `SAFETY_EXTRA_SAFE_DOMAINS`.
2. `safety.scam_verdict(features)`: hard rules (any one quarantines) — EXEC attachment or EXEC/
   archive URL; archive from a non-owner; a code or card-data request (reuse
   `sms_money.OTP_MARKERS` with request verbs); Telegram `User.scam` / `User.fake`; a look-alike
   payment/bank/government domain; photo-lure wording plus a file or link. Soft signals
   (weighted) — shorteners, free hosts, cheap TLDs, lure and urgency lexicons (seeded from
   `sms_money.ADVERT_MARKERS`), mass-forward shape, `fwd_from` / `via_bot_id`, first-ever
   non-contact, a script switch versus the contact's history. Possible-hijack: a known contact
   suddenly sends a file, archive, short link or loan request.
3. Features captured in `_counterparty` (`userbot/main.py:319-346`). Runs before any model call.
4. Verdict in `meta.safety`. Spam and scam rows are excluded from unanswered and nudge
   candidates and from the responder. Owner decision 2 lets the AI answer new contacts, so a
   single soft signal never blocks: «first-ever non-contact» alone (a new client writing first)
   scores below the auto-reply block threshold and is answered like anyone else; only a
   combination of soft signals reaches it. Warnings for scam and possible-hijack, once per chat per day,
   via `safety_notices`.

**Owner strings.** "🚫 {ism}dan kelgan xabar firibgarlikka o'xshaydi ({sabab}). Javob bermang,
havolani ochmang." · "⚠️ {ism}ning akkaunti buzilgan bo'lishi mumkin: odatda bunday yozmaydi. Unga
qo'ng'iroq qilib tekshiring, pul o'tkazmang." · brief: "🚫 Kecha {n} ta spam/firibgar xabar
e'tiborsiz qoldirildi."

**Tests.** `test_corpus_hard_hits_quarantined`, `test_benign_business_messages_pass`,
`test_lookalike_payme_domain_hard_hit`, `test_spam_not_in_unanswered`, `test_warning_once_per_day`,
`test_runs_before_model`, `test_new_contact_alone_not_blocked`.

**Acceptance.** Corpus precision/recall thresholds recorded in the test; a seeded test message
produces the warning.

---

#### JV-10 — Authorship tagging: MIYA-sent messages are never the owner's words

**Priority** P0 · **Depends on** — · **Migration** none

**Goal.** Before anything is sent, every consumer can tell genuine owner messages from MIYA-sent
ones.

**Files.** `miya/services/authorship.py` (new), `miya/services/windows.py`,
`miya/services/prompts.py`, `miya/services/loops.py`, `miya/services/recaps.py`,
`miya/services/passages.py`, `miya/services/recall.py`, `miya/userbot/main.py`,
`tests/test_authorship.py`.

**Behaviour.**
1. `authorship.owner_words_clause()` (SQL) and `is_owner_words(row)` are the one predicate:
   `direction='out' AND NOT (metadata ? 'sent_by' AND metadata->>'authored_by' = 'ai')`.
2. `windows._speaker` (`windows.py:112-123`) renders AI-authored rows as `[MIYA]`; the extraction
   prompt says `[MIYA]` lines are automatic replies and never the owner's assertions. Owner-authored
   rows sent via MIYA stay `[ME]` (they approved the exact text).
3. `loops._last_outgoing_per_chat` (`loops.py:421-436`) ignores AI rows, so escalated chats stay
   unanswered; `recaps.py:318,335,634` split owner and AI counts; `passages.py:100,186` set
   `from_owner=false`, speaker MIYA; recall citations are labelled.
4. When ingest sees an outgoing message whose external id matches an outbox row, it tags it.

**Owner strings.** Citation: "🤖 MIYA nomidan (avtomatik)". Recap: "MIYA: {n} ta avtomatik javob".

**Tests.** `test_ai_row_rendered_as_miya`, `test_owner_command_row_is_me`,
`test_ai_stall_not_extracted_as_owner_promise`, `test_ai_reply_not_closing_loop`,
`test_passage_not_from_owner`, `test_predicate_used_everywhere` (grep over the listed modules).

**Acceptance.** Green; no behaviour change for rows without the new keys.

---

### Phase 2 — the act layer without a sender (days 4-10)

#### JV-11 — Jarvis core: outbox chokepoint, escalations, policy, kill switch, caps, audit

**Priority** P0 · **Depends on** JV-02, JV-09, JV-10 · **Migration** `0025_jarvis_core`

**Goal.** A proven gateway exists with no channel attached, tested with a fake adapter.

**Files.** `alembic/versions/0025_jarvis_core.py`, `miya/db/models.py`, `miya/jarvis/__init__.py`,
`chatkey.py`, `policy.py`, `caps.py`, `gate.py`, `outbox.py`, `costs.py`, `miya/services/people.py`,
`miya/services/purge.py`, `miya/bot/handlers.py`, `miya/bot/replies.py`, `miya/config.py`,
`.env.example`, `README.md`, `miya/tools/doctor.py`, `docs/owner-decisions.md`,
`tests/conftest.py`, `tests/test_outbox.py`, `tests/test_jarvis_policy.py`,
`tests/test_command_menu.py`.

**Schema.** See §4.2 (0025).

**Behaviour.**
1. `outbox` exposes `create`, `approve` (row lock; a second tap raises `AlreadyAnswered`),
   `cancel`, `expire`, `claim_ready`, `complete`, `fail` and `reconcile`, exactly as §3.2. Every
   transition appends history. `create` derives `idempotency_key` from (origin, trigger or command
   interaction, chat_key, body hash).
2. `caps` counts in SQL from `reminders.day_start`; `costs` sums `usage_log` for Jarvis operations
   against `JARVIS_DAILY_COST_CAP_USD`.
3. Kill switches: `JARVIS_ENABLED`, the global pause and channel pauses in `jarvis_policy`.
4. Commands: `/jarvis` status; `/jarvis toxtat` pauses everything; `/jarvis yoq [kanal]` resumes;
   `/jarvis tarix` lists the last 20 actions. Added to `HELP` and `COMMAND_MENU`.
5. An AST test: only `miya/jarvis/outbox.py` assigns `OutboxMessage.state` or executes
   insert/update against `outbox_messages` (ORM or raw SQL text).
5a. `claim_ready` implements every gate line of §3.2, in that order, as one pure
   `gate.evaluate(row, ctx) -> Decision` fed by SQL in the claim transaction, so the same
   function serves the Telegram sender, the WhatsApp bridge claim and `gmail_send`. It reads
   `meta.safety` and `media.risk` (JV-01, JV-09) and the `reply_rules` table (rules created by
   JV-12; until then only the hard exclusions apply). A blocked row gets `state='blocked'` and a
   `last_error` code, never a silent skip.
5b. With `JARVIS_ENABLED=false` no Jarvis job is registered, the userbot starts no sender task
   and core MIYA paths do not import `miya.jarvis` at startup (rule 20).
6. Merge and purge cover every new table (`reply_rules` keeps address-only rules on purge).
7. Record the owner decisions of 2026-10-20 verbatim in `docs/owner-decisions.md`.

**Owner strings.** Status: "🤖 Jarvis: {rejim}\nBugun: {q} ta qoralama · {y} ta yuborildi · {e} ta
eskalatsiya\nLimit: {used}/{cap}\nKanallar: Telegram {holat} · WhatsApp {holat} · Gmail {holat}".
Mode labels: "o'chiq", "kuzatuv (faqat yozib boradi)", "qoralama (har javobni siz tasdiqlaysiz)",
"avtomatik". Pause: "⛔ Jarvis to'xtatildi. Hech qanday xabar yuborilmaydi. Qayta yoqish: /jarvis
yoq". Resume: "✅ Jarvis yoqildi ({rejim} rejimi)."

**Tests.** `test_state_machine_transitions`, `test_idempotency_key_unique`,
`test_concurrent_claim_skip_locked`, `test_pause_blocks_claim_of_approved_row`,
`test_guard_hash_mismatch_reguards`, `test_caps_count_from_day_start`,
`test_expired_lease_goes_unknown_not_resent`, `test_sent_requires_approved_check`,
`test_ai_row_cannot_be_approved_without_pass_check` (DB CHECK), `test_only_outbox_writes_state`
(AST), `test_purge_and_merge_cover_jarvis_tables`, `test_never_writes_question_log`,
`test_gate_blocks_ai_row_in_quarantined_chat`, `test_gate_blocks_ai_row_after_dangerous_file`,
`test_gate_personal_rule_blocks_ai_allows_warned_owner_row`,
`test_gate_demoted_intent_blocks_auto_row`, `test_gate_owner_rows_count_towards_account_gap`,
`test_gate_rejects_non_text_payload`, `test_gate_order_matches_plan`,
`test_jarvis_off_registers_nothing`.

**Acceptance.** Round trip and `alembic check` clean; the fake adapter sends exactly once under
concurrency.

---

#### JV-12 — Personal deny-list and group reply policy

**Priority** P0 · **Depends on** JV-11 · **Migration** none

**Goal.** Before the first draft, the owner can mark family and friends personal; groups never get
replies unless enabled.

**Files.** `miya/jarvis/policy.py`, `miya/bot/handlers.py`, `miya/bot/keyboards.py`,
`miya/bot/replies.py`, `miya/services/people.py`, `tests/test_reply_rules.py`,
`tests/test_command_menu.py`.

**Behaviour.**
- `/shaxsiy <ism|kod>` marks a person or chat `never` at once (resolved with `_lookup`,
  `handlers.py:1555`) and snapshots every known address.
- `/shaxsiylar` lists marked chats plus suggestions (relationship words, informal tone, no
  business vocabulary), each with a `jr:p:<person_id>` button.
- Unmarking needs a confirm (`jr:y`, `jr:n`).
- `/guruhai <guruh>` toggles group replies, which then apply only to messages addressed to the
  owner.
- Hard exclusions (§5.4) are enforced in `policy` regardless of rules. Merge moves rules; purge
  keeps address-only rules.

**Owner strings.** "🔒 {ism} shaxsiy deb belgilandi. MIYA bu chatga hech qachon o'zi yozmaydi
(o'qish davom etadi)." · "🔓 {ism}ni shaxsiydan chiqaraymi? Shunda MIYA unga qoralama tayyorlay
oladi." Buttons "✅ Ha, chiqar" / "✖️ Yo'q" · "🔒 Shaxsiy chatlar ({n}):" · "💡 Oila yoki do'st bo'lishi
mumkin:" with "🔒 {ism}" buttons · "👥 «{guruh}» guruhida MIYA javob yozishi yoqildi (faqat sizga
murojaat qilinganda)." / "… o'chirildi."

**Tests.** `test_never_wins_over_everything`, `test_saved_messages_service_bots_never`,
`test_group_default_never`, `test_rule_survives_person_purge_by_address`,
`test_merge_moves_rules`, `test_unmark_needs_confirm`.

**Acceptance.** Exhaustive precedence tests. Owner check: `/shaxsiy` works by name and by GS code.

---

#### JV-13 — Red-line guard and stall templates (pure, corpus-tested)

**Priority** P0 · **Depends on** JV-03 · **Migration** none

**Goal.** Deterministic RL1 and RL2 checks on inbound requests and outbound drafts, plus stall
templates in the owner's register.

**Files.** `miya/jarvis/redlines.py`, `miya/jarvis/stalls.py`, `miya/services/amounts.py`,
`miya/services/records.py`, `tests/fixtures/redline_corpus.py`, `tests/test_redlines.py`.

**Behaviour.**
1. Unify the currency tables first: `amounts._CURRENCY`/`_SCALE` (`amounts.py:20-50`) and
   `records._CURRENCY_WORDS` (`records.py:880-889`) become one table that both import; add `сўм`,
   `минг`, `млрд`, `€/eur/евро`, `₽`, `₸/тенге`, `у.е.`, `бакс`, `лимон` and the suffix `$`
   (`300$`).
2. `classify_inbound(text) -> set[Signal]`: price, discount, payment_terms, deadline (the
   `loops.QUESTION_WORDS` subset `qachon`, `qachongacha`, `qancha`, `qanchadan`, `necha kunda`,
   plus a TIME hit with a question shape), promise/guarantee demands, payment_claim, identity,
   complaint/legal, other_people, injection tripwires (alert only).
3. `check_outbound(text, role, allowed_tokens) -> Verdict` implementing §5.2 step 2 with the
   GUARANTEE, TIME/DEADLINE, PROMISE and MONEY lexicons from the safety research, the surface and
   word views, and the stall-verb allowlist.
4. Stall templates by reason × script × honorific; each passes `check_outbound` in the `stall`
   role.
5. `check_shapes(text, *, approved_kb=(), stall_texts=None) -> ShapeVerdict`: the statement
   allowlist of §5.2 step 3 (courtesy set, questions, stalls, KB texts; glued statements, emoji
   and rhetorical questions handled as described there). `check_outbound` runs it for the `reply`
   and `stall` roles and exposes `shapes` and `shapes_ok` on the `Verdict`, and `shapes_ok`,
   `shapes` and the non-blocking flag `unapproved_statement` on the `GuardResult` the gate stores.
   Every stall template passes it. `outbox.create` never auto-approves without `shapes_ok`, and
   `gate.evaluate` redrafts an auto row without it (rule 13a).

**Owner strings.** Stalls (Latin):
- price: "Aka, narx bo'yicha hozir aniqlashtirib, o'zim sizga yozaman."
- deadline: "Aka, muddati bo'yicha aniqlab, o'zim xabar beraman."
- guarantee: "Aka, aniq bilib, keyin o'zim aytaman."
- payment claim: "Rahmat, aka. Tekshirib, o'zim yozaman."
- general: "Savolingizni oldim, aka. Aniqlashtirib, o'zim javob beraman."
- owner default: "Aka, hozir buni aniqlashtirib, o'zim tez orada yozvoraman."
- Cyrillic: "Ака, нарх бўйича ҳозир аниқлаштириб, ўзим ёзаман." Russian: "Здравствуйте! По цене
  уточню и сам вам напишу."
- Neutral-honorific forms drop «aka» and use «Siz».
- Flag labels: "va'da", "muddat", "kafolat", "narx", "chegirma", "to'lov sharti", "to'lov
  tasdig'i", "raqam", "havola".

**Tests.** `test_corpus_all_spec_examples_block` («Ertaga tayyor bo'ladi», «Soat 5 da tashlab
beraman», «100% hal qilamiz», «Ertaga 14:00 gacha hisoblab beraman», «Dushanba kuni yuboraman»,
«Обещаю, завтра отправлю», «Цена со скидкой, предоплата 50%»), `test_owner_stall_passes`,
`test_info_questions_pass` («Yuk qayerdan qayerga, necha kilo?»), `test_cyrillic_and_russian`,
`test_surface_view_catches_symbols`, `test_allowed_code_tokens_only`,
`test_unified_currency_table`, `test_shape_courtesy_is_a_closed_set`, `test_shape_questions`,
`test_shape_glued_statements_are_statements`, `test_shape_emoji`, `test_shape_stalls_and_kb`,
`test_every_stall_template_passes_the_shape_check`, `test_corpus_auto_and_never_auto` (every
bypass of the third verifier is blocked or never auto), and in `tests/test_outbox.py`
`test_an_unapproved_statement_is_a_draft_never_auto`,
`test_an_auto_row_without_shapes_ok_is_never_sent`,
`test_gate_refuses_auto_without_a_passing_shape_check`.

**Acceptance.** Every corpus line has its expected verdict; no network.

---

#### JV-14 — Owner style profile, few-shot examples and per-person tone

**Priority** P0 · **Depends on** JV-10, JV-13 · **Migration** `0026_owner_style`

**Goal.** Learn the owner's texting style from their last 300 genuine outgoing private messages.

**Files.** `alembic/versions/0026_owner_style.py`, `miya/db/models.py`, `miya/jarvis/style.py`,
`miya/jarvis/style_prompt.py`, `miya/tools/backfill.py`, `miya/userbot/main.py`,
`miya/services/chats.py`, `miya/worker/main.py`, `miya/bot/handlers.py`, `miya/bot/keyboards.py`, `miya/bot/replies.py`,
`miya/services/people.py`, `miya/services/purge.py`, `miya/config.py`, `.env.example`,
`README.md`, `miya/tools/doctor.py`, `tests/test_userbot.py`, `tests/test_style.py`.

**Behaviour.**
1. Sampler: genuine owner words only (JV-10 predicate), private chats, newest `STYLE_SAMPLE_SIZE`.
2. Scrubber (deny by default): an example is admitted only with zero red-line hits, no digits, no
   codes, URLs, handles, phone shapes, and no person name other than the counterparty's.
3. Traits are deterministic counts; `prompt_text` is REASON_MODEL prose refused if it holds a
   digit. Examples are context-reply pairs with embeddings, retrieved by similarity and intent.
   `owner_style_people` holds honorific, siz/sen and script per person.
4. History import: `/uslub yukla` stamps `chat_monitors.style_import_requested_at` on every
   monitored private chat (`chats.request_style_import`); the userbot's
   `approved_media_loop` gains a `fetch_style_imports` step next to `fetch_backfills`
   (`userbot/main.py:802-810`, wrapped in its own try/except like its siblings) that reads
   `chats.pending_style_imports` and calls `backfill.fetch_style_history` (added to
   `HISTORY_ALLOWED_IN`), stores archive interactions, and stamps `style_import_done_at`; a
   failing chat is retried a bounded number of sweeps (`style_import_attempts`).
5. A new profile starts as `draft`; the owner activates it. Weekly `style_refresh` skips when
   `input_hash` is unchanged. Below `STYLE_MIN_SAMPLES` no profile is built.

**Owner strings.** "🗣 Uslubingiz ({n} ta xabardan):\n• Salomlashish: {..}\n• Murojaat: {..}\n•
O'rtacha uzunlik: {k} so'z\n• Tez-tez: {iboralar}\n• Emoji: {kam|o'rtacha|ko'p}\n• Yozuv: lotin
{p}% · kirill {q}% · rus so'zlari {r}%\nMisollar: …" Buttons "✅ Faollashtirish" (`js:y:<v>`) /
"🔄 Qayta tuzish" (`js:r:<v>`). "📥 Uslub uchun eski yozishmalaringizni yuklayapman. Tayyor bo'lganda
yozaman." · "✅ Uslub tayyor: {n} ta xabar, {m} ta misol. /uslub"

**Tests.** `test_sampler_excludes_miya_rows`, `test_scrubber_drops_figures_names_links`,
`test_traits_deterministic`, `test_prompt_text_digit_free`, `test_single_active_profile`,
`test_person_tone_from_history`, `test_style_history_allowed_function_only`,
`test_style_import_request_done_once`, `test_style_import_failure_bounded_and_contained`.

**Acceptance.** The profile builds from fixtures. Owner check: `/uslub` reads like them.

**As built.** `chats.request_style_import` stamps every monitored private chat except the
Telegram service chat, Saved Messages and a chat with an active `never` rule (a personal chat
is never read for style). `style_refresh` runs every 10 minutes, only with `JARVIS_ENABLED`
(rule 20): it builds once a requested import is read and says «✅ Uslub tayyor» once, and
otherwise rebuilds when the newest profile is a week old and `input_hash` changed; `/uslub`
works without it and builds on demand. `owner_style_people` is keyed by the person, so it is not
in `merge_into._fk_columns`; the merge drops the source's row and recomputes the target's.
The owner's stall overrides are stored by `style.set_stall_override` and read by `outbox`
itself (`owner_stall_texts`, for `create`, `approve` and every claim); a caller still cannot hand
approved texts in. Usage is recorded as `jarvis_style` and the prose call is skipped over the
cost cap.

---

#### JV-15 — Business knowledge base for general answers

**Priority** P1 · **Depends on** JV-13 · **Migration** `0027_knowledge_base`

**Goal.** General questions are answered only from facts the owner approved.

**Files.** `alembic/versions/0027_knowledge_base.py`, `miya/db/models.py`, `miya/jarvis/kb.py`,
`miya/worker/main.py`, `miya/bot/handlers.py`, `miya/bot/keyboards.py`, `miya/bot/replies.py`,
`miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`, `tests/test_kb.py`.

**Behaviour.** `/bilim` lists; `/bilim qo'sh Savol | Javob` adds (the writer runs `check_outbound`
in the `kb` role and refuses price, discount, payment-term and deadline content); `/bilim o'chir
b12` retires; `/bilim taklif` lists suggestions. Retrieval is hybrid (vector + tsv) with
`KB_MATCH_THRESHOLD`, at most 3 entries. Weekly `kb_mining` clusters past client questions with
the owner's genuine answers; EXTRACT_MODEL writes a neutral digit-free Q/A stored as `suggested`.

The active entries also feed the statement allowlist (§5.2 step 3): the responder passes the
ids of the entries it retrieved to `outbox.create(..., kb_entry_ids=...)` (never the texts:
`create` loads the active entries itself and stores their ids in the guard JSON; it refuses
caller-supplied `approved_kb` texts until then), and an answer that
repeats an entry verbatim (the whole entry, or whole sentences of it) is an approved `kb` shape,
so a `general_question` may go out on its own once graduated. A paraphrase of an entry is a
free-form statement and stays a draft. JV-15 also fills `gate.Context.approved_kb` in
`outbox._context` with the active entries, so a body re-guarded at the claim is matched against
the same texts (until then it is matched against none and an auto row goes back to the card). A
retired entry is no longer passed, and the claim guards every auto row with a `kb` shape again,
so a row approved before the entry's retirement goes back to the card. An entry never loosens a red line: `check_outbound` still
reads it like any text.

**Owner strings.** "📚 Bilimlar bazasi ({n} ta faol):" · "✅ Qo'shildi: b{id} — {savol}" · "⛔
Qo'shilmadi: javobda {belgilar} bor. Bunday savollarga MIYA doim sizga yo'naltiradi." · "💡
Javoblaringizdan {n} ta bilim taklif qilindi: /bilim taklif" · buttons "✅ Qo'shish" / "✖️ Kerak
emas" · "🗑 b{id} o'chirildi."

**Tests.** `test_writer_refuses_red_lines`, `test_retrieval_threshold`,
`test_suggestion_needs_approval`, `test_retired_not_retrieved`, `test_kb_text_is_an_approved_shape`
(verbatim auto, paraphrase draft), `test_retired_entry_no_longer_approves`.

**Acceptance.** Green. Owner check: adding an office-hours fact works.

**As built.** `miya/jarvis/kb.py` (mypy strict). The writer checks a client's personal data
first (a phone or card number, an e-mail, a handle, a client code or waybill, a known person's
name other than the owner's), then the answer in the `kb` role; the question is checked for
personal data only, since it is never sent. Besides the plan's strings the owner sees «⛔
Qo'shilmadi: matnda {belgilar} bor. Mijozlarning shaxsiy ma'lumotlari bilimlar bazasiga
yozilmaydi.», a length refusal, `/bilim tuzat b12 Savol | Javob` (edit; the state stays) with
«✏️ b{id} yangilandi: {savol}», and «✖️ b{id} taklifi olib tashlandi.» for «Kerak emas»
(`kb:y:<id>` / `kb:n:<id>`, not in `_button_handlers`). A dismissed suggestion is retired and
never mined again. Retrieval: an entry matches when the cosine similarity of the embeddings or
the share of the question's words found (by prefix, through `search_tsv`) in the entry's
question reaches `KB_MATCH_THRESHOLD`; every read checks the entry again. `outbox.create` takes
`kb_entry_ids` (ints only, `bad_kb_entry_ids` otherwise), loads the answers of those still
active itself and keeps the ids in the guard JSON (`kb_entry_ids`, carried over on a re-guard);
caller texts stay refused. `outbox._context` passes the answers of every active entry, read once
per claim (`owner_kb_texts`, contained). `kb_mining` runs hourly with `JARVIS_ENABLED`: it embeds
new entries, and once a week (`reminder_log` kind `kb_mining`) clusters the owner's genuine
answers to questions asked in at least two chats (no question with a red-line or identity
signal, no private data on either side) and lets EXTRACT_MODEL write at most five digit-free
suggestions (`jarvis_kb` usage, skipped over the cost cap). Purging the owner's answer takes a
mined entry with it (`/unut` counts it); forgetting a person strikes their name from every entry
that names them, retired ones included, and retires it. The writer reads the answer in the `kb`
role, the outbox in the `reply` role, so an entry with a digit or a verb the reply role reads as
a promise (office hours «soat 9 dan 18 gacha», «Biz Xitoydan yuk olib kelamiz») is stored, but a
body quoting it is `blocked` by `create`, never sent or drafted; an entry never loosens a red
line. Mining groups clients' questions at its own `MINING_CLUSTER_THRESHOLD` (0.75), not at
`KB_MATCH_THRESHOLD`.

---

#### JV-16 — Responder pipeline in shadow mode

**Priority** P0 · **Depends on** JV-09, JV-11, JV-12, JV-13, JV-14, JV-15 · **Migration** none

**Goal.** Each eligible inbound DM gets a style-cloned draft or an escalation, stored as `shadow`
for calibration.

**Files.** `miya/jarvis/responder.py`, `envelope.py`, `intent.py`, `draft.py`, `guard.py`,
`costs.py`, `miya/userbot/main.py`, `miya/worker/main.py`, `miya/bot/handlers.py`,
`miya/bot/replies.py`, `miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`,
`tests/test_responder.py`.

**Behaviour.**
1. The userbot enqueues only live incoming private messages younger than
   `JARVIS_MAX_MESSAGE_AGE_SECONDS`, after `ingest_message` returns True; never catch-up, backfill
   or edits.
2. `jarvis_respond` (15 s) claims due chat states with SKIP LOCKED and runs the gate (enabled,
   eligible, fresh, not spam, owner has not replied, caps, AI turn limit, cost cap).
3. Deterministic signals first; a red-line hit opens an escalation and queues a stall row.
   Otherwise intent model, KB retrieval, a draft with `tools=[]` and strict JSON, then the guards
   (§5.2; the model guard can block, never approve). Rows are stored as `shadow`.
   The responder prompt says: in an auto-eligible intent (`greeting`, `info_gathering`,
   `general_question`) answer **only** with questions, courtesy from the closed set and the
   retrieved KB text verbatim; no other statement of any kind. The statement allowlist (§5.2
   step 3) enforces it in code: the ids of the KB entries retrieved for the draft are passed to
   `outbox.create`, which loads their texts and the owner's stall overrides itself, and a draft
   that strays is a draft card, never an autonomous send. The shadow metrics count
   `unapproved_statement` drafts per intent, so the owner sees how often the model strays.
4. Model failures → `needs_review`; nothing is sent. Usage operations `jarvis_intent`,
   `jarvis_draft`, `jarvis_guard`. Over the cost cap: escalate-only with templates, no model calls.
5. `/qoralamalar` lists shadow drafts.

**Owner strings.** "🧪 Kuzatuv qoralamalari:\n{i}. {ism}: «{mijoz}» → «{qoralama}» [{intent}]" ·
status line: "Kuzatuv: bugun {n} ta javob tayyorlanardi ({e} tasi sizga yo'naltirilardi)." · cost
stop: "💸 Jarvis bugungi xarajat chegarasiga yetdi — ertagacha faqat sizga yo'naltiradi."

**Tests.** `test_catchup_messages_never_enqueued`, `test_old_message_never_enqueued`,
`test_debounce_burst_one_draft`, `test_red_line_skips_draft_model`,
`test_guard_failure_becomes_stall`, `test_model_guard_cannot_override_det_block`,
`test_model_error_needs_review`, `test_cost_cap_stops_drafting`,
`test_responder_has_no_tools_and_no_private_data` (assert the exact prompt inputs),
`test_prompt_limits_auto_intents_to_approved_shapes`, `test_kb_texts_reach_the_allowlist`,
`test_injection_in_thread_never_sends`.

**Acceptance.** With fake model payloads, shadow rows and escalations are correct. Owner check:
`/qoralamalar` shows plausible drafts in their style.

**As built.** `miya/jarvis/responder.py`, `envelope.py`, `intent.py`, `draft.py`, `guard.py`
(mypy strict). The userbot's live `NewMessage` handler is `userbot.on_new_message`: it ingests,
and only when `ingest_message` returned True calls `enqueue_reply`, which imports the responder
lazily after the JARVIS_ENABLED check (the catch-up, backfills and `record_edit` call
`ingest_message` directly and never enqueue); the enqueue is one upsert, no model call.
`respond_after` is the latest message plus JARVIS_DEBOUNCE_SECONDS; the claim leases a chat
for three minutes instead of holding a row lock through the model calls (so the next enqueue
never waits), and the owner-first window (JARVIS_OWNER_FIRST_SECONDS) runs from the client's
first unanswered message: a chat claimed earlier is looked at again when it ends. Every Jarvis
model call goes through `costs.ask`: it refuses over the cost cap before sending anything,
sends no `tools` parameter at all (a request without tools, rather than an empty list), and
records the usage in a transaction of its own (falling back to the caller's), so a run that
fails later never hides a spend. The deterministic guard runs before the model guard through
`outbox.preview` (exactly `create`'s inputs, nothing stored), and the model verdict rides into
`outbox.create(model_verdict=...)`: anything but `pass` makes the stored verdict `model_block`,
`approve` refuses a model-blocked body, and a claim re-guard of the same body keeps the model
verdict. «Model failures → needs_review» is an escalation (reason `other`, history code
`needs_review` and the failing step) with no stall: `interactions.needs_review` stays the
extraction pipeline's (`/tekshir`, `/qayta`). An injection tripwire in the new messages, the
model guard's spam/scam/hijack reading of the thread, the AI turn limit and the per-chat cap
send the chat to the owner with no stall; a red-line signal, an escalating intent, the
writer's `needs_owner`, a guard block and the cost cap add the one stall (the owner's default
stall when the client asked nothing only the owner may answer). An open escalation makes a
new push a `repeat` entry in its history (real signals merged into `reasons`; a push with no
signal adds no `other`), never a second stall. It holds the chat only until the owner writes
there after it opened (then the responder resolves it as `owner_replied`, `auto:own`) or, still
`open`, for JARVIS_DRAFT_EXPIRY_HOURS (`expired`, `auto:expiry`). A `general_question` is
approved on its own only when its text quotes an active KB entry (a `kb` shape), in
`outbox.create` and at the claim (`gate.auto_shapes_ok`). A message the shield flagged reads
`[hidden by the shield]` in every thread a model sees. Escalations record the channel's mode in their creation entry (`mode`), so the cards
(JV-18) can tell observation-only ones apart; there is no summary prose yet (`summary` NULL).
The writer only sees KB answers that pass the reply role. The envelope masks phone and card
numbers and money figures (codes and waybills stay) besides removing format characters,
hidden HTML, quoted mail chains and the delimiters. `/jarvis` adds the observation line and,
when drafts strayed, «Erkin gapli qoralamalar (o'zi yuborilmasdi): …» per intent; the cost
stop is told once a day (`reminder_log` kind `jarvis_cost_stop`).

---

#### JV-17 — Owner-first window, takeover and draft invalidation

**Priority** P0 · **Depends on** JV-16 · **Migration** none

**Goal.** MIYA never talks over the owner.

**Files.** `miya/jarvis/chat_state.py`, `miya/userbot/main.py`, `miya/jarvis/responder.py`,
`miya/jarvis/outbox.py`, `miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`,
`tests/test_owner_first.py`.

**Behaviour.** New handlers next to `main.py:1007-1009`: `events.MessageRead(inbox=True)` sets
`owner_seen_at`; `events.Raw(UpdateDraftMessage)` sets `owner_composing_at`; `MessageDeleted`
cancels drafts tied to the deleted message. A genuine owner outgoing message sets
`owner_replied_at`, cancels pending drafts, resolves the open escalation as `auto:own` and sets
`ai_paused_until = now + JARVIS_TAKEOVER_MINUTES`. A client edit (`record_edit`) expires the draft
and re-queues the candidate. Both the responder and `claim_ready` check these signals. Signals
from AI rows are ignored.

**Owner strings.** "🙋 Siz o'zingiz javob berdingiz — qoralama bekor qilindi." · "✏️ Mijoz xabarini
tahrirladi — qoralama yangilanadi." · "✅ {ism}ga o'zingiz javob berdingiz — eskalatsiya yopildi."

**Tests.** `test_owner_reply_cancels_and_pauses`, `test_composing_blocks_claim`,
`test_edit_expires_draft`, `test_delete_cancels`, `test_signals_from_ai_rows_ignored`.

**Acceptance.** Every signal covered with fake events.

**As built.** `miya/jarvis/chat_state.py` (mypy strict) turns the signals into state; the
userbot's handlers (`on_message_read`, `on_draft_update`, `on_message_deleted`,
`on_message_edited`, `note_owner_message`, registered by `add_signal_handlers` next to the
NewMessage/MessageEdited handlers, now at `main.py:1788-1791`, only with JARVIS_ENABLED) only
write MIYA's own database, import `chat_state` lazily and are contained. Telethon 1.44 has
`events.MessageRead(inbox=True)`, `events.MessageDeleted` and `events.Raw(UpdateDraftMessage)`; a
read counts only when it covers the client's latest stored message, a read while MIYA sends in the
chat (a `sending` row, or `last_ai_sent_at` within a minute) is the sender's own, and a cleared
draft (`DraftMessageEmpty`) says nothing (the gate's 60-second `COMPOSING_HOLD` ages typing out).
The owner's own typing (`UpdateUserTyping`) is not delivered to their other sessions, so the cloud
draft is the typing signal. "Genuine owner message" is `authorship.is_owner_typed` /
`owner_typed_clause`: `is_owner_words` minus every message MIYA sent (an owner command's echo
included), and an untagged echo whose external id or exact text matches a row of the chat on the
wire around it (Q11: `sending`/`unknown` with a lease reaching two minutes before the message, or
`sent` within two minutes) is no takeover either; the signal and the claim share that one
predicate (`authorship.owner_takeover_clause`). An outgoing sticker or GIF, which the media policy
never stores, takes the chat over too (`note_owner_sticker`; MIYA never sends media). The takeover
runs for live messages and for the catch-up (`backfill.catch_up_chat`), from the message's own
time (clamped to now): `owner_replied_at` and `ai_paused_until` only move forward, the AI turn
count starts again, `draft`/`approved` AI rows whose client message came first are cancelled
(`auto:own`, `owner_replied`; a tap after the owner's message is kept), and the open escalation
whose client message came first (even one written after the owner's message, by a responder that
could not see it yet) is resolved at once (the responder keeps resolving one for a message stored
without a signal, through the same `chat_state.resolve_escalation`). `shadow` rows stay: they are
never sent and are the observation record. The claim also reads the owner's stored typed messages,
so a signal that failed never lets a row through. A client edit expires every pending AI row that
could have read the message (its trigger is the message or a later one within
JARVIS_MAX_MESSAGE_AGE_SECONDS), cancels an `open` escalation whose stall was among them, and
re-queues a still-fresh chat (not when the edit swapped the message's media); `record_edit` runs
the JV-09 shield on the new words first (a verdict already there stays), so an edit into a scam is
never drafted. The responder then treats those rows as unanswered, and `outbox.create` gives the
fresh proposal a revision of the idempotency key, so the same text is a new row. A deletion marks
`meta.deleted_at` (private chats and small groups have no chat id in the update: their message ids
are account-wide, channels and supergroups are matched by chat) and cancels the rows answering it
(`trigger_deleted`); a draft for a later message of the same burst expires (`burst_deleted`) and
the chat is queued again; the burst never includes a deleted message, and a read covers the
client's latest message that is not deleted. The three owner strings are in `replies.py`;
`jarvis_outbox` tells them once, silently, as a reply to the card or alert, only for a draft that
had a card (`card_tg_message_id`) or an escalation that was alerted (`alerted_at`), so they start
with JV-18's cards; the edit notice waits for the fresh draft, and is never told when none is
coming.

---

#### JV-18 — Draft and escalation cards, the three buttons, a real 1-hour snooze

**Priority** P0 · **Depends on** JV-17 · **Migration** none

**Goal.** The owner sees drafts and escalations and acts on them. Until a sender exists, cards run
in copy mode.

**Files.** `miya/jarvis/cards.py`, `miya/jarvis/responder.py`, `miya/bot/keyboards.py`,
`miya/bot/handlers.py`,
`miya/bot/replies.py`, `miya/worker/main.py`, `miya/config.py`, `.env.example`, `README.md`,
`miya/tools/doctor.py`, `tests/test_jarvis_cards.py`.

**Behaviour.**
1. `jarvis_cards` (1 min) delivers new drafts and escalations through `deliver()`
   (`worker/main.py:100-133`); silent in quiet hours; expires drafts after
   `JARVIS_DRAFT_EXPIRY_HOURS`; re-alerts snoozed escalations whose `snoozed_until` passed (DB rows,
   not APScheduler DateTriggers, because the jobstore is in memory); after `JARVIS_SNOOZE_MAX`
   snoozes the escalation moves to the morning brief; over `JARVIS_ESCALATION_DAILY_CAP` alerts
   fold into one message.
1a. The spec's «IMMEDIATELY alert»: in `draft` and `auto` mode the escalation alert is sent by
   `cards.alert_now` inside the same `jarvis_respond` run that opened the escalation, as soon as
   the debounce ends; it does not wait for `JARVIS_OWNER_FIRST_SECONDS` (only the client-facing
   stall waits). `jarvis_cards` only retries rows whose `alerted_at` is still NULL. A failed
   delivery is logged and retried, never lost.
2. Card money facts are rendered from SQL (`queries.person_summary`, claims views,
   `formatting.money`); the summary prose is digit-free; client text is escaped and fenced.
3. Callbacks `jv:y|e|o|n:<id>` (draft: send, edit, I'll write, cancel) and `je:y|o|s:<id>`
   (escalation: approve and send, I'll write, remind in 1 hour). Each locks the row and
   re-validates. Never in `_button_handlers`.
4. Replying to a card (intercepted before `_batch_reply`) replaces the body with the owner's text
   (`authored_by='owner'`, flagged by the guard, not blocked) and re-renders the card. A voice
   reply is transcribed and shown before approval.
5. Send buttons appear only when the channel adapter is ready; otherwise the card shows the text
   with a copy hint.

**Owner strings.**
- Draft card: "✍️ Qoralama j{id} · {ism} · {kanal}\n💬 Mijoz ({vaqt}): «{xabar}»\n🤖 Javob:
  «{qoralama}»"
- Copy footer: "📋 Yuborish hali ulanmagan — matnni nusxa olib o'zingiz yuboring."
- Draft buttons: "✅ Yuborish", "✏️ Tahrirlash", "🙋 O'zim yozaman", "✖️ Bekor".
- Edit hint: "✏️ Yangi matnni shu qoralamaga reply qilib yozing yoki ovozli xabar yuboring."
- Escalation card: "⚠️ {ism} {sabab} · {kanal}\n👤 {ism}{ · kod}\n💬 «{xabar}» ({vaqt})\n📝
  {qisqacha}\n💰 Hisob: {sql faktlar|Ochiq qarz yo'q}\n🤖 {Mijozga yuborildi|Yuborishga tayyor}:
  «{stall}»\nO'z javobingizni shu xabarga reply qilib yozsangiz, «Tasdiqlash va yuborish» aynan
  shuni yuboradi."
- Reasons: "narx so'rayapti", "chegirma so'rayapti", "to'lov shartini so'rayapti", "muddat
  so'rayapti", "va'da yoki kafolat so'rayapti", "to'lov qilganini aytyapti", "bot emasmi deb
  so'rayapti", "shikoyat qilyapti", "boshqa savol".
- Escalation buttons: "✅ Tasdiqlash va yuborish", "✍️ O'zim javob yozaman", "⏰ 1 soatdan keyin
  eslat".
- Results: "⏳ Navbatga qo'yildi — {kanal} orqali yuboriladi" · "🙋 Mayli, o'zingiz yozasiz. {ism}
  chatida MIYA {soat} soat jim turadi." · "⏰ Soat {vaqt} da yana eslataman."
- Re-alert: "⏰ Eslatma ({n}-marta): {ism} hali javob kutyapti".
- Stale: "Bu qoralama eskirdi." / "Allaqachon javob berilgan."
- Owner text warning: "⚠️ Matningizda narx yoki muddat bor — bu sizning so'zingiz sifatida
  yuboriladi."
- Cap: "Bugungi qoralama limiti tugadi ({cap}). Qolganlari ertalabki xulosada."

**Tests.** `test_payloads_le_64_bytes`, `test_double_tap_idempotent`,
`test_snooze_realerts_after_60_min` (time faked), `test_snooze_max_moves_to_brief`,
`test_reply_to_card_edits_body`, `test_hammasi_ha_cannot_approve`, `test_copy_mode_hides_send`,
`test_card_figures_from_sql_only`, `test_escalations_do_not_touch_question_budget`,
`test_escalation_alert_sent_in_same_run`, `test_alert_not_delayed_by_owner_first_window`,
`test_failed_alert_retried_by_sweeper`.

**Acceptance.** Cards work end to end in copy mode. Owner check: one real draft card in the first
week.

**As built.** `miya/jarvis/cards.py` (mypy strict) builds plain `Card` values from SQL and records
what was shown; the bot renders them (`replies.jarvis_card`, `keyboards.jarvis_card`) through a
`Presenter` (`worker.JarvisPresenter`, over `deliver()`). 1a: in `draft` and `auto` mode a burst
with a deterministic escalating signal or an injection tripwire (`responder.escalates_at_once`,
no model call) is escalated as soon as the debounce ends; its stall is created with `not_before` at
the owner-first window's end (the owner's own message before then cancels it), the creation
entry records `stall_after`, and `responder.run(alert=…)` hands each chat's outcome to
`cards.alert_now`/`cards.card_now` right after that chat's transaction commits. Shadow mode keeps
the window for everything, and no early alert interrupts an owner who is typing in the chat. Only
escalations whose creation entry says `draft` or `auto` are ever alerted. A tap is honoured only
from the card the row is shown on now (`card_tg_message_id`, `alert_tg_message_id`); every card
and alert shown is a history entry (`card`, `alert` with `card`/`fold`/`realert`), so a reply to
an older one still finds its row. A reply to a card is intercepted first in `on_text` (ahead of the
code lookup and `_batch_reply`) and after transcription in `on_voice` (the note is stored
`processed` with `meta.kind='jarvis_reply'`); the card is shown again as a new message right under
the reply and the old card loses its buttons; if that card does not reach the owner the new words
are rolled back, so a tap always sends the text a card shows. A draft takes the words in place
(`outbox.owner_text`: `authored_by='owner'`, `ai_body` kept, the owner-role verdict bound to the
new body); an escalation gets its own reply row (`origin='escalation'`, the waiting stall
cancelled with `owner_text`), and `outbox.create` makes a fresh revision when the owner writes the
same words again after such a row was cancelled. Owner-text cards show the red-line flag warning,
JV-20's «🔒 Bu shaxsiy chat …» and the JV-09 scam or hijack warning when they apply, and
`cards.record_shown` writes `warned_personal`/`warned_safety` on the row the gate checks. «🙋»
cancels the waiting rows (`owner_will_write`), sets `owner_replying` (which the responder lets
expire after JARVIS_DRAFT_EXPIRY_HOURS) and pauses the AI (`chat_state.pause_ai`). The snooze that
reaches JARVIS_SNOOZE_MAX sends the escalation to the morning brief (`to_brief`, «Soat
{MORNING_BRIEF_TIME} da yana eslataman»), and it expires (`briefed`) once a brief was delivered
after that. The brief's lines use JV-22's wording («⚠️ Ochiq eskalatsiyalar», «📨 Javobsiz
chatlar» for the drafts over JARVIS_DRAFT_DAILY_CAP); JV-22 widens them. Strings the WP does not
give: the fold header «⚠️ Bugungi eskalatsiya limiti tugadi ({cap}). Qolganlari:» (then the
card header line per escalation, one message a day edited in place) and «✖️ Bekor qilindi» under
a cancelled card, and «✂️ Matn juda uzun, kartaga sig'maydi — qisqaroq qilib qayta yozing.»
for an owner reply that would make the card too long. The summary (`escalations.summary`, «📝»)
is one EXTRACT_MODEL sentence (`cards.write_summary`, usage operation `jarvis_summary`, inside
the cost cap) read from the chat's envelope right after the alert went out, never before it;
it is kept only when `recaps.clean_prose` finds no figure, stored on the escalation (every
later card shows it) and edited into the alert on screen when nothing happened to it since; a
refused or failed summary costs the alert nothing. The card also quotes the client's last
three messages of the escalation. «💰 Hisob» shows the ledger's open balances
(`queries.open_debts`); a counterparty's pending claims are not facts and are left out. A card
is never clipped: the quoted client lines go oldest first while it would not fit one message,
the owner's text is at most `cards.OWNER_TEXT_MAX` (2000) characters, and a card that would
still not fit is refused whole (`too_long`, nothing recorded as shown). A card without a
resolved person names the chat by its own title. ✅ on an escalation whose only waiting row is
the stall approves the stall and keeps the escalation open (`stall_approved`): the client's
question is still the owner's. An escalation opened in shadow mode and never alerted closes
(`mode_changed`) as soon as its chat is read in `draft` or `auto` mode, so a new push opens one
that is alerted. The brief's unanswered chats are the capped drafts of the last day still
waiting or expired unanswered, in chats the owner has not written in since. Drafts still expire
in `jarvis_outbox` (`outbox.expire_due`); `jarvis_cards` does not expire them a second time.

---

### Phase 3 — speak with approval on Telegram (week 2)

#### JV-19 — Telegram sender: the narrow exception to the read-only rule, with human pacing

**Priority** P0 · **Depends on** JV-02, JV-18 · **Migration** none

**Goal.** Approved Telegram rows are sent from the owner's account exactly once, inside the one
userbot process.

**Files.** `miya/userbot/sender.py` (new), `miya/userbot/main.py`, `miya/userbot/__init__.py`,
`miya/tools/backfill.py`, `miya/jarvis/outbox.py`, `tests/test_userbot.py`,
`tests/test_userbot_sender.py`, `README.md`, `docs/handoff-plan.md`, `docker-compose.yml`,
`miya/config.py`, `.env.example`, `miya/tools/doctor.py`.

**Behaviour.**
1. `TelegramClient(...)` (`main.py:959-963`) gets `flood_sleep_threshold=USERBOT_FLOOD_SLEEP_SECONDS`
   so a send never blocks silently. The startup `sync_from_client` (`main.py:991`) and
   `iter_dialogs` are wrapped in FloodWait handling so a long wait no longer exits `run()`.
2. The sender is its own asyncio task next to `approved_media_loop` (started at `main.py:1010`,
   cancelled in the `finally`), only when `TELEGRAM_SEND_ENABLED`. Each row is handled inside
   its own try/except (rule 25): an unexpected error fails that row with a code and the loop
   goes on; if the task itself dies, a done-callback logs it and restarts it with
   `with_backoff`, and the reader and `approved_media_loop` never stop. Every 2 s it calls
   `claim_ready('telegram', …)` and for each row:
   1. `get_input_entity(tg_chat_id)`, retrying once after a dialog sync; never imports contacts.
   2. Pre-delay from the pacing plan (uniform between the two delay settings).
   3. `send_read_acknowledge(peer, max_id=trigger)` — only here, only when actually replying.
   4. `client.action(peer, 'typing')` for the planned time.
   5. `send_message(peer, body, reply_to=…, parse_mode=None, link_preview=False)` with a
      deterministic `random_id` derived from `idempotency_key`; `RandomIdDuplicateError` counts as
      already sent.
   6. `outbox.complete` writes the tagged echo and advances `chat_monitors.last_seen_message_id`;
      an `IntegrityError` on `ux_interactions_tg_message` tags the existing row instead. (Verify on
      Telethon 1.44 whether self-sent messages reach `NewMessage`; either way the echo is tagged.)
3. Errors: `FloodWaitError`/`SlowModeWaitError` re-queue at `now + seconds` (no sleeping inside a
   claim); `PeerFloodError`, a flood wait above the threshold, `AuthKeyDuplicated`,
   `SessionRevoked`, `UserDeactivatedBan` trip the Telegram breaker and alert — reading continues;
   blocked, deactivated, write-forbidden, paid-message errors fail the row permanently; unknown
   outcomes go to `reconcile` (added to `HISTORY_ALLOWED_IN`). `allow_paid_stars` is never set.
4. The AST guard is rescoped, not deleted: recursive scan of `miya/userbot/**` plus every module
   that imports telethon; innermost-function attribution in `_calls_by_function`; `WRITE_CALLS`
   extended (`action`, `mark_read`, `reply`, `respond`, `edit`, `delete`, `forward_to`,
   `pin_message`, `send_reaction`, `click`, `upload_file`) plus banned raw request names
   (`SendMessageRequest`, `ReadHistoryRequest`, `SetTypingRequest`, `ImportContactsRequest`, …);
   writes allowed only in named functions of `sender.py`; `HISTORY_ALLOWED_IN` gains the existing
   backfill functions (`catch_up_chat`, `backfill_chat`, `archive_chat`), `fetch_style_history`
   and `reconcile`; a structural test that those send functions take rows only from
   `claim_ready`.
5. Sender status merges into the single userbot beat detail (detail is replaced whole, so one
   writer). Read-only wording updated in the docstring (`main.py:1-17,989`), `__init__.py:1`,
   `docker-compose.yml:136`, README (`26, 394-403, 601-603, 717-718`) and
   `docs/handoff-plan.md:32,47`; README egress gains "Telegram sending as the owner".

**Owner strings.** "⛔ Telegram yuborishni cheklab qo'ydi. MIYA Telegramdan yozishni to'xtatdi;
o'qish davom etadi. @SpamBot orqali holatni tekshiring. Qayta yoqish: /jarvis yoq telegram" · "⏳
Telegram {daqiqa} daqiqa kutishni so'radi — xabarlar keyinroq yuboriladi." · "❓ j{id}
yuborilganmi — aniq emas. Chatni tekshiring; MIYA qayta yubormaydi." · "✖️ {ism}ga yuborib
bo'lmadi: {sabab}" with sabab "u sizni bloklagan", "akkaunt o'chirilgan", "bu chatga yozish
taqiqlangan", "faqat pullik xabar qabul qiladi" · "✅ Yuborildi ({vaqt})".

**Tests.** `test_write_calls_only_in_sender_functions` (AST), `test_sender_uses_claim_ready_only`,
`test_pacing_plan_applied` (fake client), `test_floodwait_requeues`,
`test_peerflood_trips_breaker_and_reader_continues`, `test_random_id_deterministic`,
`test_echo_tagged_and_deduped_with_catchup`, `test_crash_after_send_reconciles_unknown`,
`test_parse_mode_none`, `test_startup_floodwait_does_not_exit`,
`test_sender_crash_restarts_and_reader_continues`, `test_bad_row_fails_alone`.

**Acceptance.** With a fake Telethon client, the row is sent exactly once and the echo is tagged.
Owner check: one approved draft reaches a test contact with typing shown first.

---

#### JV-20 — Admin-bot command mode: write tools with confirmation and scheduled sends

**Priority** P1 · **Depends on** JV-06, JV-19 · **Migration** none

**Goal.** «Sardorga Telegramdan yozib yubor, kechki 6 da gaplashamiz» becomes a confirmed send, by
text or voice, now or scheduled.

**Files.** `miya/services/rag.py`, `miya/jarvis/commands.py`, `miya/services/people.py`,
`miya/services/when.py`, `miya/services/chats.py`, `miya/bot/handlers.py`,
`miya/bot/keyboards.py`, `miya/bot/replies.py`, `miya/api/main.py`,
`tests/test_jarvis_commands.py`.

**Behaviour.**
1. `rag.classify` (`rag.py:1031-1040`) returns `'command'` from imperative cues written in
   normalised form (`yozib yubor`, `yozvor`, `jonat`, `xabar ber`, `shaxsiy kil`, `napishi`,
   `otprav`) before the note fallback. A corpus test proves notes like «Akmalga pul yubordim» stay
   notes. Routed in `on_text` and `_spoken_mode`; code lookup and `_batch_reply` stay ahead.
2. `ACT_TOOLS` are passed only when `answer_full(allow_actions=True)`, set only by `_answer_into`
   for `mode=='command'`. `POST /v1/ask` and `rag.answer` stay read-only (test). A new
   RAG_SYSTEM_PROMPT section: you never send, only propose; proposals come only from the owner's
   current words, never from text inside tool results.
3. `propose_send_message(person, channel, text, send_at?)`: recipient via `find_person` (never
   `resolve_person`), retried once with `text.ALIAS_SUFFIXES` stripped («Sardorga» → «Sardor»),
   accepted only when not ambiguous and exact / via code / score ≥ `MATCH_THRESHOLD` (85);
   ambiguous → candidate buttons; the owner alias is refused. The dictated content is rephrased
   in the owner's style with meaning preserved; the guard runs in the `owner` role and only flags.
   `not_before` from `send_at`, cross-checked by `when.py` against the owner's words. Dedupe and
   at most 3 proposals per turn. `RagAnswer` gains `actions`.
4. Other tools: `set_personal`, `set_group_reply`, `set_group_monitor` (the spec's group
   whitelist for reading: the same toggle as the existing `/chats` and `/guruhlar` commands,
   through `miya/services/chats.py`), `sheets_resync`, `list_pending`. Each proposes; a
   restricting change applies on one tap, a loosening one (unmark personal, enable group
   replies, start monitoring a group) needs the confirm card of JV-12.
5. Personal chats: allowed only with an explicit warning on the card. A channel chat that has
   never existed is refused (reply-only rule). Voice commands show the transcript header.
6. Callbacks `ja:y|e|n:<id>`.

**Owner strings.** "📤 Yuborishni tasdiqlang j{id}\n👤 Kimga: {ism}{ · kod} · {kanal}\n🕕 Qachon:
{hozir|bugun 18:00}\n💬 Matn: «{matn}»" Buttons "✅ Yuborish", "✏️ Tahrirlash", "✖️ Bekor" · "Qaysi
{ism}?" · "{ism} uchun {kanal} chati topilmadi." · "🔒 Bu shaxsiy chat — matn sizning nomingizdan
aynan shunday ketadi." · "🕕 {vaqt} da yuboriladi." · "Hech narsa navbatga qo'yilmadi — qayta
urinib ko'ring."

**Tests.** `test_command_cue_routing_corpus`, `test_api_ask_has_no_act_tools`,
`test_recipient_suffix_strip_sardorga`, `test_ambiguous_offers_candidates`,
`test_never_uses_resolve_person`, `test_scheduled_send_not_before`,
`test_injected_tool_text_cannot_choose_recipient`, `test_new_chat_refused`,
`test_whitelist_tools_loosening_needs_confirm`, `test_owner_command_row_goes_through_gate`.

**Acceptance.** Green. Owner check: a voice command produces a correct card and the message
arrives at 18:00.

**As built.** `miya/jarvis/commands.py` (mypy strict) holds `ACT_TOOLS`, which `rag.answer_full`
imports only in command mode (`rag.ACT_TOOL_NAMES` mirrors the names, so a model naming one
outside command mode is told `unknown tool`); command mode is read before the question check
(«Sardorga yozib yubor: ertaga kelasanmi?» is the owner's words to send), only with
JARVIS_ENABLED (otherwise the words are read as before), never for a forwarded message (a
client's words), and skips the evidence prefetch. The cues cover the settings tools too
(«shaxsiydan chiqar», «guruhini o'qi/o'qima», «guruhida javob yoz/yozma», «jadvalni
yangila», «kutayotgan xabarlarni ko'rsat»). Its model calls are recorded as `jarvis_command`;
over JARVIS_DAILY_COST_CAP_USD (or with the spend unreadable) a command is answered read-only
as a question (`rag`, no act tools) with «💸 Jarvis bugungi xarajat chegarasiga yetdi — hech
narsa navbatga qo'yilmadi, ertaga qayta urinib ko'ring.». A command that proposed nothing (a
`list_pending` read alone aside) is the owner's note after all («Ertaga konteynerni
jo'nat»): its row loses `kind: question` and is extracted, and the receipt follows the
«Hech narsa navbatga qo'yilmadi» answer. The `person` a tool names must be words of the
owner's command text (case endings aside) before `find_person` runs, and one person is
accepted only when the reading as written and the one without a case ending agree («Malika»
next to «Malik» is asked back); «Qaysi {ism}?» shows the name without its ending. The time is
read from the owner's words without the recipient's name («Jumaga» is not Friday). Every money
figure in the text must be one the owner said (`figure_not_in_owner_words`). The channel is
the one the owner's words name, else Telegram («pochta» and «mail» alone name none); with two
named, the model picks one of them. The same send or «which one?» twice in a turn is the first
answer, before the three-proposal limit; the same text at another time is refused as
`already_proposed` with the row's own time. `outbox.create` refuses an `owner_command` row without
`command_interaction_id` or not authored by the owner. A card that does not reach the owner
cancels its row. Besides `ja:y|e|n:<outbox id>`, a «Qaysi …?» button is
`ja:c:<command message id>:<n>:<person id>` and a setting card's buttons are
`ja:s|x:<command message id>:<n>`; the proposal they act on is stored on the command message
(`meta.jarvis_proposals`), never read from the payload. Unmarking personal and switching group
replies on show the JV-12 cards themselves (`jr:y`, `jr:g`); the other settings (mark
personal, group replies off, reading a group on or off, `sheets_resync`) have cards whose
wording the plan did not give («🔒 {ism}ni shaxsiy deb belgilaymi? …», «👁 «{guruh}»
guruhini o'qishni boshlaymi? …», «🙈 … to'xtataymi?», «👥 … o'chiraymi?», «📊 Google Sheets
jadvalini bazadan qaytadan to'liq yozaymi?»). `chats.set_monitor` sets the `/chats` toggle to a
value under a row lock, `chats.find_titled` is the chat-title lookup `/shaxsiy` and `/guruhai`
used, and `sheets_sync.request_resync` puts every written row back to pending. The command
card names the chat by its own title too when it differs from the person's name.

---

#### JV-21 — Jarvis health, /holat lines and breaker alerts

**Priority** P1 · **Depends on** JV-19 · **Migration** none

**Goal.** The owner sees whether Jarvis works; a limit or stuck queue is told at once.

**Files.** `miya/services/health.py`, `miya/bot/replies.py`, `miya/worker/main.py`,
`tests/test_health.py`.

**Behaviour.** Optional `Status` fields from heartbeat detail and SQL (`COMPONENTS` is not
extended; `health.py:58` is pinned). New `PROBLEM_KEYS`, each gated on its feature:
`outbox_stuck`, `telegram_send_limited`, `jarvis_cost_cap`, each with a «tiklandi» recovery and a
test case. Breaker trips are sent immediately by `jarvis_cards` (not held by the 6-hour alert
cadence); health only records them. `/holat` lines return None when Jarvis is off.

**Owner strings.** "🤖 Jarvis: {rejim} · bugun {y}/{cap} yuborildi · navbatda {q}" · "📤 Telegram
yuborish: ✅ ishlayapti" / "⛔ cheklangan ({gacha})" · "📤 {n} ta xabar {daqiqa} daqiqadan beri
yuborilmay turibdi. Userbot ishlayotganini tekshiring." · "✅ Xabar yuborish tiklandi" · "✅ Jarvis
xarajat chegarasi tiklandi".

**Tests.** A case per new key in the `set(cases)==set(PROBLEM_KEYS)` test,
`test_disabled_feature_no_problem`, `test_breaker_alert_immediate`.

**Acceptance.** Health suite green; an install without Jarvis shows no new lines.

**As built.** `miya/jarvis/status.health_facts` (mypy strict) counts Jarvis's share in SQL and
`health.gather` imports it only with JARVIS_ENABLED, in a savepoint: a failed read is the one line
«⚠️ Jarvis — holatini o'qib bo'lmadi (make bot logini ko'ring)» (the bot gathers `/holat` and logs
the failure), never a failed status, and while the read fails no Jarvis key is announced as
recovered (`health.unjudged`). The values are plain (`health.JarvisHealth`, `health.ChannelSend`),
so `problems()` and `replies.py` never import miya.jarvis. `outbox_stuck` counts approved rows due
for 10 minutes (`STUCK_AFTER`; the due moment is the latest of the approval, `not_before` and the
gate's `next_attempt_at`) on a channel whose switch is on with no pause or breaker in force, and the
Telegram sender's own flood-wait pause (`sender_paused_until` on the userbot beat) counts as waiting
until 10 minutes past its end; claimed rows whose lease ran out 10 minutes ago without a reconcile
count too. The overdue rows of a channel not looked at (switched off, paused, limited, or the sender
waiting out a flood wait) are counted as held (`backlog_held`): they are not a stuck queue, but
while any are held `outbox_stuck` does not recover, so «✅ Xabar yuborish tiklandi» is said only once
the rows left or were cancelled. The sweep tells `outbox_stuck` in quiet hours too, silently
(`health.QUIET_SILENT`): the owner's approved sends go out at night, so a stuck queue is not held
until morning; its recovery waits for morning like every other. The breaker trip is not sent by
`jarvis_cards` but by `jarvis_send_notices` in the `jarvis_outbox` job, where JV-19 put it, and the
JV-16 cost-stop notice by `jarvis_respond`: each records its delivery as the ledger's `alert:` row
(`health.TOLD_FIRST_ELSEWHERE`), so the 5-minute sweep never says them first, repeats them word for
word after ALERT_REPEAT_HOURS and says «tiklandi» once. A Jarvis key never recovers while its switch
is off. The plan's «✅ Xabar yuborish tiklandi» is `outbox_stuck`'s recovery; `telegram_send_limited`
says «✅ Telegram yuborish tiklandi». In «🤖 Jarvis: … bugun {y}/{cap} yuborildi» `{y}/{cap}` is the
pair `/jarvis` shows as its limit: the mode's capped count today against its daily cap (auto sends
against JARVIS_AUTO_DAILY_CAP in `auto`, draft cards against JARVIS_DRAFT_DAILY_CAP otherwise),
`{q}` the approved rows waiting. Beyond the plan's lines `/holat` shows today's drafts, escalations
and spend against the cap («🧾 Jarvis bugun: …»), the Telegram send line's other states (off, paused,
waiting out a flood wait, the sender down), another channel's line only while its breaker is in
force, rows stuck in a claim, sends in doubt of the last 24 hours and the responder job's last run.

---

#### JV-22 — Jarvis sections in the 09:00 and 20:00 reports

**Priority** P1 · **Depends on** JV-07, JV-08, JV-18 · **Migration** none

**Goal.** The briefs report Jarvis activity, open escalations, waiting drafts and unanswered chats.

**Files.** `miya/services/brief.py`, `miya/services/recaps.py`, `miya/services/sheets_sync.py`,
`miya/bot/replies.py`, `tests/test_brief_jarvis_lines.py`.

**Behaviour.** All counts from SQL over `outbox_messages`, `escalations` and loops. Client_FollowUps
gains the open escalation reason and its age.

**Owner strings.** "🤖 Jarvis (kecha): {y} ta javob yuborildi ({a} tasi avtomatik, {t} tasi siz
tasdiqlagan), {e} ta eskalatsiya ({o} tasi ochiq)" · "✍️ Kutayotgan qoralamalar: {n} —
/qoralamalar" · "📨 Javobsiz chatlar: {n} — {ismlar}" · "⚠️ Ochiq eskalatsiyalar: {ism} ({sabab},
{qachondan})" · evening: "🤖 Bugun Jarvis: …".

**Tests.** `test_counts_from_sql`, `test_snoozed_over_max_in_brief`,
`test_sheets_followups_escalation_column`.

**Acceptance.** Green; lines appear on real days.

**As built.** `miya/jarvis/status.day_report(start, end)` (mypy strict) counts a report's window
in SQL: replies sent (`state='sent'`, `sent_at` in the window; `{a}` approved `auto:policy`,
`{t}` the rest, the owner's own escalation replies among them), the escalations the owner was
shown (created in `draft` or `auto` mode, `cards.shown_escalation`) and of those how many are
open now, the observation-mode escalations apart, the sends the owner commanded, the sends in
doubt (`unknown`, by their claim moment), the spend (`costs.spend_between` over the Jarvis
operations) and, per intent, the drafts and observation drafts made (`caps.became`), how many
the owner approved with the AI's words, how many they rewrote (the row turned the owner's,
`authored_by`; every AI row keeps `ai_body` from the start) and how many strayed (`unapproved_statement`, or since JV-23 `free_question`; a rewritten draft
is judged by the owner's words). The brief
gathers yesterday's, the recap today's (`recaps.jarvis_day`), each only with JARVIS_ENABLED and
in its own savepoint: a failed read logs its error class and leaves that part out, never the
report. The lines under the headline are shared (`formatting.jarvis_day_lines`, since
`recap_text` imports only formatting): «📤 Buyrug'ingiz bilan yuborildi: {n} ta», «❓ {n} ta
xabar yuborilganmi — aniq emas (/holat)», «🧪 Kuzatuv rejimida {n} ta xabar sizga
yo'naltirilardi», «💸 Jarvis xarajati: {x} / {cap}», «🧪 Qoralamalar sifati:» and «• {intent}:
{n} ta qoralama — {k} tasi o'zgartirilmasdan tasdiqlandi, {r} tasi tahrirlandi, {s} tasi erkin
gapli (o'zi yuborilmasdi)» (zero parts left out). The evening's «🤖 Bugun Jarvis: …» repeats the
brief's sentence for today and is its own section after «📅 Ertaga»; yesterday's activity alone
does not turn the brief's «✅ Hammasi joyida» into a full brief. `cards.brief_items` widens JV-18's
lines: every open, snoozed or owner-replying escalation that was shown (not only the snooze
limit's), named by person or chat title, and the unanswered chats are any draft of the last
day still waiting or expired unanswered (card or not) in a chat the owner has not written in
since and no reply was sent into since (a later draft the owner sent answers an older one),
leaving out chats already named among the open escalations. Lists show ten names, then
«… va yana {n} ta». «✍️ Kutayotgan qoralamalar» counts `draft` rows not run out, and
`/qoralamalar` now lists them («✍️ Kutayotgan qoralamalar:», «{i}. j{id} · {ism}: «{mijoz}» →
«{qoralama}» [{intent}]») above the observation drafts, since capped drafts have no card; its
help line reads «/qoralamalar — tasdiq kutayotgan qoralamalar va kuzatuv rejimidagi
javoblar», its menu entry «Kutayotgan va kuzatuv qoralamalari», and with
neither it says «✍️ Kutayotgan va kuzatuv qoralamalari hali yo'q.».
`Ochiq eskalatsiya` in Client_FollowUps is the client's oldest open shown escalation as
«{sabab} · {vaqt} dan beri»: its age is written as the moment it opened, so the row is not
rewritten every minute; a client with only an open escalation gets a row, and with Jarvis off
the column stays blank. The intent labels moved from `replies.py` to `formatting.py`
(`replies.jarvis_intent_label` still works).

---

#### JV-23 — Rollout controller: per-intent graduation, auto mode, demotion

**Priority** P1 · **Depends on** JV-19, JV-21 · **Migration** `0028_auto_shapes`

**Goal.** After 1-2 weeks of drafts, autonomy grows to the spec's level one intent at a time,
always with the owner's yes.

**Files.** `miya/jarvis/rollout.py`, `miya/jarvis/qbank.py`, `miya/jarvis/policy.py`,
`miya/jarvis/gate.py`, `miya/jarvis/redlines.py`, `miya/jarvis/outbox.py`, `miya/jarvis/cards.py`,
`miya/jarvis/status.py` (a `free_question` counts as strayed), `miya/jarvis/caps.py`,
`miya/jarvis/draft.py` and `miya/jarvis/responder.py` (the drafter is given the question bank),
`miya/jarvis/__init__.py`, `miya/db/models.py`, `alembic/versions/0028_auto_shapes.py`,
`miya/worker/main.py`, `miya/bot/handlers.py`, `miya/bot/keyboards.py`, `miya/bot/replies.py`,
`miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`, `tests/test_rollout.py`,
`tests/test_responder.py`, `tests/test_outbox.py`, `tests/test_redlines.py`, `tests/test_style.py`,
`tests/test_brief_jarvis_lines.py`.

**Behaviour.** `jarvis_rollout` (daily) computes channel × intent metrics from outbox history and
offers promotion (§6.4) with `jg:y` / `jg:n`. In auto mode rows are approved with `auto:policy`
only when their bound verdict carries `shapes_ok` (rule 13a); a graduated intent's text with a
free-form statement is a draft card (`unapproved_statement`), and the claim redrafts an auto row
without a passing shape check. Those drafts count in the intent's metrics like any other;
each send produces a silent notice with `jv:x:<id>` «↩️ Bu xato edi», which demotes that intent.
Breakers and incidents demote automatically. Never-auto intents are rejected. `/jarvis rejim`
shows the mode of each intent. Auto rows are held in quiet hours unless
`JARVIS_AUTO_IN_QUIET_HOURS`.

Two binding decisions from the red-line work are part of this WP:

- **(A) The question bank.** An auto-sendable question must come from a closed, owner-editable
  question bank (§5.2 step 3): seeded in Uzbek Latin, Uzbek Cyrillic and Russian with the origin
  city, destination, cargo type, weight, volume or cubes, number of places, GS code, contact
  person, documents or invoice and pickup address, with no time, price or promise (every seed
  passes the red-line guard in the reply role and the shape check; a test holds them), matched
  after normalisation, with slot fillers limited to the counterparty's name or honorific, a
  courtesy segment, and the GS codes and waybills already in the chat. A free-form AI question
  stays draft-only: `shapes_ok`
  is false for auto unless every question sentence matches a bank entry (`free_question`
  otherwise); drafts are unaffected. The owner edits the bank with `/savolbank` (list, `qo'sh`,
  `o'chir`); each addition is guarded and enters the bank only on «✅ Qo'shish» (`qb:y|n|d:<ref>`,
  not in `_button_handlers`). No new table: the bank is the history of the `jarvis_policy` row
  `question_bank`, replayed over the code seeds (`qbank.replay`); a removal takes the question's
  twins in the other scripts too (the Latin and Cyrillic texts share a key; `qbank.TWINS` groups
  each seed's Latin, Cyrillic and Russian texts). In an auto-eligible intent the drafter is given
  the bank's texts and asks only those, word for word (§5.6); a question it writes outside them
  is a card for the owner.
- **(B) Defense in depth.** Migration `0028_auto_shapes` adds `ck_outbox_messages_auto_shapes`
  (§4.2): an AI-authored row with `approved_by='auto:policy'` must carry `shapes_ok` true;
  NULL never passes. The later migrations are renumbered (JV-24 `0029`/`0030`, JV-25 `0031`).

Also: the holding reply (`stall`) and `other` are never autonomous (§6.2), so a bare courtesy
reply never goes out on its own for a message with a payment claim or a price, deadline or
promise ask (JV-16 escalates those; `test_courtesy_never_auto_after_a_red_line_ask` pins it at
the auto level). The metrics window starts at the channel × intent's last mode change or
incident; the cost cap reached twice in the week holds an offer back (the next daily job would
demote it again); an offer is made at most once every `JARVIS_GRADUATION_MIN_DAYS` days and the tap
re-checks the criteria under the scope's row lock (an old offer is stale). A breaker trip demotes
every autonomous intent of its channel inside `policy.trip_breaker` (contained); the daily job
reads every autonomous send of the week again with that day's guard, paging by id (a red line
found is an incident; a shape note such as `free_question` is never its reason)
and demotes on the cost cap reached twice in a week; each automatic demotion is told once
(«↩️ «{intent}» yana qoralama rejimiga qaytdi: {sabab}.»). The sent text of a «Bu xato edi», of
an incident, and of a draft whose red-line or free-form sentence the owner took out in an edit is
kept on its outbox row (`corpus_candidate`) as a review case for `tests/fixtures/redline_corpus.py`;
`/jarvis rejim` counts them.

**Owner strings.** "🎓 «{intent}» javoblari tayyor: {d} kunda {n} ta qoralama, {p}% tini
o'zgartirmasdan tasdiqladingiz, qizil chiziq buzilmadi. Endi bunday javoblarni MIYA o'zi
yuborsinmi? (kuniga {cap} tagacha)" Buttons "✅ Ha, avtomatik" / "⏳ Yana kuzataylik" · "🤖
Avtomatik yuborildi · {ism}: «{matn}»" with "↩️ Bu xato edi" · "↩️ «{intent}» yana qoralama
rejimiga qaytdi: {sabab}." · intent labels "salomlashish", "ma'lumot so'rash", "umumiy savol",
"kutib turish javobi" · ceiling hint: "Avtomatik rejim uchun serverdagi .env da JARVIS_MODE=auto
qilish kerak." · the demotion reasons, the `/jarvis rejim` lines and the `/savolbank` strings are
in `miya/bot/replies.py` (`JARVIS_DEMOTION_REASONS`, `JARVIS_MODES_*`, `QBANK_*`).

**Tests.** `test_criteria_math`, `test_no_silent_promotion`, `test_never_auto_intents_rejected`,
`test_xato_demotes`, `test_env_ceiling_respected`, `test_auto_held_in_quiet_hours`,
`test_auto_needs_shapes_ok`; besides: `test_question_bank_seeds_pass_the_guard`,
`test_bank_slot_fillers_are_name_honorific_and_codes`, `test_savolbank_add_and_remove`,
`test_a_removed_bank_question_redrafts_at_the_claim`,
`test_breaker_and_incident_demote_automatically`, `test_cost_cap_twice_in_a_week_demotes`,
`test_courtesy_never_auto_after_a_red_line_ask`, `test_owner_edit_is_kept_for_review`,
`test_rejim_shows_each_intent`, `test_migration_0028_takes_the_approver_off_forged_rows`.

**Acceptance.** Green; graduation happens only on the owner's tap.

**Deviations recorded.** (1) §6.2 listed `stall` among the intents that may ever be auto; the
JV-23 binding list makes it never-auto, and `gate.AUTO_INTENTS` is now `policy.AUTO_INTENTS`
without it. (2) The CHECK compares `guard->'shapes_ok'` with the JSON boolean `true` rather than
`guard->>'shapes_ok' = 'true'`, which would also have let the string `"true"` pass. (3) The
`jarvis_policy` scope pattern gains `question_bank` in 0028 (the bank lives there instead of a
new table). (4) A bank question's slot fillers include a courtesy segment («Rahmat aka, yuk
qaysi shahardan?»), as §5.2 step 3 describes, besides the name, honorific and codes. (5) §5.6's
drafter inputs gain the owner's question bank in an auto-eligible intent: owner-authored and not
private, like the KB entries, so a graduated `info_gathering` intent asks the bank's questions
word for word instead of writing its own. (6) §6.4's criteria gain the cost cap: an intent is
not offered, and a tap does not promote it, while the cost cap was reached on two days of the
last week, since the next daily job would demote it again.

---

### Phase 4 — more channels (weeks 3-6)

#### JV-24 — Multi-channel identity schema

**Priority** P2 · **Depends on** JV-11 · **Migration** `0029_source_labels`, `0030_channel_accounts`

**Goal.** WhatsApp and Gmail chats and people are first-class.

**Files.** `alembic/versions/0029_source_labels.py`, `alembic/versions/0030_channel_accounts.py`,
`miya/db/enums.py`, `miya/db/models.py`, `miya/services/people.py`, `miya/services/purge.py`,
`tests/conftest.py`, `tests/test_schema.py`, `tests/test_channels_schema.py`.

**Behaviour.** As §4.2. `resolve_person` gains a `contact_handles` lookup next to `telegram_id`
and phone; `merge_into` moves handles, chats and threads; purge plans by `wa_chat_id` and
`email_thread_id`.

**Owner strings.** None.

**Tests.** `test_fresh_install_single_transaction_upgrade`, `test_handle_unique`,
`test_merge_moves_handles`, `test_fk_indexes`.

**Acceptance.** CI round trip and `alembic check` clean.

---

#### JV-25 — Windows, extraction, recall and purge for any chat kind

**Priority** P2 · **Depends on** JV-24 · **Migration** `0031_windows_any_chat`

**Goal.** WhatsApp and Gmail messages get promise and task extraction (including the owner's own
outgoing messages), recall and purge.

**Files.** `alembic/versions/0031_windows_any_chat.py`, `miya/services/windows.py`,
`miya/services/passages.py`, `miya/services/recall.py`, `miya/services/purge.py`,
`miya/db/models.py`, `tests/test_windows_any_chat.py`.

**Behaviour.** Grouping by whichever chat column is set (`windows.py:192` stops filtering on
`telegram_userbot` only); Telegram behaviour unchanged.

**Owner strings.** "💬 WhatsAppdan yozib olindi" / "✉️ Gmaildan yozib olindi".

**Tests.** `test_wa_window_extracts_owner_promise`, `test_email_thread_window`,
`test_tg_unchanged`, `test_purge_chat_wa`.

**Acceptance.** Green; no Telegram test regresses.

---

#### JV-26 — Gmail read and extract

**Priority** P2 · **Depends on** JV-02, JV-04, JV-09, JV-25 · **Migration** none

**Goal.** Business mail is ingested, filtered and extracted; attachments go through the shield.

**Files.** `miya/services/gmail.py` (`GmailAPI` ABC + implementation), `miya/tools/gmail_auth.py`,
`miya/worker/main.py`, `miya/services/health.py`, `miya/jarvis/policy.py`, `miya/config.py`,
`.env.example`, `README.md` (config, egress), `miya/tools/doctor.py`, `Makefile`,
`tests/test_gmail_read.py`.

**Behaviour.**
1. A separate token file with the `gmail.modify` scope, created by `make gmail-auth` (the
   `gcal-auth` loopback pattern, `Makefile:125-127`); the calendar token is untouched. The guide
   says to publish the consent screen to production.
2. `gmail_pull` polls `history.list` every `GMAIL_PULL_MINUTES`, falls back to a full sync on 404,
   stores the cursor on `channel_accounts.sync_cursor`. Calls via `asyncio.to_thread` and
   `execute(num_retries=5)`. Only the worker refreshes tokens (its `/app/secrets` is writable).
3. Filters: INBOX with no category or CATEGORY_PERSONAL; skip SPAM, PROMOTIONS, SOCIAL, UPDATES,
   FORUMS, auto-submitted, bulk, list and no-reply. HTML to text with hidden content and quoted
   chains dropped.
3a. The owner's own mail is read too (the spec reads incoming **and outgoing** messages): a
   SENT message in a thread that passed the filters is stored with `direction=out` and is the
   owner's words by the JV-10 predicate (unless it is an outbox echo), so JV-25 extracts the
   promises and tasks the owner makes by email.
3b. `gmail_pull` is read-only: it never modifies labels, never marks read and never sends; only
   `gmail_send` (JV-27) removes UNREAD after a sent reply.
3c. The first `gmail_pull` run inserts `jarvis_policy('channel:gmail', mode='off')` through
   `policy.seed_channel` (`ON CONFLICT DO NOTHING`, no migration), so mail is read-only until the
   owner raises it with `/jarvis yoq gmail` (§6.3).
4. Attachments only via `attachments.get` when the shared policy allows; images go to the receipt
   reader.
5. `invalid_grant` raises the problem `gmail_auth_failed`. No-op without a token.

**Owner strings.** "✉️ Gmail ulandi: {manzil}" · "✉️ Gmail ruxsati tugadi — `make gmail-auth` bilan
qayta ulang." · "✅ Gmail ulanishi tiklandi" · `/holat`: "✉️ Gmail: oxirgi tekshiruv {vaqt}".

**Tests.** `test_history_incremental_with_fake`, `test_404_full_sync`,
`test_filters_skip_bulk_and_promotions`, `test_hidden_html_dropped`,
`test_attachment_policy_shared`, `test_invalid_grant_problem`, `test_noop_without_token`,
`test_sent_mail_is_owner_words_and_extracted`, `test_pull_never_modifies_or_sends`,
`test_gmail_channel_starts_off`.

**Acceptance.** Fake-API tests pass. Owner check: a client email appears in recall.

---

#### JV-27 — Gmail reply through the outbox

**Priority** P2 · **Depends on** JV-18, JV-23, JV-26 · **Migration** none

**Goal.** Draft and approve email replies in the owner's style, sent in the right thread.

**Files.** `miya/services/gmail.py`, `miya/worker/main.py`, `miya/jarvis/responder.py`,
`miya/jarvis/cards.py`, `miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`,
`tests/test_gmail_send.py`.

**Behaviour.** `gmail_send` (1 min) claims through `claim_ready('gmail', …)`, builds an RFC 2822
reply (`Re:` subject, `In-Reply-To` = original Message-ID, `References`, `threadId`) to the
authenticated sender (never a spoofed Reply-To on another domain), sends with retries, removes
UNREAD only after a send. `GMAIL_DAILY_SEND_CAP`. Email is its own channel with its own
graduation.

**Owner strings.** Card channel label "Gmail" · "✉️ Javob yuborildi: «{mavzu}»".

**Tests.** `test_threaded_headers`, `test_reply_to_sender_not_reply_to_spoof`, `test_cap`,
`test_exactly_once`, `test_gmail_send_uses_claim_ready_only` (AST: the only `messages.send` call
site takes its row from `claim_ready`), `test_no_attachment_part_built`.

**Acceptance.** Fake-API tests pass. Owner check: an approved reply lands in the same thread.

---

#### JV-28 — WhatsApp sidecar skeleton: pairing, reconnect, heartbeat, bridge auth

**Priority** P2 · **Depends on** JV-21, JV-24 · **Migration** none

**Goal.** A linked WhatsApp session that reports its health, without reading or sending yet.

**Files.** `whatsapp/package.json`, `whatsapp/package-lock.json`, `whatsapp/tsconfig.json`,
`whatsapp/src/main.ts`, `whatsapp/src/auth-sqlite.ts`, `whatsapp/src/bridge.ts`,
`whatsapp/Dockerfile`, `docker-compose.yml`, `Makefile`, `miya/api/deps.py`, `miya/api/main.py`,
`miya/api/bridge.py` (new router), `miya/api/middleware.py`, `miya/services/health.py`,
`miya/worker/main.py`, `miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`,
`.github/workflows/whatsapp-ci.yml`, `.github/workflows/python-ci.yml`,
`tests/test_bridge_auth.py`.

**Behaviour.**
1. The Baileys package is pinned to an exact patched release (≥ the version that fixes the 2026
   message-spoofing advisory), installed with `npm ci`, lockfile committed; the exact official
   package name is checked in CI against typosquats.
2. Auth state in SQLite (WAL) at `./data/whatsapp/auth.sqlite` with transactional key writes
   covering the v7 key types; never in PostgreSQL, never in backups; file mode 0600.
3. `markOnlineOnConnect:false`; broadcast and newsletter JIDs ignored; never a global
   `available` presence.
4. Pairing prefers a pairing code for `WHATSAPP_PHONE`, falls back to a QR PNG; both are posted to
   `/v1/bridge/whatsapp/pairing` and the worker delivers them to the owner.
5. Reconnect with backoff and jitter; 515 after pairing reconnects; 401 logged out, 440 replaced,
   403 forbidden or `reachoutTimeLock.isActive` stop the sidecar, update `channel_accounts.state`
   and alert. It never loops.
5a. Rule 25 in Node: TypeScript `strict`, every event callback and bridge call in try/catch with a
   structured JSON log line (ids and counts only, never bodies), `unhandledRejection` and
   `uncaughtException` handlers that log and keep the process up (only an auth-store integrity
   error exits, for compose to restart it); bridge POSTs retry with exponential backoff and
   jitter; the API being down never crashes the sidecar.
6. A third token family in `deps.check_authorization`: `WHATSAPP_BRIDGE_TOKEN` opens only
   `is_bridge_path()` (exact paths under `/v1/bridge/whatsapp/`); `is_upload_path` is never
   extended. `assert_startup_config` and doctor C5 warn on short or reused tokens.
7. Compose service `whatsapp` under `profiles: ['whatsapp']`, uid 10001, `./data/whatsapp`
   volume, explicit `environment:` only, no published ports, depends on `api` healthy; `init`
   mkdirs `/data/whatsapp`. `make whatsapp-pair`; `make update` restarts it only if it was
   running. python-ci gains `whatsapp/**` in `paths-ignore`.
8. `whatsapp` heartbeat; problems `whatsapp_silent` and `whatsapp_logged_out` only when
   `WHATSAPP_ENABLED`. README egress gains WhatsApp Web; the Internal API table gains the bridge
   routes.

**Owner strings.** "🟢 WhatsApp ulash kodi: {kod}\nWhatsApp → Bog'langan qurilmalar → Telefon raqami
bilan bog'lash." · "🟢 WhatsApp ulandi." · "🔴 WhatsApp hisobidan chiqildi — qayta ulash: make
whatsapp-pair" · "⛔ WhatsApp hisobingizga vaqtincha cheklov qo'ydi ({gacha}). MIYA WhatsAppdan
yozmaydi." · "✅ WhatsApp ulanishi tiklandi".

**Tests.** `test_upload_token_cannot_reach_bridge`, `test_bridge_token_cannot_read_config_or_ask`,
`test_heartbeat_problem_gating`, `test_compose_whatsapp_profile_pinned`,
`test_compose_whatsapp_env_explicit_only`; Node unit tests for disconnect classification, the
auth store, bridge retry with backoff and `unhandledRejection` containment.

**Acceptance.** Python and Node CI green; the core stack starts without the profile. Owner check:
pairing by code works.

---

#### JV-29 — WhatsApp ingest (read-only) with the media policy

**Priority** P2 · **Depends on** JV-01, JV-04, JV-09, JV-25, JV-28 · **Migration** none

**Goal.** WhatsApp DMs, voice notes and images flow into MIYA like Telegram.

**Files.** `whatsapp/src/ingest.ts`, `whatsapp/test/write-calls.test.ts`, `miya/api/bridge.py`,
`miya/services/wa_ingest.py`, `miya/jarvis/policy.py`, `miya/worker/main.py`,
`tests/test_wa_ingest.py`.

**Behaviour.** The sidecar maps `messages.upsert` (`notify` and `append`) to batch POSTs to
`/events`, skipping broadcast, newsletter and protocol messages; groups default off. The API
stores interactions idempotently (`ux_interactions_wa_message`, the SAVEPOINT pattern of
`phone_events._ingest_batch`), answering `{accepted, duplicates, rejected}`. People resolve via
handles with LID and phone mapping (`remoteJidAlt`, `lid-mapping.update`). `fromMe` becomes
`direction=out` (owner words, unless an outbox echo). The sidecar asks `/media-policy` before any
download; dangerous types are never downloaded and produce a warning; allowed media streams to
`/media` and is transcribed or vision-read by the worker; then the candidate is enqueued.
The first accepted batch inserts `jarvis_policy('channel:whatsapp', mode='off')` via
`policy.seed_channel`, so the 7 read-only days of §6.3 produce no drafts; the owner raises it
with `/jarvis yoq whatsapp`. The ingest path never calls `readMessages`, `sendPresenceUpdate`,
`sendMessage` or `chatModify`; only named functions of `whatsapp/src/sender.ts` (JV-30) may, which
a Node static test (`write-calls.test.ts`, the counterpart of the Python AST guard) enforces.

**Owner strings.** The JV-01 and JV-09 warnings with "(WhatsApp)".

**Tests.** `test_batch_idempotent`, `test_lid_and_pn_resolve_same_person`,
`test_policy_blocks_before_download`, `test_fromme_is_owner_words`,
`test_voice_transcribed_then_enqueued`, `test_whatsapp_channel_starts_off`; Node
`write-calls.test.ts`.

**Acceptance.** Green. Owner check: a week of read-only WhatsApp shows up in recall and the brief.

---

#### JV-30 — WhatsApp send through the outbox

**Priority** P2 · **Depends on** JV-23, JV-29 · **Migration** none

**Goal.** Approved WhatsApp replies are sent with the same pacing and stricter limits.

**Files.** `whatsapp/src/sender.ts`, `miya/api/bridge.py`, `miya/jarvis/outbox.py`,
`miya/config.py`, `.env.example`, `README.md`, `miya/tools/doctor.py`, `tests/test_wa_send.py`.

**Behaviour.** The sidecar polls `/outbox/claim`, which calls the same `claim_ready('whatsapp',
…)`. It follows the pacing plan (`readMessages` on the trigger key only when sending,
`composing` presence refreshed about every 8 s, then `paused`), sends, and posts the result with
the external id to `/outbox/{id}/result`. A restriction lock, 403 or a falling new-chat cap
(`fetchNewChatMessageCap`) trips the breaker. `WHATSAPP_SEND_ENABLED` defaults false; no first
message to new chats; `WHATSAPP_DAILY_SEND_CAP`; own graduation.

**Owner strings.** Card channel label "WhatsApp"; breaker texts from JV-28.

**Tests.** `test_claim_gate_shared`, `test_result_completes_once`, `test_timelock_trips_breaker`,
`test_new_chat_refused`, `test_bridge_claim_cannot_bypass_gate` (the bridge route has no other
way to read an outbox row); Node `write-calls.test.ts` extended to `sender.ts` and a pacing-plan
unit test.

**Acceptance.** Works against a fake bridge. Owner check: one approved WhatsApp draft is delivered.

---

#### JV-31 — Owner guide and decision record for Jarvis

**Priority** P2 · **Depends on** JV-19 · **Migration** none

**Goal.** The owner knows how to pair, approve, pause and graduate, in Uzbek.

**Files.** `docs/ornatish.md`, `docs/owner-decisions.md`, `docs/handoff-plan.md`, `README.md`.

**Behaviour.** The guide covers personal chats first, copy mode, approving drafts, the three
escalation buttons, `/jarvis toxtat`, Sheets sharing, Gmail auth (consent screen in production),
WhatsApp pairing and re-pairing after a restore, the cost cap and the `.env` ceiling for auto
mode. The handoff plan lists the JV packages.

**Owner strings.** Headings: "Jarvis: birinchi hafta", "Shaxsiy chatlarni belgilash", "Qoralamani
tasdiqlash", "Jarvisni to'xtatish", "WhatsAppni ulash", "Gmailni ulash", "Google Sheets".

**Tests.** The existing doc, username and vendor-identifier guard tests pass.

**Acceptance.** The owner can follow the guide without help.

---

## 10. Risks

1. **Account bans.** Telegram and WhatsApp restrict unofficial automation; a limit on the owner's
   real accounts is possible despite reply-only behaviour, caps, pacing and breakers. Core MIYA
   keeps reading through a sending ban; each channel has its own breaker; WhatsApp comes last and
   starts read-only.
2. **Session loss.** A second Telethon client on the same session risks `AuthKeyDuplicatedError`;
   the sender lives in the userbot process (JV-19), and the AST guard is rescoped, never deleted.
3. **Self-ingestion.** A consumer that misses the authorship predicate would turn an AI stall into
   an owner promise or train the style profile on AI output. JV-10 lands first and is grep-tested.
4. **Exactly-once.** A crash between the network send and the commit leaves `unknown` rows; MIYA
   tells the owner and never resends. Telegram `random_id` narrows the window further.
5. **Lexicon quality.** False positives («Juma» as a name, «kecha», «kurs») make autonomy feel
   useless; false negatives in dialect and ASR text slip through. Shadow mode measures both; the
   corpus grows from real data; the output guard stays zero-digit and fails closed.
6. **Approval fatigue.** Too many cards lead to rubber-stamping. Draft caps, expiry, debounce and
   owner-first windows reduce volume; graduation numbers are monitored.
7. **Prompt injection** through client text, images and email. Isolation is the defence; filters
   only alert.
8. **Dependency churn.** The WhatsApp library is a release candidate with a recent critical
   advisory, and npm has had typosquats. Exact pin, lockfile, CI name check, review every upgrade.
9. **Google token expiry** when the consent screen stays in Testing. A health problem and the guide
   mitigate it; Sheets uses a service account.
10. **Fake-receipt false alarms** for accounts whose SMS never reaches the phone. «Suspicious» only
    when the payee card is the owner's and the phone is healthy.
11. **Cost growth.** Hard daily cap, labelled `usage_log` operations, deterministic fast paths.
12. **Migration traps.** New enum labels in the same run, ids over 32 characters, server-default
    drift. Labels are isolated in 0029; CI round-trips.
13. **Compromised server.** With sending enabled, it could message clients as the owner. SQL caps,
    the outbox-only path, breakers, the kill switch and approval in the careful phase contain it.
14. **Sheets misuse.** Someone treats the mirror as truth. It is one-way and labels mirrored
    figures.
15. **Legal and honesty exposure.** Processing third-party WhatsApp and Gmail content under
    Uzbekistan's personal-data law is unverified; identity questions always escalate.

---

## 11. Open questions (defaults are used if the owner does not answer)

| # | Question | Default chosen |
|---|---|---|
| Q1 | Which business facts seed the knowledge base? | Office address and hours, working days, routes (China → Uzbekistan via Yiwu), accepted cargo types, required documents; no prices. |
| Q2 | Default honorific for a new contact with no history? | Neutral «Siz»; «aka» only where the owner's own history uses it. |
| Q3 | Auto replies during quiet hours? | No (`JARVIS_AUTO_IN_QUIET_HOURS=false`). |
| Q4 | Escalations at night: loud, silent or held? | Delivered silently, never held. |
| Q5 | WhatsApp number to pair, and is it the Business app? | The owner's existing long-lived number; pairing code; Business vs personal recorded at pairing. |
| Q6 | Gmail address, and may MIYA remove UNREAD? | The main business address; `gmail.modify`; UNREAD removed only after a sent reply. |
| Q7 | Sheets: who else gets access; Uzbek columns? | Owner plus the service account; Uzbek headers as in JV-07. |
| Q8 | Daily Jarvis cost cap? | 3.00 USD (`JARVIS_DAILY_COST_CAP_USD`); spend priced from config only. |
| Q9 | Owner-dictated messages to personal chats? | Allowed with an explicit warning on the card; never AI-authored. |
| Q10 | Which cards receive client payments? | `PAYMENT_OWN_CARDS` as set; without it no receipt is ever «suspicious» for `own_card_no_bank`. |
| Q11 | Does Telethon 1.44 dispatch self-sent messages to NewMessage handlers? | Assume no; the sender writes and tags the echo itself either way (JV-19). |
| Q12 | Fold extra escalations into one summary? | Yes, above `JARVIS_ESCALATION_DAILY_CAP`. |
| Q13 | Graduation per chat, per intent or global? | Per channel × intent, owner-confirmed; plus the `.env` ceiling. |
| Q14 | Node sidecar or a pure-Python WhatsApp client? | Node/TypeScript sidecar with the official library (accepted by the owner); isolated in a compose profile. |
| Q15 | Look-alike domain allowlist (payme.uz, click.uz, uzum.uz, humocard.uz, my.gov.uz, soliq.uz, …)? | The code seed list; the owner verifies it and may add domains via `SAFETY_EXTRA_SAFE_DOMAINS`. |
| Q16 | Should the personal list auto-import existing family/friend contacts? | No; suggestions only (JV-12), one tap each. |
| Q17 | What should the AI say when a client sincerely asks «botmisiz?» | Nothing on its own: always escalate; never deny being an AI. |
