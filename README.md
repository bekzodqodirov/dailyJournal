# MIYA — Personal AI Second Brain

Self-hosted, single-user life-logging system. Captures the owner's day from
Telegram, phone calls and notes; extracts structured facts (debts, promises,
transactions, events, tasks) with Claude; and answers questions, reminds, and
reports back in Uzbek.

Owner-facing language is Uzbek. Code, comments and commits are English.

**Status: all five phases complete.** The assistant bot captures text, voice,
photos and documents; call recordings sync in from the phone via Syncthing; a
passive Telegram userbot reads whichever chats the owner whitelists. Everything
is transcribed, extracted and persisted as debts, promises, transactions,
events, tasks and searchable memories. Questions are answered with figures
straight from SQL, a report and tomorrow's plan arrive every evening, reminders
go out hourly, calendar events sync both ways, and the database is backed up
encrypted every night.

---

## Architecture

```
┌──────────────── CAPTURE ────────────────────────────────────────────┐
│ A) Assistant bot (aiogram)   — owner-facing: notes, voice, receipts │
│ B) Userbot (Telethon)        — passive, read-only DM/group reader   │
│ C) Call recordings           — Syncthing folder sync from the phone │
│ D) Google Calendar           — pull events, push extracted ones     │
└────────────────────────────┬────────────────────────────────────────┘
                             ▼
┌──────────── PROCESSING (FastAPI + APScheduler workers) ─────────────┐
│ normalize → interactions row                                        │
│ audio → ElevenLabs Scribe → transcript                              │
│ docs → local text extraction · photos → Haiku vision triage         │
│ EXTRACTION: Claude Haiku → validated JSON  (real-time or Batch API) │
│ person resolution (rapidfuzz) → debts / promises / transactions / … │
│ facts → bge-m3 embeddings → pgvector                                │
└────────────────────────────┬────────────────────────────────────────┘
                             ▼
┌──────────── STORAGE: PostgreSQL 16 + pgvector ──────────────────────┐
│ people · chat_monitors · interactions · conversation_windows ·      │
│ debts · debt_payments · promises · transactions · events · tasks ·  │
│ memories · daily_reports · usage_log · reminder_log                 │
└────────────────────────────┬────────────────────────────────────────┘
                             ▼
┌──────────── ACT (via the assistant bot) ────────────────────────────┐
│ daily report (19:00 Tashkent) · tomorrow planner · RAG chat ·       │
│ reminders for due debts, promises and tasks                         │
└─────────────────────────────────────────────────────────────────────┘
```

**Money and debt answers always come from SQL, never from an LLM guess.** The
reasoning model only phrases a query result in Uzbek.

---

## Quick start

The owner's step-by-step guide, in Uzbek, is [docs/ornatish.md](docs/ornatish.md).

### Server requirements

- **At least 8 GB RAM** plus 2–4 GB swap; 2 vCPU (4 is better); 80 GB SSD;
  Ubuntu 24.04 LTS; Docker Engine with Compose v2.
- Outbound HTTPS to the Anthropic API, ElevenLabs, api.telegram.org,
  huggingface.co (on first start) and GitHub.
- Why 8 GB: the api holds the bge-m3 search model, about 2.2 GB of weights
  plus the torch runtime. Below 8 GB it is OOM-killed in a loop.

```bash
cp .env.example .env      # then fill it in — see below
make backup-key           # prints BACKUP_AGE_RECIPIENT=… for .env
make doctor               # checks .env and the server; fix every ❌
make up                   # runs doctor again, builds, starts, applies migrations
make health               # {"status":"ok", ...}
make bot                  # tail the assistant bot's logs
```

Before `make up`, fill in: `POSTGRES_PASSWORD` (and the same password inside
`DATABASE_URL`, before the first start), `ANTHROPIC_API_KEY`,
`ELEVENLABS_API_KEY`, `ASSISTANT_BOT_TOKEN`, `OWNER_TELEGRAM_ID`,
`TELETHON_API_ID` and `TELETHON_API_HASH` (unless `USERBOT_ENABLED=false`),
`OWNER_ALIASES`, `API_BEARER_TOKEN` (at least 32 characters: `openssl rand -hex
32`), `UPLOAD_TOKENS` for the phone, and `BACKUP_AGE_RECIPIENT` (`make
backup-key` prints the line). `make doctor` checks all of them, the RAM, the
swap and the disk; `make up` refuses to start while it reports an error
(`SKIP_DOCTOR=1` skips it, at your own risk). The bot and worker refuse to
start without a bot token and owner id rather than run unrestricted, and
every service refuses a blank or short API token.

`make help` lists every target.

