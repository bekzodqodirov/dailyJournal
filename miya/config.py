"""Application settings, loaded from environment / .env."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_hhmm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(hour=int(hour), minute=int(minute))


def _within(current: time, start: time, end: time) -> bool:
    """Is ``current`` inside [start, end), a range that may wrap midnight?

    The same rule as reminders.in_quiet_hours, spelled here because that
    module imports this one.
    """
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _blank_to_none(v: object) -> object:
    """`.env.example` ships credentials as empty keys; treat those as unset."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


# An integer setting that may legitimately be blank until the owner fills it in.
OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://miya:miya@db:5432/miya"

    # --- Anthropic ----------------------------------------------------------
    anthropic_api_key: str = ""
    # Cheap, fast model for the structured-extraction pipeline (see §5 of the spec).
    extract_model: str = "claude-haiku-4-5"
    # Reasoning model for the daily report, planner and RAG answers.
    reason_model: str = "claude-sonnet-5"

    # --- Transcription ------------------------------------------------------
    elevenlabs_api_key: str = ""
    transcriber: str = "elevenlabs"  # future: local_whisper

    # --- Embeddings ---------------------------------------------------------
    embed_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    # When set (bot/worker containers), embedding requests go to the API
    # process over HTTP so only one process holds the ~2 GB model in RAM.
    # Empty (api container) means load the model locally.
    embed_service_url: str = ""

    # --- Telegram -----------------------------------------------------------
    assistant_bot_token: str = ""
    owner_telegram_id: OptionalInt = None
    userbot_enabled: bool = True
    telethon_api_id: OptionalInt = None
    telethon_api_hash: str = ""
    telethon_session: str = ""
    # How people address the owner in groups, comma-separated ("Bekzod,
    # Begi, Bekzod aka"). A group message containing one of these as a whole
    # word — Latin or Cyrillic, any case — counts as aimed at him, the same
    # as an @-mention. Empty by default: the aliases are the owner's own and
    # belong in .env, not in code.
    owner_aliases: str = ""

    # --- Open loops (docs/owner-decisions.md, build step 2) -----------------
    # A question nobody answered is nudged after this many hours.
    loop_question_hours: int = 4
    # ... and is not a loop at all once older than this many days. The bound
    # is also what keeps the half-hourly scan on the occurred_at indexes.
    loop_question_max_days: int = Field(default=30, ge=1)
    # An undated promise or debt untouched this long is "ageing" — and the
    # weekly "Hali ochiqmi?" uses the same threshold, as does the re-ask
    # cadence of a dated item the owner said is still open (the owner's
    # decision: re-remind after one week). One knob, one week.
    loop_undated_days: int = Field(default=7, ge=1)
    # Someone with an open debt or promise not heard from for this long.
    loop_quiet_days: int = 14
    # When the morning brief goes out (owner's timezone). The owner's
    # decision: 09:00 Asia/Tashkent — never skipped, so it must not fall
    # inside QUIET_HOURS; a validator refuses that combination at start-up.
    morning_brief_time: str = "09:00"

    # --- Userbot conversation windows (spec §7B) ----------------------------
    window_idle_minutes: int = 30
    # A private chat, or a group backlog with a message aimed at the owner,
    # closes its window after this much silence instead — and is extracted
    # at once rather than on the next batch (build step 2: the instant path).
    window_idle_minutes_addressed: int = 5
    window_max_messages: int = 25
    window_max_chars: int = 4000
    # A window whose batch keeps failing falls back to real-time extraction.
    batch_max_attempts: int = 3

    # --- Documents (spec §6) ------------------------------------------------
    doc_max_bytes: int = 20 * 1024 * 1024
    doc_max_chars: int = 15_000

    # --- Audio files (spec §6) ----------------------------------------------
    # Cap for *audio files* (music, podcasts) shared into monitored chats.
    # Voice notes are always transcribed; a shared 200 MB podcast would burn
    # bandwidth and Scribe minutes on content that is rarely business.
    audio_max_bytes: int = 30 * 1024 * 1024

    # --- Oversized media: ask rather than skip ------------------------------
    # Anything above audio_max_bytes / doc_max_bytes, and every video, used to
    # be dropped silently. The owner would rather decide: MIYA asks in Telegram
    # and downloads only on a yes. Above the hard ceiling it does not even ask —
    # a multi-gigabyte file is not worth a round trip.
    media_ask_max_bytes: int = 500 * 1024 * 1024
    # How long an unanswered question stays actionable. After this the buttons
    # are stale — the owner has moved on and the file is rarely still relevant.
    media_ask_expiry_hours: int = 48

    # --- Google Calendar ----------------------------------------------------
    google_oauth_client_json: str = "./secrets/google_oauth.json"
    google_token_json: str = "./secrets/google_token.json"
    gcal_calendar_id: str = "primary"
    gcal_pull_minutes: int = 30
    gcal_days_ahead: int = 14

    # --- Call recordings ----------------------------------------------------
    call_recordings_dir: str = "/data/call_recordings"
    audio_retention_days: int = 90

    # --- Backups (spec §10) -------------------------------------------------
    backup_dir: str = "/data/backups"
    backup_retention_days: int = 14
    # age public key (age1…). Without it the backup job does nothing rather
    # than writing an unencrypted dump of every debt and transcript to disk.
    backup_age_recipient: str = ""
    backup_time: str = "03:30"

    # --- Internal API -------------------------------------------------------
    api_bearer_token: str = ""
    api_port: int = 8000
    # Optional per-device tokens for the Android companion, as space- or
    # comma-separated `name:token` pairs (e.g. "phone:9f3c…"). A token listed
    # here opens /v1/recordings and /v1/recordings/probe and nothing else, so
    # an extracted APK cannot read /v1/ask, /v1/debts or /v1/config. Leave it
    # empty and the phone uses API_BEARER_TOKEN, which authorises everything.
    upload_tokens: str = ""

    # --- Scheduling ---------------------------------------------------------
    timezone: str = "Asia/Tashkent"
    report_time: str = "19:00"
    quiet_hours: str = "23:30-07:30"
    batch_flush_hours: int = 3

    # --- Misc ---------------------------------------------------------------
    log_level: str = "INFO"
    debug: bool = Field(default=False)

    @field_validator("report_time", "backup_time", "morning_brief_time")
    @classmethod
    def _validate_report_time(cls, v: str) -> str:
        _parse_hhmm(v)
        return v

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet_hours(cls, v: str) -> str:
        start, end = v.split("-")
        _parse_hhmm(start)
        _parse_hhmm(end)
        return v

    @model_validator(mode="after")
    def _brief_outside_quiet_hours(self) -> Settings:
        """A brief inside quiet hours would either wake the owner or never
        go out; neither is what "09:00, never skipped" means."""
        start, end = self.quiet_hours_parsed
        if _within(self.morning_brief_time_parsed, start, end):
            raise ValueError(
                f"MORNING_BRIEF_TIME={self.morning_brief_time} falls inside "
                f"QUIET_HOURS={self.quiet_hours}; the brief is never skipped, "
                "so pick a time outside the quiet range"
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def media_dir(self) -> Path:
        """Where bot-supplied voice notes and photos are kept.

        A sibling of the call-recordings share so both live on the same
        encrypted volume and share one retention policy.
        """
        return Path(self.call_recordings_dir).parent / "bot_media"

    @property
    def report_time_parsed(self) -> time:
        return _parse_hhmm(self.report_time)

    @property
    def backup_time_parsed(self) -> time:
        return _parse_hhmm(self.backup_time)

    @property
    def morning_brief_time_parsed(self) -> time:
        return _parse_hhmm(self.morning_brief_time)

    @property
    def owner_aliases_parsed(self) -> tuple[str, ...]:
        """The comma-separated aliases, trimmed, blanks dropped, order kept."""
        return tuple(
            alias.strip() for alias in self.owner_aliases.split(",") if alias.strip()
        )

    @property
    def upload_tokens_parsed(self) -> dict[str, str]:
        """`name:token` pairs → {name: token}. Malformed entries are ignored."""
        pairs: dict[str, str] = {}
        for entry in self.upload_tokens.replace(",", " ").split():
            name, sep, token = entry.partition(":")
            if sep and name.strip() and token.strip():
                pairs[name.strip()] = token.strip()
        return pairs

    @property
    def quiet_hours_parsed(self) -> tuple[time, time]:
        start, end = self.quiet_hours.split("-")
        return _parse_hhmm(start), _parse_hhmm(end)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
