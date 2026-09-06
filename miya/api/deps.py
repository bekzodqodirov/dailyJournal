"""Shared FastAPI dependencies.

The token check lives here as a plain function rather than only as a
dependency, because it has to be callable from two places: the ASGI guard in
`miya.api.middleware`, which must decide *before* the request body is read, and
the router dependency below, which stays as defence in depth in case a route is
ever mounted outside the guard.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from miya.config import settings

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


def is_upload_path(path: str) -> bool:
    """True for the two routes the Android companion is allowed to reach."""
    return path == "/v1/recordings" or path.startswith("/v1/recordings/")


def bearer_credentials(authorization: str | None) -> str | None:
    """The token out of an `Authorization: Bearer …` header, if it is one."""
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


def check_authorization(authorization: str | None, *, path: str) -> None:
    """Raise unless the header carries a token that is valid for `path`.

    Two token families. `API_BEARER_TOKEN` is the owner's, and opens
    everything. An `UPLOAD_TOKENS` entry is a device's, and opens only the
    recording routes — so a stolen phone (or an APK someone unzipped) can push
    audio but cannot read a single debt, transcript or contact back out.
    """
    upload_tokens = settings.upload_tokens_parsed
    if not settings.api_bearer_token and not upload_tokens:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_BEARER_TOKEN is not configured",
        )

    presented = bearer_credentials(authorization)
    if presented is not None:
        if settings.api_bearer_token and secrets.compare_digest(
            presented, settings.api_bearer_token
        ):
            return
        if is_upload_path(path) and any(
            secrets.compare_digest(presented, token) for token in upload_tokens.values()
        ):
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing bearer token",
        headers=_UNAUTHORIZED,
    )


async def require_token(request: Request) -> None:
    """Guard every non-public route with the static bearer token from .env.

    The API is bound to localhost, but a second lock costs nothing and stops a
    stray container on the same Docker network from reading the owner's data.
    By the time this runs the ASGI guard has already made the same decision
    without reading the body; this is the belt to that pair of braces.
    """
    check_authorization(request.headers.get("authorization"), path=request.url.path)
