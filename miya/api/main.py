"""Internal FastAPI app (spec §11). Bound to localhost — never exposed publicly."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from miya import __version__
from miya.api.deps import require_token
from miya.config import settings
from miya.db.session import engine
from miya.services.embeddings import EmbeddingError, get_local_embedder

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    log.info("MIYA API starting (tz=%s, version=%s)", settings.timezone, __version__)
    yield
    await engine.dispose()
    log.info("MIYA API stopped")


app = FastAPI(
    title="MIYA",
    description="Personal AI second brain — internal API",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)


async def _db_ok() -> tuple[bool, str | None]:
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


@app.get("/health", tags=["meta"])
async def health() -> JSONResponse:
    """Liveness + database reachability. Public (no token) so `make up` can poll it."""
    db_ok, db_error = await _db_ok()
    body: dict[str, Any] = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "timezone": settings.timezone,
        "now": datetime.now(settings.tz).isoformat(),
        "database": "ok" if db_ok else "unreachable",
    }
    if db_error:
        body["database_error"] = db_error
    return JSONResponse(body, status_code=200 if db_ok else 503)


# Everything below the health check requires the bearer token.
api = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@api.get("/config", tags=["meta"])
async def config() -> dict[str, Any]:
    """Non-secret effective configuration — useful when debugging a deploy."""
    quiet_start, quiet_end = settings.quiet_hours_parsed
    return {
        "extract_model": settings.extract_model,
        "reason_model": settings.reason_model,
        "embed_model": settings.embed_model,
        "transcriber": settings.transcriber,
        "timezone": settings.timezone,
        "report_time": settings.report_time_parsed.strftime("%H:%M"),
        "quiet_hours": [quiet_start.strftime("%H:%M"), quiet_end.strftime("%H:%M")],
        "userbot_enabled": settings.userbot_enabled,
        "batch_flush_hours": settings.batch_flush_hours,
        "audio_retention_days": settings.audio_retention_days,
    }


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=256)


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    model: str
    dim: int


@api.post("/embed", tags=["embeddings"], response_model=EmbedResponse)
async def embed(body: EmbedRequest) -> EmbedResponse:
    """Embedding service for the bot and worker (see services/embeddings.py).

    This process is the only one holding bge-m3 in RAM; always the local
    embedder here, never get_embedder(), or a misconfigured EMBED_SERVICE_URL
    would make the API proxy to itself forever.
    """
    try:
        vectors = await get_local_embedder().embed(body.texts)
    except EmbeddingError as exc:
        log.exception("embed request failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return EmbedResponse(
        vectors=vectors, model=settings.embed_model, dim=settings.embed_dim
    )


app.include_router(api)
