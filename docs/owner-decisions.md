# Owner decisions (2026-09-15, updated 2026-09-25 and 2026-10-20)

The 2026-09-25 answers replace earlier ones where they differ; see docs/handoff-plan.md §1.5.
Lines marked 'Design default' are implementation choices, not the owner's words.

Answers the owner gave to the questions from the vision audit (2026-09-15) and
again on 2026-09-25. These are product decisions, not defaults: code that
contradicts the owner's words is wrong. A design default may change; the
owner's words may not be contradicted.

## Sources and priority
- Owner's words (2026-09-25): «muhum suhbatlarim telegram va telefonda» — my important
  conversations are on Telegram and the phone. **Both are primary sources.**
- Owner's words (2026-09-25): «sms va payme ilovadan keladi» — money notifications come by
  SMS and from the Payme app. **Both are captured** (the Payme app's own push notifications,
  not only SMS).
- Design default (change if the owner disagrees — handoff-plan §5 Q57): one deterministic
  parser classifies both and de-duplicates across channels. A completed payment is booked
  without asking and reported to Telegram with 🗑 O'chir and ✏️ Tuzat; an uncertain one waits
  in /tekshir, and the likely-real uncertain ones are also offered once in the daily question
  batch. /pul lists the day, /ochir x12 voids, /tuzat x12 corrects.

## Counterparty claims
- When someone *else* says "you owe me" or "I paid you back": **ask first**,
  never write silently.
- Owner's words (2026-09-25): «tasdiq sorasin 5-10 ta tasdiq bosa olaman» — ask for
  confirmation; the owner can press **5–10 confirmations a day** (it was 20–30).
- Design default (change if the owner disagrees — handoff-plan §5 Q12, Q13): everything MIYA
  asks for a tap shares one daily budget, QUESTION_BUDGET_PER_DAY=10; each group in the
  new-groups digest counts as one. Money at stake comes first, then the kind of question, then
  age. Over-budget questions wait in /savollar and appear as one line in the morning brief and
  the evening recap; they are never dropped. Not counted: replies to the owner's own commands
  and to messages the owner sends the bot, money receipts, pulls (/savollar, /davolar,
  /tekshir) and health alerts. A claim row carried on an automatic chat receipt counts as one.
- Implemented in step 3 as *claims*: money a counterparty asserts (a debt, a
  repayment, a transaction), an obligation they say the owner took on, and their
  own "I did my part" wait in `/davolar` with Ha / Yo'q / Tuzat. A
  counterparty's own promise is written as before (opening one is safe;
  closing one on their word is not). A repayment accepted before its debt
  stays open until the debt is confirmed, so a Ha is never spent on nothing.

## Reminders and open loops
- Undated promises and unanswered questions: **re-remind after one week**.
- Morning brief at **09:00 Asia/Tashkent**.
- Owner's words (2026-09-25): «kuni ohirida va ertalab eslatib tursa boladi kun davomida
  bolgan anrsalarni» — at the end of the day and in the morning, remind the owner of what
  happened during the day.
- Design default (change if the owner disagrees — handoff-plan §5 Q22, Q24): an evening
  'Bugun nima bo'ldi' at REPORT_TIME and a morning 'Kecha' before the brief. Figures come only
  from SQL.
- Design default (2026-09-25, handoff-plan §5 Q12): unanswered questions, missed calls and
  'hali ochiqmi?' share the daily budget; what does not fit waits in /savollar and in the
  brief's queue line. '⏰ Ertalab eslat' still brings an item back at the next brief.
- Design default (2026-09-25, handoff-plan §5 Q15, Q16): new groups are offered in one daily
  digest, most active first, each group counting against the budget; channels are never
  asked; groups switched off earlier stay off without a question.

## Which chats
- **Every private chat** is read (the current default stands).
- New groups: **on with one tap** (ask in the bot, one button).
- Owner's words (2026-09-25): «hamma oqishga ruxsat berilgan chatlar oqilishi kerak … saqlanib
  borishi kerak … shunday narsa bolgandi nima deb oylaysan desam u chiqarib bersin» — every
  chat allowed for reading is read and stored, and 'such-and-such happened, what do you
  think?' must be answered from it.
- Design default (change if the owner disagrees — handoff-plan §5 Q37): 'allowed' means the
  chats the owner switched on; private chats start on, groups start off and are allowed with
  one tap. Answers cite their source.

## Clients
- Owner's words (2026-09-25): «GS necchidur kod yokida ismi boyicha aniqlashtiramiz» — clients
  are identified by their **GS code or by name**.
