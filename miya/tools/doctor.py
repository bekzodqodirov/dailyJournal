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

import os
import re
import shutil
import sys
from collections.abc import Mapping
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
    "GOOGLE_OAUTH_CLIENT_JSON",
    "GOOGLE_TOKEN_JSON",
    "GCAL_CALENDAR_ID",
    "GCAL_PULL_MINUTES",
    "GCAL_DAYS_AHEAD",
    "CALL_RECORDINGS_DIR",
    "AUDIO_RETENTION_DAYS",
    "LOOP_MISSED_CALL_MINUTES",
    "LOOP_MISSED_CALL_MAX_DAYS",
    "PAYMENT_SMS_SENDERS",
    "PAYMENT_ADVERTS_TO_REVIEW",
    "PAYMENT_APP_PACKAGES",
    "MONEY_AUTOBOOK",
    "PAYMENT_DEDUPE_WINDOW_MINUTES",
    "PAYMENT_REPEAT_SECONDS",
    "MONEY_RECEIPTS",
    "MONEY_RECEIPTS_FOLD_AT",
    "MONEY_RECEIPT_MAX_AGE_HOURS",
    "MONEY_RECEIPTS_SILENT_AT_NIGHT",
    "MONEY_TYPED_MATCH_HOURS",
    "RECAP_MAX_PARTS",
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
    "API_BEARER_TOKEN",
    "UPLOAD_TOKENS",
    "API_PORT",
    "API_MEM_LIMIT",
    "TIMEZONE",
    "REPORT_TIME",
    "QUIET_HOURS",
    "BATCH_FLUSH_HOURS",
    "LOG_LEVEL",
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

API_TOKEN_MIN_LENGTH = 32
# An "8 GB" VPS reports about 7.6–7.8 GiB of MemTotal.
MIN_RAM_GIB = 7.3
MIN_DISK_FREE_GB = 20
_GIB = 1024**3
_GB = 1000**3


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warn"]
    text: str


def _error(text: str) -> Finding:
    return Finding("error", text)


def _warn(text: str) -> Finding:
    return Finding("warn", text)


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


def check(
    env: Mapping[str, str],
    meminfo: str,
    disk_free: int,
    key_file_exists: bool,
) -> list[Finding]:
    """Everything wrong with this `.env` and this server, errors first."""
    found: list[Finding] = []

    def value(key: str) -> str:
        return (env.get(key) or "").strip()

    # C1: keys that .env.example has and this .env lacks.
    for key in EXPECTED_ENV_KEYS:
        if key not in env:
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
    if _truthy(env.get("USERBOT_ENABLED", "true")) and not value("TELETHON_SESSION"):
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

    found.sort(key=lambda f: f.level != "error")
    return found


def summary(findings: list[Finding]) -> str:
    errors = sum(1 for f in findings if f.level == "error")
    if errors:
        return f"❌ {errors} ta muammo. Tuzatib, qaytadan: make doctor"
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


def main() -> int:
    findings = check(os.environ, _read_meminfo(), _disk_free(), _key_file_exists())
    for finding in findings:
        print(finding.text)
    print(summary(findings))
    return exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