| Target | What it does |
|---|---|
| `make up` | Build and start db, api, bot, worker and userbot, then migrate |
| `make down` | Stop everything (the database volume is kept) |
| `make migrate` | Apply migrations |
| `make revision m="…"` | Autogenerate a migration from the models |
| `make psql` | Open a psql shell |
| `make logs` / `make ps` | Tail logs / show container status |
| `make bot` / `make worker` / `make userbot` | Tail each process's logs |
| `make userbot-login` | One-time Telethon login (prints `TELETHON_SESSION`) |
| `make gcal-auth` | One-time Google Calendar OAuth (see below) |
| `make backfill CHAT=… DAYS=…` | Read one chat's recent history |
| `make import-clients FILE=… [APPLY=1]` | Import the client-code list (CSV/.xlsx); a dry run unless APPLY=1 |
| `make doctor` | Check `.env` and the server before `make up` |
| `make backup-key` | Create the backup key and print the `.env` line |
| `make backup-key-show` | Print the secret backup key (store it off the server) |
| `make backup` | Run the encrypted database backup now |
| `make test` / `make lint` / `make fmt` | Local dev loop |

On first start the api container downloads the bge-m3 embedding model (~2 GB,
cached in a volume). Until it finishes, `/qidir` and semantic search politely
report that search is not ready yet; everything else works immediately.

Two `/holat` alerts watch the server itself: **API restarting** (three or
more api starts within an hour — almost always too little RAM; the api is
capped at `API_MEM_LIMIT`, 4 GB by default, so the kernel kills it rather than
PostgreSQL) and **search down** (a memory waiting more than
`EMBED_STALE_MINUTES` for its vector — the first start downloads the ~2 GB
model, so give it half an hour).