- Context (not the owner's words): the code, e.g. GS367, is printed on carton stickers next to
  a waybill number such as YW26-004715. Design default (handoff-plan §5 Q39): a code is exact
  and never fuzzy-matched.

## Jarvis: replies in the owner's name (2026-10-20, docs/jarvis-plan.md)
- Owner's words (2026-10-20), on autonomy: «Promptdagidek» — as the Jarvis spec says: greetings,
  asking how to help, gathering the shipment details and general questions are answered by MIYA;
  a price, a discount, payment terms, a deadline, a promise or a guarantee is never answered by
  it: a polite stall goes to the client and the owner is alerted at once.
- Owner's words (2026-10-20), on who MIYA may write to: «hammaga yoza olsin faqat men belgilagan
  chatlarga yozmasin shaxsiy chatlarga» — MIYA may write to everyone, except the personal chats
  the owner marks.
- Owner's words (2026-10-20), on channels — Telegram, WhatsApp, Gmail, Google Sheets: «hammasida
  yozsin legin apk fillarni ochmasin va spammlardan ehtiyot bolsin» — write on all of them, but
  never open APK files, and beware of spam.
- Owner's words (2026-10-20), on risk: «Ehtiyot bo'lib boramiz» — we go carefully.
- Design default (change if the owner disagrees — docs/jarvis-plan.md §6): everything starts
  switched off (`JARVIS_ENABLED=false`); once on, Jarvis first only stores what it would have
  written (`shadow`), then every reply is a card the owner approves (`draft`, the ceiling
  `JARVIS_MODE=draft`), with daily caps; a kind of reply becomes automatic only after the
  graduation numbers and the owner's own «Ha», and never for money, deadlines or promises.
  Groups get no AI reply unless the owner enables one; MIYA never writes first and never denies
  being an AI.
- Implemented in JV-11: every message MIYA may send is a row in `outbox_messages` that only
  `miya/jarvis/outbox.py` changes, and every send re-runs the whole gate when it is claimed (the
  kill switches, `/jarvis toxtat`, the personal-chat rules, the spam and file shield, the
  red-line guard, the caps and the cost cap). The rest of MIYA never loads Jarvis and works the
  same with it off.
- Implemented in JV-12: `/shaxsiy Ism` (or a GS code, or a family group's title) marks a chat
  personal at once and keeps every address the person is known at, so forgetting or merging the
  person never reopens it; `/shaxsiylar` lists the personal chats and suggests likely family and
  friends from the database, one tap each, and nothing is marked without the owner. Unmarking
  asks first. Groups get no AI reply until `/guruhai` switches one on with a confirm tap, and
  then only drafts for the owner's tap, only for messages addressed to the owner. A personal
  rule outranks every other rule, mode and setting; Saved Messages, Telegram's service chat,
  bots, channels, the owner's own names and WhatsApp broadcasts are never written to at all.

## Dangerous files (owner rule 4, docs/jarvis-plan.md decision 4)
- The owner's decision of 2026-10-20 (docs/jarvis-plan.md §8.1): MIYA never downloads,
  opens or offers to fetch an APK, an executable, an installer or an archive, on any
  channel, even from a known contact (such files often come from hijacked accounts); the
  owner is warned once instead.
- Implemented in JV-01: `miya/services/safety.py` classifies a file by its normalised name,
  its double extensions and its MIME type before any download, and checks the bytes of an
  allowed document before it is parsed. The blocklists are code constants; no setting can
  loosen them, and the owner's «✅ O'qi» tap cannot fetch such a file either. Macro
  documents are read as text only, with a note.
- The one exception is MIYA's own phone app: MIYA Companion comes only from the MIYA bot,
  with its signing-certificate SHA-256 printed under it (docs/ornatish.md, step 11).
- Design default (change if the owner disagrees): the warning goes out within a minute, also
  at night (silently then), and is never counted in QUESTION_BUDGET_PER_DAY; the owner's own
  outgoing files and messages older than a week raise no warning.

## Delivery
- Owner's words (2026-09-25): «telegramga jonatishi maqul» — everything is delivered to
  Telegram.

## Retention
- Keep verbatim messages **indefinitely** unless it becomes expensive;
  keeping them only until debts/promises are extracted is also acceptable.
  The owner would prefer the system to learn what matters on its own.
- Design default (2026-09-25, handoff-plan §5 Q51): message text is kept forever and only
  /unut deletes it; photo, document and video originals are also kept until the owner sets
  MEDIA_RETENTION_DAYS / VIDEO_RETENTION_DAYS.

## Backups
- Implemented in step 5: `pg_dump -Fc | age` nightly, sent to the owner's
  Telegram (split into 45 MB pieces when large), restored with
  `make restore`. The key in `secrets/backup-key.txt` is the only way back.
- **Yes**, send the encrypted backup to Telegram nightly.

## How people address the owner
- Owner's words (2026-09-25): «Bekzod bekzodaka bekzodaka yokida @‹username›» — people address
  the owner as Bekzod, Bekzod aka / bekzodaka, or by the owner's Telegram @username (kept in
  .env only, never committed). The earlier forms stay: `Begi`, `Begika`, `Bega`, the company
  `GSR Logistics`.
- Design default (change if the owner disagrees — handoff-plan §5 Q43, Q44): Latin and
  Cyrillic, any case, joined or hyphenated honorifics and Uzbek case endings (Bekzodga,
  bekzodakaga) all count. In a group where another Bekzod speaks, a bare «Bekzod» without
  «aka» is shown under «Balki sizga»; «Bekzod aka», «bekzodaka» and the @username always count.

## The bar
"The system must adapt to me 100% and know everything about me. When I work
with clients it is my assistant: when I ask *what was that thing that
happened*, it answers."

## Agreed build order
1. Close and correct (buttons, /bajarildi, /tuzat, /yop, fulfilment) — done
2. Open loops and the morning brief — done (needs `OWNER_ALIASES` in `.env`)
3. Ask-before-writing for counterparty claims — done
4. Per-person memory — done (`/kim`, `/tarix`, `/eslab`; profiles refresh at most once a day
   per person (2026-09-25, WP-24))
5. Self-monitoring (/holat, heartbeat, backups to Telegram) — done
6. Phone app, missed calls, SMS — done (the owner un-deferred it; the app
   uploads call-log events and payment SMS, missed calls become open loops,
   Payme/bank SMS become transactions without a model call)
7. Production hardening and the 2026-09-25 answers — docs/handoff-plan.md.
