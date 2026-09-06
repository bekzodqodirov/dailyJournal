"""The guard that runs before anything reads the request body.

Why this exists at all: FastAPI parses the body *before* it solves a route's
dependencies (`fastapi/routing.py` calls `await request.form()` and only then
`solve_dependencies`), and Starlette's multipart parser applies its
`max_part_size` to non-file parts only — a file part is streamed, unbounded,
into a `SpooledTemporaryFile` that rolls over to a real file on disk at 1 MB.
Together that meant an anonymous POST to /v1/recordings had its whole payload
on disk by the time `Depends(require_token)` produced the 401, and that
`RECORDING_UPLOAD_MAX_BYTES` was an assertion made after the fact rather than a
cap. Neither is fixable inside the handler, so both decisions are made out
here, as pure ASGI, above the router:

  * the bearer token is checked against the request headers, so an
    unauthenticated request is answered without its body ever being received;
  * a declared Content-Length over the route's limit is refused with 413
    before the receive channel is touched;
  * the receive channel is then wrapped in a counter, so a client that lies
    about (or omits) its Content-Length is cut off at the same limit instead
    of being trusted.

The handler still counts the bytes it writes; see `miya.api.uploads`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from miya.api.deps import check_authorization
from miya.services import call_recordings

log = logging.getLogger(__name__)

# Everything under /v1 that is not an upload: /ask, /embed, /recordings/probe.
# The largest legitimate body is a 200-item probe batch (~40 KB), so a
# mebibyte is generous and still stops an unbounded `await request.body()`.
JSON_BODY_MAX_BYTES = 1024 * 1024

GUARDED_PREFIX = "/v1"


def body_limit_for(path: str) -> int:
    """How many bytes this route may receive."""
    if path.rstrip("/") == "/v1/recordings":
        # Read through the module, not an imported copy: a from-import
        # binds the value here as a second name, and a test that patches
        # the cap would move one of them while the guard kept the other.
        return call_recordings.RECORDING_UPLOAD_MAX_BYTES
    return JSON_BODY_MAX_BYTES


class BodyTooLarge(HTTPException):
    """Raised out of `receive` once a body outgrows its route's limit.

    An HTTPException, so it surfaces through FastAPI's exception handler as a
    413 wherever the handler happened to be awaiting the next chunk.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413,
            detail=f"request body exceeds the {limit} byte limit",
        )


def _counting_receive(receive: Receive, limit: int) -> Receive:
    seen = 0

    async def guarded() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise BodyTooLarge(limit)
        return message

    return guarded


class RequestGuard:
    """Authenticate and bound every /v1 request before its body is read."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(
            GUARDED_PREFIX
        ):
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        headers = Headers(scope=scope)

        try:
            check_authorization(headers.get("authorization"), path=path)
        except HTTPException as exc:
            # Answered from the headers alone: not one byte of the body has
            # been requested from the transport, so nothing was spooled.
            await self._respond(
                exc.status_code, exc.detail, exc.headers, scope, receive, send
            )
            return

        limit = body_limit_for(path)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await self._respond(
                    400, "malformed Content-Length", None, scope, receive, send
                )
                return
            if length > limit:
                log.warning(
                    "refused %s: declared %d bytes over the %d byte limit",
                    path,
                    length,
                    limit,
                )
                noun = (
                    "recording"
                    if limit == call_recordings.RECORDING_UPLOAD_MAX_BYTES
                    else "request body"
                )
                await self._respond(
                    413,
                    f"{noun} exceeds the {limit} byte upload limit",
                    None,
                    scope,
                    receive,
                    send,
                )
                return

        await self.app(scope, _counting_receive(receive, limit), send)

    @staticmethod
    async def _respond(
        status_code: int,
        detail: Any,
        headers: dict[str, str] | None,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        response = JSONResponse(
            {"detail": detail}, status_code=status_code, headers=headers
        )
        await response(scope, receive, send)