### Local development without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make test          # schema + API tests; DB tests skip if no database
```

Database-backed tests skip automatically unless `DATABASE_URL` points at a
migrated PostgreSQL with the `vector` extension. Anthropic and ElevenLabs are
never called from the test suite — the client is stubbed, so `make test` costs
nothing and needs no keys.

---

## Configuration

Everything is read from `.env` (see `.env.example`). Nothing is hardcoded.

| Key | Notes |
|---|---|
| `DATABASE_URL` | Must match `POSTGRES_*`; driver is `postgresql+psycopg` |
| `ANTHROPIC_API_KEY` | Required — extraction, reports and answers all use it |
| `EXTRACT_MODEL` | `claude-haiku-4-5` — the extraction engine |
| `REASON_MODEL` | `claude-sonnet-5` — `/reja` planner, RAG answers |
| `ELEVENLABS_API_KEY` | Scribe transcription (uz/ru) |
| `TRANSCRIBER` | `elevenlabs`; a local Whisper backend can be swapped in later |
| `ASSISTANT_BOT_TOKEN`, `OWNER_TELEGRAM_ID` | The bot rejects every other user |
| `USERBOT_ENABLED` | One-flag kill switch for the passive Telegram reader |
| `OWNER_ALIAS_NAMESAKE_DAYS` | 90 — in a group where another member with the owner's first name spoke this recently, a bare first name is only "maybe to you" |
| `USERBOT_CATCHUP_MINUTES`, `USERBOT_CATCHUP_MAX_PER_CHAT`, `USERBOT_CATCHUP_MAX_CHATS` | 30 / 500 / 20 — after downtime the userbot re-reads allowed chats past the last message it saw, never before a chat was switched on |
| `API_BEARER_TOKEN` | `openssl rand -hex 32` — required for every `/v1/*` route |
| `TIMEZONE`, `REPORT_TIME`, `QUIET_HOURS` | Asia/Tashkent, 19:00, 23:30–07:30 |
| `EMBED_SERVICE_URL` | Blank in `.env`; compose points bot/worker at the api container so only one process holds bge-m3 in RAM |
| `GOOGLE_OAUTH_CLIENT_JSON`, `GOOGLE_TOKEN_JSON` | Calendar OAuth files under `secrets/`; sync stays off until the token exists |
| `GCAL_CALENDAR_ID`, `GCAL_PULL_MINUTES`, `GCAL_DAYS_AHEAD` | Which calendar, how often, how far ahead |
| `PAYMENT_SMS_SENDERS` | Extra bank/payment SMS senders on top of the built-in list |
| `PAYMENT_APP_PACKAGES` | Blank — package ids of the payment apps whose pushes may book money; blank trusts the phone's allow-list |
| `PAYMENT_ADVERTS_TO_REVIEW` | `false` — bank adverts are stored and ignored; `true` sends them to `/tekshir` |
| `MONEY_AUTOBOOK` | `true` — the emergency brake: `false` sends every completed payment to `/tekshir` instead of booking it |
| `PAYMENT_DEDUPE_WINDOW_MINUTES`, `PAYMENT_REPEAT_SECONDS` | 10 min / 120 s — one payment seen by SMS and app push is booked once; a re-posted text is not a second payment |
| `MONEY_RECEIPTS`, `MONEY_RECEIPTS_FOLD_AT`, `MONEY_RECEIPT_MAX_AGE_HOURS`, `MONEY_RECEIPTS_SILENT_AT_NIGHT` | `each` / 4 / 12 h / `false` — a receipt with 🗑 O'chir per booked payment within the minute, folded for bursts, one summary for a first import, held through quiet hours |
| `MONEY_TYPED_MATCH_HOURS`, `MONEY_RECEIPT_ON_TYPED_MATCH` | 6 / `false` — a payment you typed and the bank's record of the same amount on the same day within 6 h are booked once (a ➕ button splits them); optionally a receipt when the bank confirms |
| `RECAP_MAX_PARTS` | 4 — a long evening report is split into at most this many messages, never clipped |
| `EXTRACT_MODEL_PRICE`, `REASON_MODEL_PRICE` | Blank — "input,output" USD per million tokens for the two model roles; set them when you change a model, then `make reprice SINCE=…` |
| `SPEND_ALERT_DAILY_USD`, `SPEND_ALERT_MONTHLY_USD` | 5 / 60 — a Telegram warning when MIYA's own API spend passes either (0 = off); the hard cap is the Anthropic Console limit |
| `QUESTION_BUDGET_PER_DAY`, `QUESTION_BRIEF_SLOTS`, `QUESTION_EVENING_SLOTS` | 10 / 5 / 3 — how many taps a day MIYA may ask for, and how many ride the brief and the evening report (`0` = never push) |
| `QUESTION_BATCH_MAX`, `QUESTION_PUSH_GAP_MINUTES`, `QUESTION_URGENT_MIN_UZS` | 5 / 120 / 5 mln — questions per pushed message, the gap between pushes, the stake that moves a question up |
| `QUESTION_GROUP_DIGEST_SIZE`, `QUESTION_GROUP_MAX_SHOWS`, `QUESTION_GROUP_MIN_MESSAGES`, `QUESTION_ASK_CHANNELS` | 3 / 2 / 1 / `false` — the new-groups digest |
| `CLAIM_ASK_AFTER_MINUTES`, `CLAIM_DUPLICATE_DAYS`, `CLAIM_BANK_MATCH_HOURS` | 10 / 14 / 48 — when an unshown claim is queued, when a repeat folds into the first, how far a bank payment can settle a claim |
| `CLAIM_BANK_AUTOCLOSE_TRANSACTIONS`, `CLAIM_BANK_AUTOACCEPT_SETTLEMENTS` | `true` / `false` — what bank evidence may settle without asking |
| `MEDIA_ASK_IN_GROUPS`, `MEDIA_ASK_OUTGOING`, `MEDIA_UNASKED_EXPIRY_DAYS` | `false` / `false` / 7 — which big files are asked about, and when an unasked question expires |

Credentials in `.env.example` are intentionally blank; blank integer keys
(`OWNER_TELEGRAM_ID`, `TELETHON_API_ID`) are treated as unset, not as `0`.

---

## Using the bot

Send the bot a note, a voice message or a receipt photo and it replies with what
it recorded:

> **you:** Akmal akaga 5 mln so'm berdim, 25-avgustgacha qaytaradi
> **MIYA:** 💰 Qarz: → senga 5 mln so'm, muddat: 25-avg
> 🤝 Va'da: U — 25-avgustda qaytaradi

| Command | What it answers |
|---|---|
| `/qarz` | Open balances, split into who owes you and who you owe |
| `/vada` | Open promises, split into yours and theirs |
| `/bugun` | Today: money in and out, people spoken to, new debts and promises |
| `/pul [kecha\|YYYY-MM-DD]` | One day's money rows by ref (`x12`), each with ✏️ Tuzat and 🗑 O'chir; voided rows marked 🗑 |
| `/ochir x12` | Void one wrong money row: it leaves every total, stays in the history, and ↩️ Qaytar (or `/qaytar x12`) restores it |
| `/tuzat x12 …` | Correct a money row: amount, currency, `kirim`/`chiqim`/`teskari`, `kim <ism>`, `sana kecha`, `izoh …`, `turkum …` |
| `/kim <ism yoki GS kod>` | One person: who they are, the written profile, balances, promises, remembered facts, recent contact |
| `/tarix <ism> [N]` | A person's full contact history, oldest to newest |
| `/eslab <ism>: <matn>` | Remember a fact about a person by hand |
| `/kod <ism> <GS kod>` | Attach, show (`/kod GS367`), move or detach (`/kod GS367 o'chir`) a client code |
| `/kodlar` | Code suggestions learned from chats; send a CSV/.xlsx captioned `/kodlar` to import the client list |
| `/yuk <YW26-004715 yoki GS367>` | Every message, call and note that mentions a waybill or client code, oldest first; a bare code or waybill sent as a message does the same |
| `/qidir <so'z>` | Semantic search over long-term memory (bge-m3 → pgvector) |
| `/hisobot` | Generate and send today's report right now |
| `/reja` | Tomorrow's time-blocked plan |
| `/chats` | Which Telegram chats the userbot reads, with per-chat toggles |
| `/process` | Reply to a video/document/voice to process it on demand |
| `/xarajat` | What MIYA's own API calls cost this month |
| `/unut` | Delete a person, a chat or a date range — asks first |
| `/tekshir` | Inputs whose processing failed and needs the owner's eye |
| `/savollar` | Every question waiting for the owner's tap (claims, money texts, missed calls, groups, files), ranked money first and paged; answering here spends none of the day's 5–10 asked questions |
| `/holat` | MIYA's own health: each process, the database, disk, the last backup, Anthropic, the queue and this month's spend — with what to type for anything wrong |
| `/yordam` | The command list |

Free-form **questions** (ending in `?` or starting with an interrogative like
*qancha*, *kim*, *сколько*) are answered instead of logged. Money and debt
figures in those answers always come from SQL tools — Sonnet only phrases the
result; it is instructed and structurally unable to invent a number that is
not in a tool result.

Reminders arrive on the hour for anything due today, tomorrow, or already
overdue — once per item per 24 hours, and never inside `QUIET_HOURS`. The
daily report lands at `REPORT_TIME` (19:00 Tashkent = 22:00 in China) with the
day's money, people, debts, completions and tomorrow's plan.

---

## How extraction works

1. Text, a Scribe transcript and any vision output are concatenated into one
   block (`miya/services/ingest.py`).
2. That block goes to Claude Haiku with a **structured output schema** derived
   from the Pydantic model in `miya/services/extraction.py`, so the API itself
   guarantees the response validates. The static system prompt sits behind a
   cache breakpoint; `CURRENT_DATE` and the message go in the user turn, where
   they cannot invalidate the cached prefix.
3. Transport errors, refusals and truncation get one retry with a
   "valid JSON only" nudge. If that also fails, the interaction is flagged
   `needs_review` and kept — **the raw input is never lost**.
4. Names resolve through rapidfuzz against display names and aliases at
   threshold 85, with honorifics ("aka", "opa") stripped first. A matched
   person learns the new spelling as an alias.
5. Debts, promises, transactions, events and tasks are written, each linked to
   the interaction that produced it. Repayments pay down that person's open
   debts **on the side they were made on**, in the same currency, oldest due
   date first. With a regular supplier the owner is often owed and owing at
   once, so "Akmal 5 mln qaytardi" must not touch what the owner owes Akmal.
   When the text genuinely does not say who paid whom and both sides are open,
   nothing is paid — MIYA asks instead, because a wrong balance is worse than
   a missing one.
6. Facts are stored in `memories` without an embedding; a worker job backfills
   them with bge-m3 within a couple of minutes.

> **Prompt caching does not engage yet.** Claude Haiku 4.5 has a 4096-token
> minimum cacheable prefix and the extraction system prompt is roughly 400
> tokens, so the breakpoint is a no-op today — caching is silently skipped
> rather than reported as an error. The cost model in the spec assumes it will
> help; it will only do so once the prompt grows (few-shot examples, a person
> glossary) past that floor. `usage_log` records `cache_read_tokens` on every
> call, so the moment it starts working the numbers will show it.

---

## Call recordings (Syncthing)

The phone's call recordings reach the VPS through Syncthing — device-authenticated,
end-to-end encrypted sync; no cloud in between.

**One-time pairing:**

1. `make up` starts the `syncthing` container. Open its UI over an SSH tunnel:
   `ssh -L 8384:127.0.0.1:8384 vps` → http://127.0.0.1:8384. Set a UI password
   immediately (Actions → Settings → GUI).
2. Install Syncthing on the phone and add the VPS as a remote device
   (Actions → Show ID on the VPS side; scan the QR from the phone).
   ⚠️ The original Syncthing Android app was discontinued in late 2024 — use
   the maintained community fork **Syncthing-Fork** (F-Droid, or Google Play
   where available).
3. On the phone, share the call-recordings folder (on Samsung usually
   `Internal storage/Recordings/Call`) with the VPS device.
4. Accept the share in the VPS UI and point it at `/var/syncthing/call_recordings`,
   **receive-only** — the VPS must never push deletions back to the phone.

**What happens next:** the worker sweeps the folder every minute. A file counts
as ready when it has an audio extension, is non-empty, is not a Syncthing temp
file, and has not been modified for 30 seconds. Each ready file is hashed
(SHA-256) and skipped if that hash was ever ingested — so re-syncs, renames and
conflict copies never double-bill Scribe. The filename is parsed defensively
(`Call recording <name-or-number>_YYMMDD_HHMMSS.m4a`); whatever fails to parse
falls back to the file's mtime and an unlinked person. A phone number in the
name is matched against `people.phone` on the last 9 digits, so `+998 90
123-45-67` and `901234567` agree. The audio then flows through the same
pipeline as a voice note: Scribe transcript → Haiku extraction → rows. When a
call produces something concrete (a debt, a promise, a transaction) the bot
sends a short summary; routine calls land silently and appear in the daily
report.

A transcription failure marks the interaction `needs_review` and keeps the hash
row, so a broken file is not retried (and re-billed) every minute. The nightly
retention job (04:15) deletes audio older than `AUDIO_RETENTION_DAYS` from both
the recordings share and the bot-media folder — interactions and transcripts
outlive the audio.

---

## Memory, reports and calendar

**Long-term memory.** Extraction's `facts` land in `memories` with a NULL
embedding; a worker job embeds them with bge-m3 (1024-dim, multilingual
uz/ru/zh) every two minutes and pgvector's HNSW index serves cosine search.
Only the api container loads the model — the bot and worker call its
`POST /v1/embed` over the compose network (`EMBED_SERVICE_URL`), so the ~2 GB
of weights sit in RAM once, not three times.

**RAG chat.** A question to the bot goes to Sonnet with a fixed toolbox:
`open_debts`, `person_summary`, `spending_summary`, `due_items`,
`upcoming_events`, `search_memories`, `recent_interactions`. The SQL tools are
the only source of financial figures; `search_memories` covers contextual
questions. If the model or a tool fails, the owner gets an honest "try again
later" instead of a guess.

**Daily report.** At `REPORT_TIME` the worker gathers the day from SQL and
renders the report deterministically, with Uzbek section headings; no model
touches it, so no figure can be mis-copied on the owner's evening money
surface. The result is stored in `daily_reports` (upsert per date) and sent
to the owner. `/hisobot` runs the same path on demand. Its "📅 Ertaga" section
is tomorrow's SQL listing; `/reja` is the only model-written plan, and the
model writing it never sees an amount (debts are named by person and ref).

**Google Calendar.** One-time auth: create an OAuth *Desktop app* client in
Google Cloud Console, save it to `secrets/google_oauth.json`, then

```bash
ssh -L 8765:127.0.0.1:8765 <vps>     # keep open during the flow
make gcal-auth                        # prints a URL — open it locally
```

After the token exists the worker pulls the next `GCAL_DAYS_AHEAD` days every
`GCAL_PULL_MINUTES` (upsert by `gcal_event_id` — re-pulls update, never
duplicate) and pushes extracted events that have a concrete future time,
marked `[MIYA]` in the description. Date-only events (midnight) stay local so
the calendar isn't spammed with 00:00 entries. Without the token both jobs are
quiet no-ops.

---

## The Telegram userbot

The userbot reads the owner's **personal** Telegram account with Telethon so
that business conversations become debts, promises and facts without anyone
re-typing them. It is passive by construction:

* It never sends, edits, deletes, forwards or marks anything as read. A test
  parses the package and fails the build if a writing call ever appears.
* It downloads no history. Only messages that arrive after it starts are read;
  the dialog list is used for chat titles only.
* `USERBOT_ENABLED=false` turns the whole thing off in one flag.
* In a group that is switched off and not yet decided, it keeps only a
  message counter and two timestamps (when the owner last wrote there, when
  someone addressed him) — never the text, the sender or any media — so the
  daily "Yangi guruhlar" digest can offer the active groups first.
* The Telethon session string is a **full credential** for the account. Keep it
  in `.env`, and revoke it from Telegram → Settings → Devices if it leaks.

Automating a personal account technically violates Telegram's Terms of
Service. The design above is what keeps the risk minimal, but the decision to
run it is the owner's.

**Setup**

```bash
# 1. Get api_id / api_hash from https://my.telegram.org → API development tools
# 2. Log in once (asks for phone, code, 2FA password):
make userbot-login          # prints TELETHON_SESSION=… for .env
# 3. Restart and pick which chats are read:
make up && make userbot     # then send /chats to the assistant bot
```

`/chats` lists every known chat with three toggles: read this chat, run vision
on its photos, read its documents. Private chats start on, groups and channels
start off — exactly the spec's default, and the same screen doubles as the DM
exclude list.

**From messages to facts.** Each monitored message is stored on its own, then a
worker job groups a chat's messages into a **conversation window** — flushed
after 30 minutes of silence, 25 messages, or 4,000 characters, whichever comes
first. Windows are rendered as `[ME]` / `[THEM (Name)]` transcripts and
extracted through the **Message Batches API at half price**, which is why they
are not instant: a window submitted at 14:00 typically lands within the hour.
Buffering happens in the database, so a restart never loses a message, and a
window that keeps failing in batches is retried at full price rather than lost.

**Media**, per chat settings: voice messages and video notes are always
transcribed (ffmpeg pulls the audio out of a video note first); photos are only
sent to vision where `vision_enabled` is on; documents (`pdf`, `xlsx`, `docx`,
`txt`, `csv`) are parsed **locally on the VPS** and capped at 15,000
characters; videos are stored but not processed until the owner replies
`/process`; stickers and GIFs are dropped entirely.

---

## Data model

Thirteen tables (`miya/db/models.py`, migrations under `alembic/versions/`):

* **`people`** — display name plus an `aliases` array, so "Akmal aka" and
  "Akmal GZ" resolve to one person. GIN-indexed for fuzzy matching.
* **`interactions`** — every input lands here first, processed or not. Media
  lives in a JSONB blob; `needs_review` flags extractions that failed validation
  so **data is never silently dropped**.
* **`debts` / `debt_payments`** — a debt's balance is `amount` minus its
  payments, per currency. `CHECK (amount > 0)` on both.
* **`promises`, `transactions`, `events`, `tasks`** — the rest of the extraction
  output, each linked to the interaction that produced it.
* **`memories`** — RAG store, `vector(1024)` for bge-m3, HNSW index with
  `vector_cosine_ops`.
* **`daily_reports`**, **`usage_log`** — report archive and per-call API cost
  accounting (feeds `/xarajat` in Phase 5).
* **`reminder_log`** — one row per reminder sent, so nothing is pinged twice
  within 24 hours.

Invariants enforced by the schema and covered by tests:

* Money is `NUMERIC(14,2)` with a `currency` enum — never a float.
* Every timestamp is `timestamptz`; the app timezone is Asia/Tashkent.
* Everything derived from an interaction has
  `source_interaction_id … ON DELETE CASCADE`, so purging one interaction
  erases what it produced (the `/unut` command in Phase 5).

---

## Internal API

Bound to `127.0.0.1`, bearer-token authenticated, and deliberately thin: every
route reads through the same services the bot does, so an HTTP client and
Telegram can never report different balances.

| Route | What it does |
|---|---|
| `GET /health` | Liveness + database reachability (public, for healthchecks) |
| `GET /v1/config` | Effective non-secret configuration |
| `GET /v1/debts` | Open balances, filterable by `direction` and `person_id` |
| `POST /v1/debts/{id}/settle` | Record a payment against one debt |
| `GET /v1/promises` | Open promises |
| `GET /v1/transactions` | Income/expense totals over `date_from`…`date_to` |
| `GET /v1/people/{id}/summary` | One person's balances, promises and contact |
| `POST /v1/ask` | RAG answer (same SQL-first path as the bot) |
| `POST /v1/report/today` | Generate today's report (not stored) |
| `GET /v1/plan/tomorrow` | Tomorrow's plan |
| `GET /v1/usage` | API spend over a range |
| `POST /v1/embed` | Embedding service for the bot and worker |

---

## Security

* **The API is bound to `127.0.0.1`.** So is Postgres. Nothing but Syncthing's
  sync port is reachable from outside the VPS.
* **Bearer token on every `/v1/*` route**, compared in constant time. `/health`
  is deliberately public so the container healthcheck can poll it.
* **The container runs as a non-root user** (uid 10001).
* **Secrets never enter git.** `.env`, `secrets/` and `data/` are gitignored;
  only `.env.example` is committed.
* **The Telethon session string is a credential** — it grants full access to the
  owner's Telegram account. Keep it in `.env` or an encrypted file, never in the
  repository.
* **Nightly backups are encrypted before they touch the disk.** `pg_dump -Fc`
  (compressed custom format) is piped straight into `age`; the plaintext
  exists only inside that pipe. With `BACKUP_AGE_RECIPIENT` unset the job
  writes nothing at all — an unencrypted dump of every debt and transcript is
  not an acceptable fallback. Backups are kept for `BACKUP_RETENTION_DAYS`
  (14), the nightly file is also sent to the owner's Telegram as a document
  (`BACKUP_TO_TELEGRAM`, in ≤ 45 MB pieces when large — still ciphertext),
  and a failing or unsent backup pings the owner. See *Backups and restore*
  below.

* **`/unut` really deletes.** It shows exactly what would go — interactions,
  debts, promises, transactions, events, tasks, memories and media files — and
  only acts after the owner confirms. Cascades do the work, so nothing is left
  orphaned, and the audio and photos are unlinked from disk in the same pass.

### Backups and restore

**The key.** Backups are locked with an `age` key pair. The *public* key is
`BACKUP_AGE_RECIPIENT` in `.env`; the *private* key is one line in
`secrets/backup-key.txt`. Make it once and copy that file somewhere that is
not this server — a password manager entry, a USB stick in a drawer, both:

```bash
make backup-key        # creates secrets/backup-key.txt, prints BACKUP_AGE_RECIPIENT=… for .env
make backup-key-show   # prints the secret key: store it off this server
make backup            # write one now, do not wait for 03:30
```

`make backup-key` refuses to overwrite an existing key, because a new key
cannot open the old backups.

**Losing the key means losing every backup.** Nobody — not Telegram, not
the person who wrote this — can open a `.dump.age` file without it. The
files in Telegram and in `data/backups/` are only as safe as that one line.

**What you have.** Every night at `BACKUP_TIME` the worker writes
`data/backups/miya-<date>-<time>.dump.age` and sends the same file to your
Telegram (as pieces `….dump.age.part01`, `.part02`, … when it is over 45 MB).
`/holat` shows the newest one and whether it reached Telegram. Files from
before this format, `….sql.age`, still open with
`age -d -i secrets/backup-key.txt miya-….sql.age | psql "$DATABASE_URL"`.

**Restoring, the three commands.** On a fresh server: clone the repo, copy
`.env` and `secrets/backup-key.txt` back into place, put the backup file
(or all of its pieces, from Telegram) into `data/backups/`, then:

```bash
docker compose up -d db                                               # 1. the database only
make restore FILE=/data/backups/miya-20260915-033000.dump.age DRY=1   # 2. look inside first
make restore FILE=/data/backups/miya-20260915-033000.dump.age         # 3. load it
make up                                                               # 4. migrate forward, start everything
```

Only the database runs while the backup loads: the bot, worker and userbot
would otherwise hold locks on the tables being replaced and write rows of
their own. On a server that is already running, `docker compose stop bot
worker userbot api` first and `make up` afterwards. For a split backup name
any one piece, e.g. `….dump.age.part01`; the others are found next to it.
The restore refuses a database that already holds people or debts; add
`FORCE=1` to replace what is there. `make up` at the end brings an older
backup up to the current schema.
The decrypted dump exists only in a temporary file that is deleted when the
command ends, success or not.

### Documented egress

Four external services receive data, plus one optional bare ping. Nothing
else leaves the VPS.

| Destination | What is sent | Why |
|---|---|---|
| **Anthropic API** | Message text, call transcripts, document text, receipt images | Extraction, daily report, planner, RAG answers |
| **ElevenLabs Scribe** | Audio files (voice notes, call recordings) | Transcription |
| **Google Calendar API** | Event titles, times, locations, attendees | Calendar pull and push |
| **Telegram Bot API** | The bot's replies to the owner, and the nightly backup as an `age`-encrypted document (ciphertext only) | The assistant channel; an off-server copy of the backup |

| **`DEADMAN_PING_URL`** (optional) | A bare GET every `DEADMAN_PING_MINUTES`, no data | An outside watcher notices a dead server |

Embeddings run locally on the VPS CPU (`BAAI/bge-m3`) — no egress.

Anthropic does not train on API data by default, but **text does leave the
server**. Same for audio sent to ElevenLabs. Any new egress must be added to
this table in the same change that introduces it.

### Legal note

Recording your own calls for personal use is generally acceptable, but rules on
the other party's consent vary by jurisdiction. Complying with the law where the
owner and the other party are located is the owner's responsibility.

The Telegram userbot automates a personal account, which technically
violates Telegram's Terms of Service. It stays strictly passive — it never sends
messages, never marks chats as read, and never bulk-downloads history — and
`USERBOT_ENABLED=false` disables it entirely.

---

## Build phases

| Phase | Scope | Status |
|---|---|---|
| **0** | Repo, Compose, schema + migration, health API, Makefile, README | ✅ done |
| **1** | Assistant bot, Scribe, Haiku extraction, person resolution, `/qarz` `/vada` `/bugun` `/kim`, reminders | ✅ done |
| **2** | Syncthing share + folder watcher → phone-call interactions | ✅ done |
| **3** | bge-m3 memories, RAG chat (SQL-first for money), daily report, planner, Google Calendar | ✅ done |
| **4** | Telethon userbot, `/chats`, conversation windowing, media policy, Batch API | ✅ done |
| **5** | `/xarajat`, `/unut` purge tooling, chat backfill, encrypted backups | ✅ done |

Dependencies are added per phase rather than up front. Phase 0 installed
FastAPI, SQLAlchemy, Alembic, psycopg, pgvector and APScheduler; Phase 1 added
`anthropic`, `aiogram` and `rapidfuzz`; Phase 3 added `sentence-transformers`
(and with it torch, CPU-only) plus the Google Calendar client libraries; Phase
4 added `Telethon` and the document parsers (`pdfplumber`, `openpyxl`,
`python-docx`).

---

## Cost target

Every API call is logged to `usage_log`, so the real figure is measured, not
assumed: `/xarajat` shows this month's spend per operation. The largest item
used to be the person profiles; they are now written by `PROFILE_MODEL`
(blank = `EXTRACT_MODEL`), at most once per `PROFILE_MIN_AGE_HOURS` per
person and at most `PROFILE_DAILY_CAP` a day, and appear in `/xarajat` as
«odam haqida profil». Extraction uses prompt caching on the static system
prompt and the Batch API for the userbot stream; embeddings run locally.

---

## Verification

315 tests against PostgreSQL 16.14 with pgvector 0.6.0. The Anthropic,
ElevenLabs, Google and Telegram clients are stubbed throughout (the embedder
too), so the suite is free and offline.

**Schema and migrations**
* `upgrade head` → `downgrade base` → `upgrade head` round-trips cleanly, and
  `alembic check` reports no drift between the models and the migrations.
* Enum labels land as written (`direction` is `in`, not `in_`); money
  round-trips as an exact `Decimal`; HNSW cosine search returns the expected
  row; deleting an interaction cascades to every derived row.

**Extraction**
* Amounts convert to exact `Decimal` through `str`, never a binary float.
* Unparseable dates are dropped rather than failing the batch.
* `CURRENT_DATE` is in the user turn, not the cached system prompt.
* A transport error retries once with a JSON nudge; two failures surface as an
  error and flag `needs_review`; a refusal does not retry.

**Money**
* Balances are `amount` minus payments, per currency, and a settled debt leaves
  the open list.
* A repayment settles the side it was made on; with debts open both ways and
  no stated direction it settles nothing and asks the owner.
* A partial repayment marks a debt `partially_paid`, a full one settles it, and
  a repayment spanning several debts is applied oldest due date first.
* A repayment with no matching debt is surfaced to the owner, not discarded.
* Repayments never cross currencies.

**Bot**
* An unset `OWNER_TELEGRAM_ID` locks everyone out rather than opening the bot.
* A contact name containing HTML is escaped before it reaches Telegram.
* Uzbek money and date formatting: `5 mln so'm`, `500 ming so'm`, `$300`,
  `25-avg`, `3 kun kechikdi`.

**Reminders**
* Quiet hours wrap midnight correctly (23:30–07:30).
* The same item is not pinged twice within 24 hours, and returns after.
* A ping too long for Telegram marks only the part that fitted as sent, so the
  overflow arrives next hour instead of being suppressed for 24 hours and
  starving forever. Events are rendered first, since a meeting inside the hour
  gets no second chance.

**API**
* `/health` returns `ok`/200 with a database and `degraded`/503 without one;
  every `/v1/*` route returns 401 without the bearer token.
* The HTTP layer and the bot read the same services, and a test asserts they
  report byte-identical balances — a debt settled over HTTP disappears from
  the query `/qarz` uses.

**Memory and RAG (Phase 3)**
* NULL-embedding memories are backfilled in batches; pgvector ranks a pinned
  query vector's neighbours in the expected order.
* A money question routes through the SQL tool and the exact figure is in the
  model's context before it answers; an unknown person or a broken tool comes
  back as an error the model can phrase, never a crash.
* Question routing: `?` or a leading interrogative goes to RAG; question words
  mid-sentence stay log entries.
* The daily report upserts by date and makes no model call, so an Anthropic
  outage cannot cost the owner the day's report.

**Google Calendar (Phase 3)**
* Pulls upsert by `gcal_event_id` — re-pulls change nothing, edits update the
  row in place, and a pushed event is never re-imported as a duplicate.
* Pushes carry the `[MIYA]` marker and Tashkent wall-clock times, skip
  date-only and past events, and a failed insert stays queued for retry.

**Userbot, windows and batches (Phase 4)**
* The whole chain is walked end to end against the real schema: stored
  messages → window → Batch API → a debt, a promise and a memory on the right
  person, then a chat purge that leaves nothing behind.
* An interrupted result stream is replayed on the next poll without
  re-applying what already landed.
* The userbot package is parsed by a test that fails if any Telegram-writing
  call (`send_message`, `send_read_acknowledge`, `iter_messages`, …) appears.
* `USERBOT_ENABLED=false` returns before a client is even constructed; a
  missing session exits instead of prompting on stdin.
* Each of the three window triggers is exercised separately, a long backlog
  becomes several windows in one pass, and a claimed message is never
  windowed twice.
* Batch results land debts through a synthetic window interaction at half
  price; an errored, expired, missing or unparseable result is retried, and
  only after `BATCH_MAX_ATTEMPTS` does it fall back to a real-time call — a
  window that fails everywhere still keeps its text and shows up in
  `/tekshir`.
* The media policy is table-tested per media type against both chat settings,
  and every document parser runs against a real file. A downloaded document
  keeps its real extension, without which nothing could parse it, and one that
  still fails to parse is flagged for `/tekshir` rather than silently dropped.

**Cost, purge and backups (Phase 5)**
* `/xarajat` groups real `usage_log` rows by operation and reports fractions
  of a cent, which `money()` would have rounded away to `$0`.
* Names and descriptions from Telegram are HTML-escaped before they reach the
  report or the plan, and every worker notification retries as plain text — a
  contact who renames himself "<b" cannot silence the evening summary.
* Search results reaching the RAG loop are labelled as other people's words,
  so a supplier cannot smuggle instructions or a balance claim into an answer.
* A purge plan counts exactly what would go before anything is deleted;
  executing removes the person, their debts and promises, every derived row,
  the media files **and the conversation windows** — while leaving other
  people untouched. A surviving window would have been re-extracted hours
  later and recreated exactly what the owner asked to forget.
* The backup test does the full round trip where `age` and `pg_dump` exist:
  dump → encrypt → decrypt → a dump containing `CREATE TABLE public.debts`,
  written `0600`. A failing dump leaves no `.partial` file behind, and
  pruning only ever touches MIYA's own timestamped backups.
