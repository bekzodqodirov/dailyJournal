# Owner decisions (2026-09-15, updated 2026-09-25)

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
