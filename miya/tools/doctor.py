"""Preflight: check `.env` and the server before `make up`.

    make doctor        (runs this inside the api image, with .env injected)

It must NOT import ``miya.config``: a broken `.env` is exactly what it
diagnoses, and the settings module refuses to load one. Everything comes from
``os.environ`` (Compose passes `.env` through ``env_file``), ``/proc/meminfo``
and the disk. Neither `.env` nor `.env.example` exists inside the image, so
the expected keys are a constant here; a test keeps it equal to the keys of
`.env.example`.

Exit code 1 when anything is an error; warnings alone exit 0.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EXPECTED_ENV_KEYS: tuple[str, ...] = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "DATABASE_URL",
    "DB_PORT",
    "ANTHROPIC_API_KEY",
    "EXTRACT_MODEL",
    "REASON_MODEL",
    "EXTRACT_MODEL_PRICE",
    "REASON_MODEL_PRICE",
    "SPEND_ALERT_DAILY_USD",
    "SPEND_ALERT_MONTHLY_USD",
    "PROFILE_MODEL",
    "PROFILE_MIN_AGE_HOURS",
    "PROFILE_DAILY_CAP",
    "PROFILE_REFRESH_PER_RUN",
    "ELEVENLABS_API_KEY",
    "TRANSCRIBER",
    "EMBED_MODEL",
    "EMBED_DIM",
    "EMBED_SERVICE_URL",
    "EMBED_MAX_SEQ_LENGTH",
    "EMBED_BATCH_SIZE",
    "EMBED_STALE_MINUTES",
    "API_RESTART_ALERT_COUNT",
    "DEADMAN_PING_URL",
    "DEADMAN_PING_MINUTES",
    "ASSISTANT_BOT_TOKEN",
    "OWNER_TELEGRAM_ID",
    "USERBOT_ENABLED",
    "TELETHON_API_ID",
    "TELETHON_API_HASH",
    "TELETHON_SESSION",
    "OWNER_ALIASES",
    "OWNER_ALIAS_NAMESAKE_DAYS",
    "CLIENT_CODE_PREFIXES",
    "CLIENT_CODE_MAX_DIGITS",
    "CLIENT_CODE_STRIP_LEADING_ZEROS",
    "WAYBILL_PREFIXES",
    "USERBOT_CATCHUP_MINUTES",
    "USERBOT_CATCHUP_MAX_PER_CHAT",
    "USERBOT_CATCHUP_MAX_CHATS",
    "LOOP_QUESTION_HOURS",
    "LOOP_QUESTION_MAX_DAYS",
    "LOOP_UNDATED_DAYS",
    "LOOP_QUIET_DAYS",
    "MORNING_BRIEF_TIME",
    "WINDOW_IDLE_MINUTES",
    "WINDOW_IDLE_MINUTES_ADDRESSED",
    "WINDOW_MAX_MESSAGES",
    "WINDOW_MAX_CHARS",
    "BATCH_MAX_ATTEMPTS",
    "DOC_MAX_BYTES",
    "DOC_MAX_CHARS",
    "AUDIO_MAX_BYTES",
    "MEDIA_ASK_MAX_BYTES",
    "MEDIA_ASK_EXPIRY_HOURS",
    "SAFETY_EXTRA_SAFE_DOMAINS",
    "JARVIS_ENABLED",
    "JARVIS_MODE",
    "JARVIS_DRAFT_DAILY_CAP",
    "JARVIS_AUTO_DAILY_CAP",
    "JARVIS_PER_CHAT_DAILY_CAP",
    "JARVIS_SEND_MIN_GAP_SECONDS",
    "JARVIS_DRAFT_EXPIRY_HOURS",
    "JARVIS_DAILY_COST_CAP_USD",
    "STYLE_SAMPLE_SIZE",
    "STYLE_MIN_SAMPLES",
    "KB_MATCH_THRESHOLD",
    "JARVIS_DEBOUNCE_SECONDS",
    "JARVIS_OWNER_FIRST_SECONDS",
    "JARVIS_MAX_MESSAGE_AGE_SECONDS",
    "JARVIS_MAX_AI_TURNS",
    "JARVIS_HISTORY_TURNS",
    "JARVIS_TAKEOVER_MINUTES",
    "JARVIS_ESCALATION_SNOOZE_MINUTES",
    "JARVIS_SNOOZE_MAX",
    "JARVIS_ESCALATION_DAILY_CAP",
    "TELEGRAM_SEND_ENABLED",
    "USERBOT_FLOOD_SLEEP_SECONDS",
    "JARVIS_REPLY_DELAY_MIN_SECONDS",
    "JARVIS_REPLY_DELAY_MAX_SECONDS",
    "JARVIS_TYPING_CHARS_PER_SECOND",
    "JARVIS_TYPING_MAX_SECONDS",
    "JARVIS_GRADUATION_MIN_DAYS",
    "JARVIS_GRADUATION_MIN_DRAFTS",
    "JARVIS_GRADUATION_MIN_APPROVAL_PCT",
    "JARVIS_AUTO_IN_QUIET_HOURS",
    "GOOGLE_OAUTH_CLIENT_JSON",
    "GOOGLE_TOKEN_JSON",
    "GCAL_CALENDAR_ID",
    "GCAL_PULL_MINUTES",
    "GCAL_DAYS_AHEAD",
    "SHEETS_ENABLED",
    "SHEETS_SERVICE_ACCOUNT_JSON",
    "SHEETS_SPREADSHEET_ID",
    "SHEETS_SYNC_SECONDS",
    "CALL_RECORDINGS_DIR",
    "AUDIO_RETENTION_DAYS",
    "TRANSCRIBE_DIARIZE_CALLS",
    "MEDIA_RETENTION_DAYS",
    "VIDEO_RETENTION_DAYS",
    "LOOP_MISSED_CALL_MINUTES",
    "LOOP_MISSED_CALL_MAX_DAYS",
    "PAYMENT_SMS_SENDERS",
    "PAYMENT_ADVERTS_TO_REVIEW",
    "PAYMENT_APP_PACKAGES",
    "MONEY_AUTOBOOK",
    "PAYMENT_DEDUPE_WINDOW_MINUTES",
    "PAYMENT_REPEAT_SECONDS",
    "MONEY_RECEIPTS",
    "MONEY_GS_SETTLE_ASK",
    "PAYMENT_OWN_CARDS",
    "CODE_PLACEHOLDERS",
    "MONEY_RECEIPTS_FOLD_AT",
    "MONEY_RECEIPT_MAX_AGE_HOURS",
    "MONEY_RECEIPTS_SILENT_AT_NIGHT",
    "MONEY_TYPED_MATCH_HOURS",
    "RECEIPT_MATCH_MINUTES",
    "RECEIPT_BANK_WAIT_MINUTES",
    "RECEIPT_STALE_HOURS",
    "RECAP_MAX_PARTS",
    "RECAP_PHONE_STALE_HOURS",
    "RECAP_PROSE_ENABLED",
    "RECAP_MODEL",
    "RECAP_MAX_PEOPLE",
    "RECAP_MAX_GROUPS",
    "RECAP_SUBJECT_INPUT_CHARS",
    "RECAP_INPUT_MAX_CHARS",
    "RECAP_MAX_OUTPUT_TOKENS",
    "RECAP_MODEL_TIMEOUT_SECONDS",
    "RECAP_MORNING_TOP_PEOPLE",
    "PASSAGE_CHUNK_CHARS",
    "PASSAGE_CHUNK_OVERLAP",
    "PASSAGE_EMBED_MIN_CHARS",
    "PASSAGE_INDEX_BATCH",
    "PASSAGE_EMBED_BATCH",
    "RECALL_TOP_K",
    "RECALL_CANDIDATES",
    "RECALL_MIN_SIMILARITY",
    "RECALL_RECENCY_HALFLIFE_DAYS",
    "RECALL_RECENCY_WEIGHT",
    "RECALL_CONTEXT_LINES",
    "RAG_FOLLOWUP_MINUTES",
    "RAG_FOLLOWUP_PAIRS",
    "MONEY_RECEIPT_ON_TYPED_MATCH",
    "QUESTION_BUDGET_PER_DAY",
    "QUESTION_BRIEF_SLOTS",
    "QUESTION_EVENING_SLOTS",
    "QUESTION_BATCH_MAX",
    "QUESTION_PUSH_GAP_MINUTES",
    "QUESTION_URGENT_MIN_UZS",
    "QUESTION_GROUP_DIGEST_SIZE",
    "QUESTION_GROUP_MAX_SHOWS",
    "QUESTION_GROUP_MIN_MESSAGES",
    "QUESTION_ASK_CHANNELS",
    "CLAIM_ASK_AFTER_MINUTES",
    "CLAIM_DUPLICATE_DAYS",
    "CLAIM_BANK_MATCH_HOURS",
    "CLAIM_BANK_AUTOCLOSE_TRANSACTIONS",
    "CLAIM_BANK_AUTOACCEPT_SETTLEMENTS",
    "MEDIA_ASK_IN_GROUPS",
    "MEDIA_ASK_OUTGOING",
    "MEDIA_UNASKED_EXPIRY_DAYS",
    "BACKUP_DIR",
    "BACKUP_RETENTION_DAYS",
    "BACKUP_TIME",
    "BACKUP_AGE_RECIPIENT",
    "BACKUP_TO_TELEGRAM",
    "HEARTBEAT_STALE_MINUTES",
    "USERBOT_STALE_MINUTES",
    "DISK_MIN_FREE_GB",
    "BACKUP_MAX_AGE_HOURS",
    "ALERT_REPEAT_HOURS",
    "RESTORE_DRILL_ENABLED",
    "RESTORE_DRILL_DAY",
    "PHONE_SILENT_HOURS",
    "API_BEARER_TOKEN",
    "UPLOAD_TOKENS",
    "API_PORT",
    "API_MEM_LIMIT",
    "TIMEZONE",
    "REPORT_TIME",
    "QUIET_HOURS",
    "PROMISE_REMIND_LEAD_MINUTES",
    "BATCH_FLUSH_HOURS",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DEBUG",
)

# Where to get each required value, in the owner's words.
REQUIRED_HINTS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "console.anthropic.com → API Keys (MIYA uchun alohida kalit)",
    "ELEVENLABS_API_KEY": "elevenlabs.io → API Keys",
    "ASSISTANT_BOT_TOKEN": "Telegramda @BotFather → /newbot",
    "OWNER_TELEGRAM_ID": "Telegramda @userinfobot'ga yoz — raqamingni aytadi",
    "API_BEARER_TOKEN": "openssl rand -hex 32",
    "BACKUP_AGE_RECIPIENT": "make backup-key",
    "POSTGRES_PASSWORD": "openssl rand -hex 24",
}
TELETHON_HINTS: dict[str, str] = {
    "TELETHON_API_ID": "my.telegram.org → API development tools",
    "TELETHON_API_HASH": "my.telegram.org → API development tools",
}
# Required only with the Sheets mirror switched on (JV-07).
SHEETS_HINTS: dict[str, str] = {
    "SHEETS_SPREADSHEET_ID": (
        "jadval manzilidagi uzun id: docs.google.com/spreadsheets/d/<id>/edit"
    ),
}

# The Sheets key's default path, relative to the app directory (/app).
SHEETS_KEY_DEFAULT = "./secrets/sheets_service_account.json"
APP_DIR = Path("/app")
# The models miya.services.usage prices without *_MODEL_PRICE (a test keeps
# this equal to usage.MODEL_PRICES: this module may not import it, rule above).
PRICED_MODELS: frozenset[str] = frozenset(
    {"claude-haiku-4-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-5"}
)
MODEL_DEFAULTS: dict[str, str] = {
    "EXTRACT_MODEL": "claude-haiku-4-5",
    "REASON_MODEL": "claude-sonnet-5",
}
# After any edit of the Jarvis lines: bot, worker and userbot each read them.
RELOAD = "make reload"

# More missing keys than this are reported as one line (C1).
MISSING_KEYS_LISTED = 3
API_TOKEN_MIN_LENGTH = 32
# An "8 GB" VPS reports about 7.6–7.8 GiB of MemTotal.
MIN_RAM_GIB = 7.3
MIN_DISK_FREE_GB = 20
_GIB = 1024**3
_GB = 1000**3


@dataclass(frozen=True)
class Finding:
    # info: printed for the owner (an address to share a sheet with), never
    # a problem.
    level: Literal["error", "warn", "info"]
    text: str


def _error(text: str) -> Finding:
    return Finding("error", text)


def _warn(text: str) -> Finding:
    return Finding("warn", text)


def _info(text: str) -> Finding:
    return Finding("info", text)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _meminfo_kib(meminfo: str, field: str) -> int | None:
    match = re.search(rf"^{field}:\s+(\d+)\s*kB", meminfo, re.MULTILINE)
    return int(match.group(1)) if match else None


def _upload_tokens(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for entry in raw.replace(",", " ").split():
        name, sep, token = entry.partition(":")
        if sep and name.strip() and token.strip():
            pairs[name.strip()] = token.strip()
    return pairs


def _sheets_key(path: str, read_file: Callable[[str], bytes]) -> list[Finding]:
    """C14: the Sheets service-account key the worker will read, and the
    address the owner shares the spreadsheet with."""
    name = "SHEETS_SERVICE_ACCOUNT_JSON"
    try:
        raw = read_file(path)
    except PermissionError:
        # make up's init hands ./secrets to uid 10001; the doctor runs first,
        # so an unreadable key only warns rather than block that very fix.
        return [
            _warn(
                f"⚠️ {name}: {path} faylini MIYA o'qiy olmayapti (egasi root). "
                "make up buni o'zi tuzatadi; yoki serverda: "
                "sudo chown 10001:10001 secrets/sheets_service_account.json"
            )
        ]
    except OSError:
        return [
            _error(
                f"❌ {name}: {path} fayli topilmadi — Google Cloud'dan yuklangan JSON "
                "kalitni serverdagi miya/secrets/sheets_service_account.json ga "
                "ko'chir (yoki SHEETS_ENABLED=false)."
            )
        ]
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        return [
            _error(
                f"❌ {name}: {path} xizmat hisobi kaliti emas (type=service_account "
                "yo'q) — Google Cloud → IAM → Service accounts → Keys → Add key → JSON."
            )
        ]
    email = str(data.get("client_email") or "").strip()
    if not email:
        return [
            _error(f"❌ {name}: {path} ichida client_email yo'q — kalitni qayta yukla.")
        ]
    return [_info(f"ℹ️ Google jadvalni shu manzilga «Muharrir» qilib ulashing: {email}")]


def _jarvis(env: Mapping[str, str], value: Callable[[str], str]) -> list[Finding]:
    """C15: Jarvis switches that contradict each other or cannot work."""
    found: list[Finding] = []
    jarvis_on = _truthy(env.get("JARVIS_ENABLED", "false"))
    if _truthy(env.get("TELEGRAM_SEND_ENABLED", "false")) and not jarvis_on:
        found.append(
            _warn(
                "⚠️ TELEGRAM_SEND_ENABLED=true, lekin JARVIS_ENABLED=false — "
                "yuboruvchi ishga tushmaydi. Jarvis kerak bo'lsa .env da "
                "JARVIS_ENABLED=true, aks holda TELEGRAM_SEND_ENABLED=false; "
                f"keyin {RELOAD}."
            )
        )
    if not jarvis_on:
        return found
    if not _truthy(env.get("USERBOT_ENABLED", "true")):
        found.append(
            _warn(
                "⚠️ JARVIS_ENABLED=true, lekin USERBOT_ENABLED=false — mijoz xabarlari "
                "o'qilmaydi, Jarvis hech narsa qilmaydi. .env da USERBOT_ENABLED=true "
                f"qil, keyin {RELOAD}."
            )
        )
    elif not value("TELETHON_SESSION"):
        found.append(
            _warn(
                "⚠️ JARVIS_ENABLED=true, lekin TELETHON_SESSION bo'sh — Jarvis mijoz "
                "xabarlarini ko'rmaydi: make userbot-login, chiqqan TELETHON_SESSION=… "
                f"qatorini .env ga yoz, keyin {RELOAD}."
            )
        )
    mode = value("JARVIS_MODE").lower() or "draft"
    if mode == "auto":
        found.append(
            _info(
                "ℹ️ JARVIS_MODE=auto — bu faqat yuqori chegara: hech bir javob turi siz "
                "«✅ Ha, avtomatik» bosmaguningizcha o'zi yuborilmaydi. "
                "Boshlash uchun JARVIS_MODE=draft yetarli."
            )
        )
    elif mode in ("off", "shadow"):
        found.append(
            _info(
                f"ℹ️ JARVIS_MODE={mode} — qoralamalar sizga ko'rsatilmaydi. Ko'rsatish "
                f"uchun .env da JARVIS_MODE=draft, keyin {RELOAD} va botda "
                "/jarvis qoralama."
            )
        )
    for key, default in MODEL_DEFAULTS.items():
        model = value(key) or default
        if model not in PRICED_MODELS and not value(f"{key}_PRICE"):
            found.append(
                _warn(
                    f"⚠️ {key}={model} narxi noma'lum — Jarvis xarajatni hisoblay "
                    "olmaydi va faqat sizga eskalatsiya qiladi. .env ga "
                    f"{key}_PRICE=<kirish>,<chiqish> (USD / 1M token) yoz."
                )
            )
    return found


def check(
    env: Mapping[str, str],
    meminfo: str,
    disk_free: int,
    key_file_exists: bool,
    read_file: Callable[[str], bytes] | None = None,
) -> list[Finding]:
    """Everything wrong with this `.env` and this server, errors first.
    ``read_file`` reads a path as the app would (the Sheets key); without
    it the key file is not checked."""
    found: list[Finding] = []

    def value(key: str) -> str:
        return (env.get(key) or "").strip()

    # C1: keys that .env.example has and this .env lacks. After an update
    # many new keys are missing at once; they run on their defaults, so they
    # are one line, not one alarm each.
    missing = [key for key in EXPECTED_ENV_KEYS if key not in env]
    if len(missing) > MISSING_KEYS_LISTED:
        found.append(
            _warn(
                f"⚠️ .env faylida {len(missing)} ta yangi sozlama yo'q — ular standart "
                "qiymat bilan ishlaydi, o'zgartirmoqchi bo'lsang .env.example'dan "
                f"ko'chir: {', '.join(missing)}"
            )
        )
    else:
        for key in missing:
            found.append(_warn(f"⚠️ {key} .env faylida yo'q — .env.example'dan ko'chir."))

    # C2: a trailing comment read as the value.
    commented = {key for key, raw in env.items() if (raw or "").lstrip().startswith("#")}
    for key in sorted(commented):
        found.append(
            _error(
                f"❌ {key}: qiymat izohga o'xshaydi ('#…'). "
                ".env faylida izohni alohida qatorga o'tkaz."
            )
        )

    # C3: required values.
    required = dict(REQUIRED_HINTS)
    if _truthy(env.get("USERBOT_ENABLED", "true")):
        required.update(TELETHON_HINTS)
    if _truthy(env.get("SHEETS_ENABLED", "false")):
        required.update(SHEETS_HINTS)
    for key, hint in required.items():
        if key not in commented and not value(key):
            found.append(_error(f"❌ {key} bo'sh — to'ldir: {hint}"))

    # C4: the owner's API token.
    token = value("API_BEARER_TOKEN")
    if (
        token
        and "API_BEARER_TOKEN" not in commented
        and len(token) < API_TOKEN_MIN_LENGTH
    ):
        found.append(
            _error(
                f"❌ API_BEARER_TOKEN juda qisqa ({len(token)} belgi) — "
                "kamida 32: openssl rand -hex 32"
            )
        )

    # C5: device tokens.
    for name, device in _upload_tokens(value("UPLOAD_TOKENS")).items():
        if len(device) < API_TOKEN_MIN_LENGTH or device == token:
            found.append(
                _warn(
                    f"⚠️ UPLOAD_TOKENS: «{name}» tokeni juda qisqa yoki API_BEARER_TOKEN "
                    "bilan bir xil — alohida openssl rand -hex 32 ishlat."
                )
            )

    # C6: the owner id.
    owner_id = value("OWNER_TELEGRAM_ID")
    if owner_id and not owner_id.isdigit() and "OWNER_TELEGRAM_ID" not in commented:
        found.append(
            _error("❌ OWNER_TELEGRAM_ID faqat raqam bo'lishi kerak (masalan 123456789).")
        )

    # C7: the backup key, the same rule as the settings validator.
    recipient = value("BACKUP_AGE_RECIPIENT")
    if (
        recipient
        and "BACKUP_AGE_RECIPIENT" not in commented
        and not recipient.startswith(("age1", "ssh-"))
    ):
        found.append(
            _error(
                "❌ BACKUP_AGE_RECIPIENT «age1…» bilan boshlanishi kerak — "
                "make backup-key chiqargan qatorni ko'chir."
            )
        )

    # C8: the database password.
    password = value("POSTGRES_PASSWORD")
    if password == "change-me":
        found.append(
            _error(
                "❌ POSTGRES_PASSWORD hali «change-me» — birinchi make up'dan oldin "
                "almashtir (keyin o'zgartirish qiyin)."
            )
        )
    elif password and f":{password}@" not in value("DATABASE_URL"):
        found.append(
            _error("❌ DATABASE_URL'dagi parol POSTGRES_PASSWORD bilan bir xil emas.")
        )

    # C9, C10: what works without, but works worse.
    if not value("OWNER_ALIASES"):
        found.append(
            _warn(
                "⚠️ OWNER_ALIASES bo'sh — guruhlarda «Bekzod aka, …» deb yozilgan "
                "xabarlar senga qaratilgan deb tanilmaydi."
            )
        )
    # With Jarvis on, C15 says the same with the whole fix.
    if (
        _truthy(env.get("USERBOT_ENABLED", "true"))
        and not value("TELETHON_SESSION")
        and not _truthy(env.get("JARVIS_ENABLED", "false"))
    ):
        found.append(
            _warn("⚠️ TELETHON_SESSION bo'sh — make up'dan keyin make userbot-login qil.")
        )

    # C11: memory and swap.
    mem_kib = _meminfo_kib(meminfo, "MemTotal")
    if mem_kib is not None and mem_kib * 1024 / _GIB < MIN_RAM_GIB:
        gb = mem_kib * 1024 / _GIB
        found.append(
            _error(
                f"❌ Serverda {gb:.1f} GB RAM bor; MIYA uchun kamida 8 GB kerak (qidiruv "
                "modeli o'zi ~3 GB oladi). Kattaroq server ol yoki SKIP_DOCTOR=1 bilan "
                "o'z xavfingga ishga tushir."
            )
        )
    if _meminfo_kib(meminfo, "SwapTotal") == 0:
        found.append(
            _warn("⚠️ Swap yo'q — 2–4 GB swap qo'sh (docs/ornatish.md, 1-qadam).")
        )

    # C12: disk.
    if disk_free < MIN_DISK_FREE_GB * _GB:
        found.append(
            _warn(
                f"⚠️ Diskda {disk_free / _GB:.0f} GB bo'sh — kamida 20 GB tavsiya etiladi."
            )
        )

    # C13: the secret half of the backup key.
    if recipient and not key_file_exists:
        found.append(
            _warn(
                "⚠️ secrets/backup-key.txt topilmadi — tiklash uchun maxfiy kalit kerak. "
                "make backup-key-show bilan nusxasini server tashqarisida saqla."
            )
        )

    # C14: the Sheets key, read as the worker will read it.
    if _truthy(env.get("SHEETS_ENABLED", "false")) and read_file is not None:
        path = value("SHEETS_SERVICE_ACCOUNT_JSON") or SHEETS_KEY_DEFAULT
        found.extend(_sheets_key(path, read_file))

    # C15: the Jarvis switches.
    found.extend(_jarvis(env, value))

    # C16: the evening recap moved to 20:00; an .env from before still says 19:00.
    if value("REPORT_TIME") == "19:00":
        found.append(
            _info(
                "ℹ️ REPORT_TIME=19:00 — yangi standart 20:00. Kechki xulosa 20:00 da "
                "kelsin desang: .env da REPORT_TIME=20:00 qil (yoki qatorni o'chir), "
                f"keyin {RELOAD}."
            )
        )

    rank = {"error": 0, "warn": 1, "info": 2}
    found.sort(key=lambda f: rank[f.level])
    return found


def summary(findings: list[Finding], *, in_update: bool = False) -> str:
    """``in_update``: run by ``make update`` (MIYA_IN_UPDATE=1), which goes on
    by itself, so the all-clear does not tell the owner to type make up."""
    errors = sum(1 for f in findings if f.level == "error")
    if errors:
        return f"❌ {errors} ta muammo. Tuzatib, qaytadan: make doctor"
    if in_update:
        return "✅ Hammasi joyida — make update davom etadi, hech narsa yozmang."
    return "✅ Hammasi joyida — endi: make up"


def exit_code(findings: list[Finding]) -> int:
    return 1 if any(f.level == "error" for f in findings) else 0


def _read_meminfo() -> str:
    try:
        return Path("/proc/meminfo").read_text()
    except OSError:
        return ""


def _disk_free() -> int:
    for path in ("/data", "/"):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            continue
    return 0


def _key_file_exists() -> bool:
    return any(
        Path(p).exists()
        for p in ("/app/secrets/backup-key.txt", "secrets/backup-key.txt")
    )


def _read_app_file(path: str) -> bytes:
    """A file as the app reads it: a relative path is under /app (the
    containers' working directory), or the current one outside a container."""
    file = Path(path)
    if not file.is_absolute():
        file = (APP_DIR if APP_DIR.is_dir() else Path.cwd()) / file
    return file.read_bytes()


def main() -> int:
    findings = check(
        os.environ,
        _read_meminfo(),
        _disk_free(),
        _key_file_exists(),
        read_file=_read_app_file,
    )
    for finding in findings:
        print(finding.text)
    print(summary(findings, in_update=os.environ.get("MIYA_IN_UPDATE") == "1"))
    return exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
