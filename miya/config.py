"""Application settings, loaded from environment / .env."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_hhmm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(hour=int(hour), minute=int(minute))


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

    # --- Telegram -----------------------------------------------------------
    assistant_bot_token: str = ""
    owner_telegram_id: OptionalInt = None
    userbot_enabled: bool = True
    telethon_api_id: OptionalInt = None
    telethon_api_hash: str = ""
    telethon_session: str = ""

    # --- Google Calendar ----------------------------------------------------
    google_oauth_client_json: str = "./secrets/google_oauth.json"

    # --- Call recordings ----------------------------------------------------
    call_recordings_dir: str = "/data/call_recordings"
    audio_retention_days: int = 90

    # --- Internal API -------------------------------------------------------
    api_bearer_token: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- Scheduling ---------------------------------------------------------
    timezone: str = "Asia/Tashkent"
    report_time: str = "19:00"
    quiet_hours: str = "23:30-07:30"
    batch_flush_hours: int = 3

    # --- Misc ---------------------------------------------------------------
    log_level: str = "INFO"
    debug: bool = Field(default=False)

    @field_validator("report_time")
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
    def quiet_hours_parsed(self) -> tuple[time, time]:
        start, end = self.quiet_hours.split("-")
        return _parse_hhmm(start), _parse_hhmm(end)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
