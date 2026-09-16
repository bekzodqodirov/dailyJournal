"""Streaming multipart reader for POST /v1/recordings.

FastAPI's `UploadFile` cannot be used here. It is produced by
`await request.form()`, which Starlette runs to completion before the handler
is even entered — the whole file part is written to a temp file with no size
limit at all, and only then does the handler get a chance to look at it. So
this module reads the body itself, one bounded slice at a time, and hands the
handler:

  * the `meta` part, decoded, capped, and available *before* the audio starts
    (the wire contract puts `meta` first exactly so the server can refuse a
    recording on its metadata without spooling a byte);
  * the `audio` part as an async iterator of chunks, counted as they arrive.

Nothing is buffered beyond one slice, so an oversized upload is cut off at the
limit rather than measured after the fact, and a request that is refused —
wrong token, bad metadata, already have it — never reaches the filesystem.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from python_multipart.multipart import MultipartParser, parse_options_header

log = logging.getLogger(__name__)

# How much of the body is handed to the parser at a time. Everything the
# parser emits from one slice is held in memory until the handler consumes it,
# so this is the reader's real memory ceiling — regardless of how large a
# chunk the transport hands us.
SLICE_BYTES = 64 * 1024

# The `meta` part is a small JSON object (design §2.5); anything larger is a
# client bug or an attempt to make us buffer.
META_MAX_BYTES = 64 * 1024


@dataclass(slots=True)
class PartInfo:
    """What a part's headers say about it. The filename is provenance only."""

    name: str
    filename: str | None
    content_type: str | None


class _Collector:
    """Turns the parser's callbacks into an ordered queue of events.

    The callbacks are synchronous and fire in the middle of `parser.write()`,
    while the work they trigger (opening a file, a duplicate check) is async —
    so they only record, and the reader below does the work between slices.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self._field = bytearray()
        self._value = bytearray()
        self._headers: dict[bytes, bytes] = {}

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._headers = {}
        self._field.clear()
        self._value.clear()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field.extend(data[start:end])

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value.extend(data[start:end])

    def _header_end(self) -> None:
        self._headers[bytes(self._field).lower()] = bytes(self._value)
        self._field.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        disposition, options = parse_options_header(
            self._headers.get(b"content-disposition", b"")
        )
        del disposition
        content_type, _ = parse_options_header(self._headers.get(b"content-type", b""))
        self.events.append(
            (
                "headers",
                PartInfo(
                    name=_decode(options.get(b"name")) or "",
                    filename=_decode(options.get(b"filename")),
                    content_type=_decode(content_type) or None,
                ),
            )
        )

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        self.events.append(("data", bytes(data[start:end])))

    def _part_end(self) -> None:
        self.events.append(("end", None))


def _decode(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


class RecordingUploadReader:
    """Reads `meta` then `audio` out of the request without spooling either."""

    def __init__(self, request: Request, *, max_audio_bytes: int) -> None:
        content_type, options = parse_options_header(
            request.headers.get("content-type", "")
        )
        if content_type != b"multipart/form-data" or not options.get(b"boundary"):
            raise HTTPException(
                status_code=422,
                detail="expected a multipart/form-data body with a boundary",
            )
        self.max_audio_bytes = max_audio_bytes
        self._collector = _Collector()
        self._parser = MultipartParser(options[b"boundary"], self._collector.callbacks())
        self._iterator = request.stream().__aiter__()
        self._carry = b""
        self._cursor = 0
        self._exhausted = False

    # --- body plumbing ------------------------------------------------------

    async def _next_slice(self) -> bytes | None:
        while not self._carry:
            try:
                self._carry = await self._iterator.__anext__()
            except StopAsyncIteration:
                return None
        head, self._carry = self._carry[:SLICE_BYTES], self._carry[SLICE_BYTES:]
        return head

    async def _next_event(self) -> tuple[str, Any] | None:
        """The next parser event, or None once the body is exhausted."""
        while self._cursor >= len(self._collector.events):
            if self._exhausted:
                return None
            self._collector.events.clear()
            self._cursor = 0
            chunk = await self._next_slice()
            if chunk is None:
                self._exhausted = True
                self._parser.finalize()
                continue
            self._parser.write(chunk)
        event = self._collector.events[self._cursor]
        self._cursor += 1
        return event

    # --- the two parts ------------------------------------------------------

    async def read_meta(self) -> tuple[str, PartInfo]:
        """Consume the body up to the first byte of audio.

        Returns the raw `meta` JSON and the audio part's headers, so the
        handler can validate metadata, pick a suffix and check for a duplicate
        while the phone is still sending.
        """
        meta: bytes | None = None
        buffer = bytearray()
        collecting: str | None = None

        while (event := await self._next_event()) is not None:
            kind, payload = event
            if kind == "headers":
                if payload.name == "audio":
                    if meta is None:
                        raise HTTPException(
                            status_code=422,
                            detail="the meta part must be sent before the audio part",
                        )
                    return _decode_meta(meta), payload
                if payload.name == "meta" and meta is None:
                    collecting = "meta"
                    buffer.clear()
                else:
                    # Forward compatibility: a newer app may send a part this
                    # server has never heard of. Its bytes are discarded, not
                    # buffered and never written anywhere.
                    log.warning("ignoring unexpected upload part %r", payload.name)
                    collecting = None
            elif kind == "data":
                if collecting == "meta":
                    buffer.extend(payload)
                    if len(buffer) > META_MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"meta exceeds the {META_MAX_BYTES} byte limit",
                        )
            elif kind == "end" and collecting == "meta":
                meta = bytes(buffer)
                collecting = None

        raise HTTPException(status_code=422, detail="multipart body has no audio part")

    async def stream_audio(self) -> AsyncIterator[bytes]:
        """The audio part's bytes, refused the moment they outgrow the cap."""
        total = 0
        while (event := await self._next_event()) is not None:
            kind, payload = event
            if kind == "data":
                total += len(payload)
                if total > self.max_audio_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"recording exceeds the {self.max_audio_bytes} "
                            "byte upload limit"
                        ),
                    )
                yield payload
            elif kind == "end":
                return
            else:
                raise HTTPException(
                    status_code=422, detail="the audio part ended unexpectedly"
                )
        raise HTTPException(
            status_code=422, detail="the upload ended mid-audio; nothing was stored"
        )

    async def drain(self) -> None:
        """Read out whatever follows the audio part, discarding it.

        Leaving a request body unread makes a keep-alive connection unusable
        and can lose the response the phone is waiting for. The trailer is a
        boundary and, at worst, parts from a future app version.
        """
        while (event := await self._next_event()) is not None:
            if event[0] == "headers":
                log.warning("ignoring trailing upload part %r", event[1].name)


def _decode_meta(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="meta is not valid UTF-8") from exc
