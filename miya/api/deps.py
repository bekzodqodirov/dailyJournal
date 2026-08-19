"""Shared FastAPI dependencies."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from miya.config import settings

_bearer = HTTPBearer(auto_error=False)


async def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Guard every non-public route with the static bearer token from .env.

    The API is bound to localhost, but a second lock costs nothing and stops a
    stray container on the same Docker network from reading the owner's data.
    """
    if not settings.api_bearer_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_BEARER_TOKEN is not configured",
        )
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, settings.api_bearer_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
