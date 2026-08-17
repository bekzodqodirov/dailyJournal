"""Speech-to-text behind a swappable interface (spec §3).

ElevenLabs Scribe is the Phase 1 backend because of its Uzbek support. A local
fine-tuned Whisper can replace it later by implementing `Transcriber` and adding
a branch to `get_transcriber()` — nothing else in the codebase changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from miya.config import settings

log = logging.getLogger(__name__)

SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
SCRIBE_MODEL = "scribe_v1"


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None
    duration: float  # seconds


class TranscriptionError(RuntimeError):
    pass


class Transcriber(ABC):
    name: str = "base"
    model: str | None = None

    @abstractmethod
    async def transcribe(
        self, path: str | Path, *, language_hint: str | None = None
    ) -> Transcript: ...


class ElevenLabsScribe(Transcriber):
    name = "elevenlabs"
    model = SCRIBE_MODEL

    def __init__(self, api_key: str | None = None, timeout: float = 300.0) -> None:
        self._api_key = api_key or settings.elevenlabs_api_key
        self._timeout = timeout

    async def transcribe(
        self, path: str | Path, *, language_hint: str | None = None
    ) -> Transcript:
        if not self._api_key:
            raise TranscriptionError("ELEVENLABS_API_KEY is not configured")

        file_path = Path(path)
        if not file_path.is_file():
            raise TranscriptionError(f"audio file not found: {file_path}")

        data = {"model_id": SCRIBE_MODEL}
        if language_hint:
            data["language_code"] = language_hint

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                with file_path.open("rb") as fh:
                    response = await client.post(
                        SCRIBE_URL,
                        headers={"xi-api-key": self._api_key},
                        data=data,
                        files={"file": (file_path.name, fh)},
                    )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"Scribe request failed: {exc}") from exc

        if response.status_code >= 400:
            raise TranscriptionError(
                f"Scribe returned {response.status_code}: {response.text[:300]}"
            )

        payload = response.json()
        return Transcript(
            text=(payload.get("text") or "").strip(),
            language=payload.get("language_code"),
            duration=_duration_from_payload(payload),
        )


def _duration_from_payload(payload: dict) -> float:
    """Scribe has no duration field; derive it from the last word's end time."""
    words = payload.get("words") or []
    for word in reversed(words):
        end = word.get("end")
        if isinstance(end, int | float):
            return float(end)
    return 0.0


def get_transcriber() -> Transcriber:
    backend = settings.transcriber.lower()
    if backend == "elevenlabs":
        return ElevenLabsScribe()
    raise TranscriptionError(
        f"unknown TRANSCRIBER={settings.transcriber!r}; supported: elevenlabs"
    )
