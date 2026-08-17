# MIYA — Personal AI Second Brain

Self-hosted, single-user life-logging system. Captures the owner's day from
Telegram, phone calls and notes; extracts structured facts (debts, promises,
transactions, events, tasks) with Claude; and answers questions, reminds, and
reports back in Uzbek.

Owner-facing language is Uzbek. Code, comments and commits are English.

**Status: Phase 0 (skeleton) complete.** The database schema, the internal API,
Docker Compose and migrations are in place and verified. Nothing ingests data
yet — that is Phase 1.

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
make up                   # builds, starts db + api, applies migrations
make health               # {"status":"ok", ...}
```

`make help` lists every target.

| Target | What it does |
|---|---|
| `make up` | Build and start `db` + `api`, then migrate |
| `make down` | Stop everything (the database volume is kept) |
| `make migrate` | Apply migrations |
| `make revision m="…"` | Autogenerate a migration from the models |
| `make psql` | Open a psql shell |
| `make logs` / `make ps` | Tail logs / show container status |
| `make bot` / `make worker` | Assistant bot / scheduler (Phase 1) |
| `make test` / `make lint` / `make fmt` | Local dev loop |

### Local development without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make test          # schema + API tests; DB tests skip if no database
```

The database tests in `tests/test_db_integration.py` skip automatically unless
`DATABASE_URL` points at a migrated PostgreSQL with the `vector` extension.

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

## Data model

Twelve tables (`miya/db/models.py`, migration `alembic/versions/0001_initial_schema.py`):

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
| **1** | Assistant bot, Scribe, Haiku extraction, person resolution, `/qarz` `/vada` `/bugun` `/kim`, reminders | next |
| **2** | Syncthing share + folder watcher → phone-call interactions | |
| **3** | bge-m3 memories, RAG chat (SQL-first for money), daily report, planner, Google Calendar | |
| **4** | Telethon userbot, `/chats`, conversation windowing, media policy, Batch API | |
| **5** | `/xarajat`, purge tooling, backfill, retention jobs | |

Dependencies are added per phase rather than up front — Phase 0 installs only
FastAPI, SQLAlchemy, Alembic, psycopg, pgvector and APScheduler.

---

## Cost target

~$25–45/month at the expected volume (~50 calls/day plus Telegram): ~$7
transcription, ~$10–20 Haiku extraction (Batch API at −50% for the userbot
stream, prompt caching on the static system prompt), ~$5–15 Sonnet reasoning,
$0 embeddings. Every API call is logged to `usage_log` so the real figure is
measured, not assumed.

---

## Verification (Phase 0)

Against PostgreSQL 16.14 with pgvector 0.6.0:

* `alembic upgrade head` → `downgrade base` → `upgrade head` round-trips cleanly.
* `alembic check` reports no drift between the models and the migration.
* Enum labels land as written (`direction` is `in`, not `in_`).
* Money round-trips as an exact `Decimal`; HNSW cosine search returns the
  expected row; deleting an interaction cascades to every derived row.
* `/health` returns `ok`/200 with a database and `degraded`/503 without one;
  `/v1/config` returns 401 without the bearer token.
