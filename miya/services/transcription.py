"""Speech-to-text behind a swappable interface (spec §3).

ElevenLabs Scribe is the Phase 1 backend because of its Uzbek support. A local
fine-tuned Whisper can replace it later by implementing `Transcriber` and adding
a branch to `get_transcriber()` — nothing else in the codebase changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
    # (speaker number, words) per turn when diarised (WP-85); else empty.
    segments: list[tuple[int, str]] = field(default_factory=list)


SPEAKER_LABEL = "[{n}-ovoz]"


def segments_from_payload(payload: dict) -> list[tuple[int, str]]:
    """Consecutive words of one speaker as one turn; speakers numbered 1, 2…
    in the order they first speak. Empty when the payload names no speaker."""
    numbers: dict[str, int] = {}
    turns: list[tuple[int, list[str]]] = []
    for word in payload.get("words") or []:
        if not isinstance(word, dict):
            continue
        text = word.get("text") or ""
        speaker = word.get("speaker_id")
        if speaker is None:
            if word.get("type") == "word":
                return []  # a word without a speaker: not a diarised payload
            if turns:
                turns[-1][1].append(text)
            continue
        number = numbers.setdefault(str(speaker), len(numbers) + 1)
        if turns and turns[-1][0] == number:
            turns[-1][1].append(text)
        else:
            turns.append((number, [text]))
    out = []
    for number, parts in turns:
        words = " ".join("".join(parts).split())
        if words:
            out.append((number, words))
    return out


def render_segments(segments: list[tuple[int, str]]) -> str:
    """'[1-ovoz] …' lines, one per turn."""
    return "\n".join(f"{SPEAKER_LABEL.format(n=n)} {text}" for n, text in segments)


class TranscriptionError(RuntimeError):
    pass


class Transcriber(ABC):
    name: str = "base"
    model: str | None = None

    @abstractmethod
    async def transcribe(
        self, path: str | Path, *, language_hint: str | None = None, **options
    ) -> Transcript: ...


class ElevenLabsScribe(Transcriber):
    name = "elevenlabs"
    model = SCRIBE_MODEL

    def __init__(self, api_key: str | None = None, timeout: float = 300.0) -> None:
        self._api_key = api_key or settings.elevenlabs_api_key
        self._timeout = timeout

    async def transcribe(
        self,
        path: str | Path,
        *,
        language_hint: str | None = None,
        diarize: bool = False,
    ) -> Transcript:
        if not self._api_key:
            raise TranscriptionError("ELEVENLABS_API_KEY is not configured")

        file_path = Path(path)
        if not file_path.is_file():
            raise TranscriptionError(f"audio file not found: {file_path}")

        data = {"model_id": SCRIBE_MODEL}
        if language_hint:
            data["language_code"] = language_hint
        if diarize:
            # Who spoke when (WP-85); calls only, and only when switched on.
            data["diarize"] = "true"

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

        # Any parsing surprise must surface as TranscriptionError: callers
        # treat that as "flag needs_review and keep the dedupe hash". A raw
        # exception here would roll the hash back and re-bill Scribe for the
        # same file on every sweep.
        try:
            payload = response.json()
            segments = segments_from_payload(payload) if diarize else []
            text = (payload.get("text") or "").strip()
            return Transcript(
                text=render_segments(segments) if segments else text,
                language=payload.get("language_code"),
                duration=_duration_from_payload(payload),
                segments=segments,
            )
        except (ValueError, AttributeError, TypeError) as exc:
            raise TranscriptionError(
                f"Scribe returned an unparseable body: {exc}"
            ) from exc


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
