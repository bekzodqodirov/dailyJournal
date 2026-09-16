# Owner decisions (2026-09-15)

Answers the owner gave to the eight questions from the vision audit. These are
product decisions, not defaults: code that contradicts them is wrong.

## Sources and priority
- **Phone and Telegram** are where his conversations happen. For now, effort
  goes to **Telegram**; the Android companion is deferred ("keyinroq").
- Money notifications arrive mostly via **Payme**; **SMS** capture is welcome
  later. Not built yet.

## Counterparty claims
- When someone *else* says "you owe me" or "I paid you back": **ask first**,
  never write silently.
- He tolerates **20–30 confirmations a day**.
- Implemented in step 3 as *claims*: money a counterparty asserts (a debt, a
  repayment, a transaction), an obligation they say he took on, and their
  own "I did my part" wait in `/davolar` with Ha / Yo'q / Tuzat. A
  counterparty's own promise is written as before (opening one is safe;
  closing one on their word is not). A repayment accepted before its debt
  stays open until the debt is confirmed, so a Ha is never spent on nothing.

## Reminders and open loops
- Undated promises and unanswered questions: **re-remind after one week**.
- Morning brief at **09:00 Asia/Tashkent**.
- An unanswered question gets **one** nudge (after `LOOP_QUESTION_HOURS`),
  then only the brief and the evening report carry it. "⏰ Ertalab eslat"
  brings it back once more at the next brief. Chosen in step 2 so the same
  question is not pinged three times a day.
- Only groups discovered after step 2 is deployed are asked "o'qiymi?";
  groups switched off earlier in /chats stay off without a question.

## Which chats
- **Every private chat** is read (the current default stands).
- New groups: **on with one tap** (ask in the bot, one button).

## Retention
- Keep verbatim messages **indefinitely** unless it becomes expensive;
  keeping them only until debts/promises are extracted is also acceptable.
  He would prefer the system to learn what matters on its own.

## Backups
- Implemented in step 5: `pg_dump -Fc | age` nightly, sent to the owner's
  Telegram (split into 45 MB pieces when large), restored with
  `make restore`. The key in `secrets/backup-key.txt` is the only way back.
- **Yes**, send the encrypted backup to Telegram nightly.

## How people address him
Latin and Cyrillic forms all count as addressed to him:
`Bekzod`, `Begi`, `Bekzod aka`, `Begika`, `Bega`, and the company `GSR Logistics`.

## The bar
"The system must adapt to me 100% and know everything about me. When I work
with clients it is my assistant: when I ask *what was that thing that
happened*, it answers."

## Agreed build order
1. Close and correct (buttons, /bajarildi, /tuzat, /yop, fulfilment) — done
2. Open loops and the morning brief — done (needs `OWNER_ALIASES` in `.env`)
3. Ask-before-writing for counterparty claims — done
4. Per-person memory — done (`/kim`, `/tarix`, `/eslab`; profiles refresh every 30 min)
5. Self-monitoring (/holat, heartbeat, backups to Telegram) — done
6. Phone app, missed calls, SMS — done (the owner un-deferred it; the app
   uploads call-log events and payment SMS, missed calls become open loops,
   Payme/bank SMS become transactions without a model call)
