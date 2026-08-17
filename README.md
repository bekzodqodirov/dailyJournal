# MIYA — Personal AI Second Brain

Self-hosted, single-user life-logging system. Captures the owner's day from
Telegram, phone calls and notes; extracts structured facts (debts, promises,
transactions, events, tasks) with Claude; and answers questions, reminds, and
reports back in Uzbek.

Owner-facing language is Uzbek. Code, comments and commits are English.

**Status: Phase 1 complete.** The assistant bot captures text, voice and photos;
Claude Haiku extracts structured facts; debts, promises, transactions, events and
tasks are persisted and queryable; due reminders go out hourly. Call recordings
and the passive Telegram reader are Phases 2 and 4.

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
│ people · chat_monitors · interactions · debts · debt_payments ·     │
│ promises · transactions · events · tasks · memories · daily_reports │
│ · usage_log                                                         │
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

Requires Docker with Compose v2.

```bash
cp .env.example .env      # then fill it in — see "Configuration" below
make up                   # builds, starts everything, applies migrations
make health               # {"status":"ok", ...}
make bot                  # tail the assistant bot's logs
```

Before `make up`, fill in at least `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`,
`ASSISTANT_BOT_TOKEN`, `OWNER_TELEGRAM_ID` and `API_BEARER_TOKEN`. The bot and
worker refuse to start without a bot token and owner id rather than run
unrestricted.

`make help` lists every target.

| Target | What it does |
|---|---|
| `make up` | Build and start db, api, bot and worker, then migrate |
| `make down` | Stop everything (the database volume is kept) |
| `make migrate` | Apply migrations |
| `make revision m="…"` | Autogenerate a migration from the models |
| `make psql` | Open a psql shell |
| `make logs` / `make ps` | Tail logs / show container status |
| `make bot` / `make worker` | Tail the assistant bot / scheduler logs |
| `make test` / `make lint` / `make fmt` | Local dev loop |

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
| `ANTHROPIC_API_KEY` | Required from Phase 1 |
| `EXTRACT_MODEL` | `claude-haiku-4-5` — the extraction engine |
| `REASON_MODEL` | `claude-sonnet-5` — daily report, planner, RAG answers |
| `ELEVENLABS_API_KEY` | Scribe transcription (uz/ru) |
| `TRANSCRIBER` | `elevenlabs`; a local Whisper backend can be swapped in later |
| `ASSISTANT_BOT_TOKEN`, `OWNER_TELEGRAM_ID` | The bot rejects every other user |
| `USERBOT_ENABLED` | One-flag kill switch for the passive Telegram reader |
| `API_BEARER_TOKEN` | `openssl rand -hex 32` — required for every `/v1/*` route |
| `TIMEZONE`, `REPORT_TIME`, `QUIET_HOURS` | Asia/Tashkent, 19:00, 23:30–07:30 |

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
| `/kim <ism>` | One person: balances, promises, last contact |
| `/yordam` | The command list |

Reminders arrive on the hour for anything due today, tomorrow, or already
overdue — once per item per 24 hours, and never inside `QUIET_HOURS`.

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
   debts in the same currency, oldest due date first.
6. Facts are stored in `memories` without an embedding; Phase 3 backfills them
   with bge-m3.

> **Prompt caching does not engage yet.** Claude Haiku 4.5 has a 4096-token
> minimum cacheable prefix and the extraction system prompt is roughly 400
> tokens, so the breakpoint is a no-op today — caching is silently skipped
> rather than reported as an error. The cost model in the spec assumes it will
> help; it will only do so once the prompt grows (few-shot examples, a person
> glossary) past that floor. `usage_log` records `cache_read_tokens` on every
> call, so the moment it starts working the numbers will show it.

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

### Documented egress

Three external services receive data. Nothing else leaves the VPS.

| Destination | What is sent | Why |
|---|---|---|
| **Anthropic API** | Message text, call transcripts, document text, receipt images | Extraction, daily report, planner, RAG answers |
| **ElevenLabs Scribe** | Audio files (voice notes, call recordings) | Transcription |
| **Google Calendar API** | Event titles, times, locations, attendees | Calendar pull and push |

Embeddings run locally on the VPS CPU (`BAAI/bge-m3`) — no egress.

Anthropic does not train on API data by default, but **text does leave the
server**. Same for audio sent to ElevenLabs. Any new egress must be added to
this table in the same change that introduces it.

### Legal note

Recording your own calls for personal use is generally acceptable, but rules on
the other party's consent vary by jurisdiction. Complying with the law where the
owner and the other party are located is the owner's responsibility.

The Telegram userbot (Phase 4) automates a personal account, which technically
violates Telegram's Terms of Service. It stays strictly passive — it never sends
messages, never marks chats as read, and never bulk-downloads history — and
`USERBOT_ENABLED=false` disables it entirely.

---

## Build phases

| Phase | Scope | Status |
|---|---|---|
| **0** | Repo, Compose, schema + migration, health API, Makefile, README | ✅ done |
| **1** | Assistant bot, Scribe, Haiku extraction, person resolution, `/qarz` `/vada` `/bugun` `/kim`, reminders | ✅ done |
| **2** | Syncthing share + folder watcher → phone-call interactions | next |
| **3** | bge-m3 memories, RAG chat (SQL-first for money), daily report, planner, Google Calendar | |
| **4** | Telethon userbot, `/chats`, conversation windowing, media policy, Batch API | |
| **5** | `/xarajat`, purge tooling, backfill, retention jobs | |

Dependencies are added per phase rather than up front. Phase 0 installed
FastAPI, SQLAlchemy, Alembic, psycopg, pgvector and APScheduler; Phase 1 added
`anthropic`, `aiogram` and `rapidfuzz`.

---

## Cost target

~$25–45/month at the expected volume (~50 calls/day plus Telegram): ~$7
transcription, ~$10–20 Haiku extraction (Batch API at −50% for the userbot
stream, prompt caching on the static system prompt), ~$5–15 Sonnet reasoning,
$0 embeddings. Every API call is logged to `usage_log` so the real figure is
measured, not assumed.

---

## Verification

125 tests against PostgreSQL 16.14 with pgvector 0.6.0. The Anthropic and
ElevenLabs clients are stubbed throughout, so the suite is free and offline.

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

**API**
* `/health` returns `ok`/200 with a database and `degraded`/503 without one;
  `/v1/config` returns 401 without the bearer token.
