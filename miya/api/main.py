"""Internal FastAPI app (spec §11). Bound to localhost — never exposed publicly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from miya import __version__
from miya.api.deps import require_token
from miya.config import settings
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    InteractionSource,
    PromiseStatus,
)
from miya.db.models import Debt, DebtPayment, Interaction, Person
from miya.db.session import engine, get_session
from miya.services import call_recordings, planner, queries, rag, reports
from miya.services.embeddings import EmbeddingError, get_local_embedder

SessionDep = Annotated[AsyncSession, Depends(get_session)]

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


# --- read models (spec §11) --------------------------------------------------
#
# A thin HTTP layer over the same services the bots use, for a future
# dashboard. Nothing here holds business logic of its own: money still comes
# out of services/queries.py, so an HTTP client and the bot can never disagree
# about a balance.


def _balance_json(balance: queries.DebtBalance) -> dict[str, Any]:
    return {
        "person_id": balance.person.id,
        "person": balance.person.display_name,
        "direction": balance.direction.value,
        "currency": balance.currency.value,
        "outstanding": str(balance.outstanding),
        "earliest_due": balance.earliest_due.isoformat()
        if balance.earliest_due
        else None,
        "debt_count": balance.count,
    }


@api.get("/debts", tags=["money"])
async def list_debts(
    session: SessionDep,
    direction: DebtDirection | None = None,
    person_id: int | None = None,
    status: str = "open",
) -> dict[str, Any]:
    """`status=open` (default) returns outstanding balances; `settled`, the
    closed debts themselves (spec §11's `?status=` filter)."""
    if status == "open":
        balances = await queries.open_debts(
            session, direction=direction, person_id=person_id
        )
        return {"debts": [_balance_json(b) for b in balances]}
    if status != "settled":
        raise HTTPException(status_code=422, detail="status must be open|settled")
    stmt = (
        sa.select(Debt, Person)
        .join(Person, Person.id == Debt.person_id)
        .where(Debt.status == DebtStatus.settled)
        .order_by(Debt.settled_at.desc().nulls_last())
        .limit(100)
    )
    if direction is not None:
        stmt = stmt.where(Debt.direction == direction)
    if person_id is not None:
        stmt = stmt.where(Debt.person_id == person_id)
    rows = (await session.execute(stmt)).all()
    return {
        "debts": [
            {
                "id": debt.id,
                "person_id": person.id,
                "person": person.display_name,
                "direction": debt.direction.value,
                "currency": debt.currency.value,
                "amount": str(debt.amount),
                "settled_at": debt.settled_at.isoformat() if debt.settled_at else None,
            }
            for debt, person in rows
        ]
    }


class SettleRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: Currency = Currency.UZS
    note: str = ""


@api.post("/debts/{debt_id}/settle", tags=["money"])
async def settle_debt(
    debt_id: int, body: SettleRequest, session: SessionDep
) -> dict[str, Any]:
    """Record a payment against one specific debt."""
    debt = await session.get(Debt, debt_id)
    if debt is None:
        raise HTTPException(status_code=404, detail="debt not found")
    if debt.currency != body.currency:
        raise HTTPException(
            status_code=422,
            detail=f"debt is in {debt.currency.value}, not {body.currency.value}",
        )

    paid = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(DebtPayment.amount), 0)).where(
            DebtPayment.debt_id == debt.id
        )
    )
    amount = body.amount.quantize(Decimal("0.01"))
    session.add(
        DebtPayment(
            debt_id=debt.id,
            amount=amount,
            currency=debt.currency,
            note=body.note or None,
        )
    )
    outstanding = debt.amount - Decimal(paid or 0) - amount
    if outstanding <= 0:
        debt.status = DebtStatus.settled
        debt.settled_at = datetime.now(settings.tz)
    else:
        debt.status = DebtStatus.partially_paid
    await session.commit()
    return {
        "debt_id": debt.id,
        "status": debt.status.value,
        "outstanding": str(max(outstanding, Decimal("0.00"))),
    }


@api.get("/promises", tags=["promises"])
async def list_promises(
    session: SessionDep,
    person_id: int | None = None,
    status: PromiseStatus = PromiseStatus.open,
) -> dict[str, Any]:
    items = await queries.promises_by_status(session, status=status, person_id=person_id)
    return {
        "promises": [
            {
                "id": promise.id,
                "person_id": person.id,
                "person": person.display_name,
                "made_by": promise.made_by.value,
                "description": promise.description,
                "due_date": promise.due_date.isoformat() if promise.due_date else None,
            }
            for promise, person in items
        ]
    }


@api.get("/transactions", tags=["money"])
async def list_transactions(
    session: SessionDep, date_from: date, date_to: date
) -> dict[str, Any]:
    summary = await queries.spending_summary(session, date_from, date_to)
    return {
        "date_from": summary.date_from.isoformat(),
        "date_to": summary.date_to.isoformat(),
        "income": {c.value: str(v) for c, v in summary.income.items()},
        "expense": {c.value: str(v) for c, v in summary.expense.items()},
        "by_category": [
            {"category": c, "currency": cur.value, "total": str(total)}
            for c, cur, total in summary.by_category
        ],
    }


@api.get("/people/{person_id}/summary", tags=["people"])
async def person_summary(person_id: int, session: SessionDep) -> dict[str, Any]:
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    summary = await queries.person_summary(session, person)
    return {
        "person": {
            "id": person.id,
            "display_name": person.display_name,
            "aliases": person.aliases,
        },
        "balances": [_balance_json(b) for b in summary.balances],
        "open_promises": [
            {"description": p.description, "made_by": p.made_by.value}
            for p in summary.open_promises
        ],
        "total_interactions": summary.total_interactions,
    }


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@api.post("/ask", tags=["intelligence"])
async def ask(body: AskRequest, session: SessionDep) -> dict[str, str]:
    """Same RAG path as the bot: figures come from SQL tools, never invented."""
    answer = await rag.answer(session, body.question)
    await session.commit()  # usage_log rows from the tool loop
    return {"answer": answer}


@api.post("/report/today", tags=["intelligence"])
async def report_today(session: SessionDep) -> dict[str, str]:
    content = await reports.generate_report(session)
    await session.commit()
    return {"report": content}


@api.get("/plan/tomorrow", tags=["intelligence"])
async def plan_tomorrow(session: SessionDep) -> dict[str, str]:
    plan = await planner.plan_tomorrow(session)
    await session.commit()
    return {"plan": plan}


@api.get("/usage", tags=["meta"])
async def usage(session: SessionDep, date_from: date, date_to: date) -> dict[str, Any]:
    summary = await queries.usage_summary(session, date_from, date_to)
    return {
        "date_from": summary.date_from.isoformat(),
        "date_to": summary.date_to.isoformat(),
        "total_usd": str(summary.total_usd),
        "today_usd": str(summary.today_usd),
        "cached_share": round(summary.cached_share, 4),
        "rows": [
            {
                "provider": row.provider,
                "model": row.model,
                "operation": row.operation,
                "calls": row.calls,
                "cost_usd": str(row.cost_usd),
            }
            for row in summary.rows
        ],
    }


# --- call recordings from the Android companion (design §2) ------------------
#
# The phone cannot record calls itself — the OEM dialer does, and the companion
# app only finds the file the dialer wrote and pushes it here. So this handler
# does the least it possibly can: authenticate, stream the bytes to disk under
# a byte cap, verify the hash, write the sidecar, rename atomically, answer.
# Everything that costs money or takes minutes (Scribe, Haiku, the notify) is
# left to the worker's existing one-minute sweep, which is where `max_instances
# =1`, per-file commit and the TranscriptionError semantics already live. A
# 300-second Scribe call inside a request handler would hold a mobile
# connection open across a cell handover *and* stall the only process holding
# bge-m3 in RAM for /v1/embed.

_MIME_SUFFIXES = {
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/amr": ".amr",
    "audio/3gpp": ".3ga",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
}


class RecordingMeta(BaseModel):
    """What the phone knows about a call, from Android's CallLog (design §2.5).

    Only the identity fields are required. Everything else is nullable on
    purpose: READ_CALL_LOG is hard-restricted on Android 10+ and a sideloaded
    APK may never be able to hold it, so a handset that can only report "an
    audio file appeared at this time" must still be able to upload. Unknown
    keys are ignored rather than rejected — a newer app version must not start
    failing against an older server.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(default=1, alias="schema")
    device_id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    started_at: datetime
    call_id: str | None = Field(default=None, max_length=128)
    duration_seconds: int | None = Field(default=None, ge=0)
    direction: str | None = None
    counterparty_name: str | None = Field(default=None, max_length=200)
    phone_e164: str | None = Field(default=None, max_length=32)
    locale: str | None = Field(default=None, max_length=16)
    sim_slot: int | None = None
    mime: str | None = None
    original_filename: str | None = None
    recorded_by: str | None = None
    correlation: str | None = None
    app_version: str | None = None
    client_ts: datetime | None = None


def _parse_meta(raw: str) -> RecordingMeta:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"meta is not valid JSON: {exc}"
        ) from exc
    try:
        return RecordingMeta.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "meta"
        raise HTTPException(
            status_code=422, detail=f"meta.{where}: {first['msg']}"
        ) from exc


def _audio_suffix(upload: UploadFile, meta: RecordingMeta) -> str:
    """The extension to stage under, from the client's filename or its type.

    The filename itself is provenance only — an attacker-controlled name must
    never reach a path, so only the suffix is taken, and only if it is one the
    scanner would pick up anyway.
    """
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in call_recordings.AUDIO_SUFFIXES:
        return suffix
    declared = (upload.content_type or meta.mime or "").split(";")[0].strip().lower()
    suffix = _MIME_SUFFIXES.get(declared, "")
    if suffix in call_recordings.AUDIO_SUFFIXES:
        return suffix
    raise HTTPException(
        status_code=422,
        detail=(
            f"unsupported audio type {declared or 'unknown'!r} "
            f"({upload.filename or 'no filename'}); expected one of "
            + " ".join(sorted(call_recordings.AUDIO_SUFFIXES))
        ),
    )


def _occurred_at(meta: RecordingMeta) -> tuple[datetime, bool]:
    """The call's start, and whether the phone's clock was obviously wrong.

    A handset whose clock is a year out would otherwise file the call into a
    day-bucket the daily report has already sent, and every relative date in
    the transcript ("ertaga") would resolve against the wrong week.
    """
    started = meta.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=settings.tz)
    now = datetime.now(settings.tz)
    if started > now + timedelta(hours=24) or started < now - timedelta(days=5 * 365):
        return now, True
    return started, False


def _write_chunk(fh, digest, chunk: bytes) -> None:
    digest.update(chunk)
    fh.write(chunk)


@api.post("/recordings", tags=["recordings"])
async def upload_recording(
    session: SessionDep,
    meta: Annotated[str, Form(description="RecordingMeta as JSON")],
    audio: Annotated[UploadFile, File(description="the recording itself")],
) -> JSONResponse:
    """Accept one call recording from the phone and stage it for the sweep.

    202 means staged, 200 means we already had it — both are success, and the
    phone drops the file from its queue on either. Anything else is a retry
    (5xx, timeouts) or a permanent failure (401, 422) it must surface.
    """
    parsed = _parse_meta(meta)
    suffix = _audio_suffix(audio, parsed)
    max_bytes = call_recordings.RECORDING_UPLOAD_MAX_BYTES
    if parsed.size_bytes > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"recording exceeds the {max_bytes} byte upload limit",
        )

    started_at, clock_suspect = _occurred_at(parsed)
    directory = Path(settings.call_recordings_dir)
    audio_path, side_path = call_recordings.staged_upload_paths(
        directory, started_at=started_at, sha256=parsed.sha256, suffix=suffix
    )

    # Both predicates, and both before a single byte is spooled: a 90-day-old
    # recording may have been purged from disk while its interaction lives on,
    # and a recording staged one minute ago has no interaction yet.
    if audio_path.exists() or await call_recordings.already_ingested(
        session, parsed.sha256, call_id=parsed.call_id
    ):
        return JSONResponse(
            {
                "status": "duplicate",
                "sha256": parsed.sha256,
                "call_id": parsed.call_id,
                "staged_as": audio_path.name,
            },
            status_code=200,
        )

    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    # `.part` is one of the scanner's temp markers, so a half-written upload is
    # already invisible to the sweep. The random middle keeps two concurrent
    # retries of the same recording off each other's bytes; they converge on
    # the one deterministic name at the rename below.
    tmp_path = audio_path.with_name(
        f"{audio_path.name}.{uuid.uuid4().hex[:8]}{call_recordings.UPLOAD_TEMP_SUFFIX}"
    )
    digest = hashlib.sha256()
    written = 0
    try:
        with tmp_path.open("wb") as fh:
            while chunk := await audio.read(1 << 20):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=422,
                        detail=f"recording exceeds the {max_bytes} byte upload limit",
                    )
                # Content-Length and UploadFile.size are whatever the client
                # said; the bytes actually arriving are the only honest count.
                await asyncio.to_thread(_write_chunk, fh, digest, chunk)

        if digest.hexdigest() != parsed.sha256:
            raise HTTPException(
                status_code=422, detail="sha256 mismatch: transfer corrupted"
            )

        sidecar = {
            **parsed.model_dump(mode="json", by_alias=True),
            "size_bytes": written,
            "started_at": started_at.isoformat(),
            "clock_suspect": clock_suspect,
            "received_at": datetime.now(settings.tz).isoformat(),
            "staged_as": audio_path.name,
        }
        await asyncio.to_thread(
            side_path.write_text,
            json.dumps(sidecar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Atomic, and only after the sidecar is on disk: the sweep can never
        # see a half-written file, and never sees audio without its metadata.
        await asyncio.to_thread(os.replace, tmp_path, audio_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    log.info(
        "staged recording %s from device %s (%d bytes)",
        audio_path.name,
        parsed.device_id,
        written,
    )
    return JSONResponse(
        {
            "status": "accepted",
            "sha256": parsed.sha256,
            "call_id": parsed.call_id,
            "staged_as": audio_path.name,
        },
        status_code=202,
    )


class RecordingProbeRequest(BaseModel):
    """A batch of "do you already have this?" — cheaper than an upload."""

    sha256: list[str] = Field(default_factory=list, max_length=200)
    call_id: list[str] = Field(default_factory=list, max_length=200)


@api.post("/recordings/probe", tags=["recordings"])
async def probe_recordings(
    body: RecordingProbeRequest, session: SessionDep
) -> dict[str, Any]:
    """Which of these recordings are already here, by hash or by call id.

    Anything named in the answer is dropped from the phone's queue without
    being uploaded — that is what makes backfilling a two-year archive over a
    3G link tractable, and what stops a lost 202 from costing 40 MB again. An
    empty batch is the app's connection test: it proves both reachability and
    the token, which /health alone cannot.
    """
    known_sha: set[str] = set()
    known_call: set[str] = set()

    if body.sha256:
        rows = await session.scalars(
            sa.select(Interaction.media["sha256"].astext)
            .where(Interaction.source == InteractionSource.phone_call)
            .where(Interaction.media["sha256"].astext.in_(body.sha256))
        )
        known_sha.update(r for r in rows if r)
        # Staged but not swept yet: the file is on disk with an interaction
        # still a minute away. Re-uploading it would be pure waste.
        staged = await asyncio.to_thread(
            _staged_hash_prefixes, Path(settings.call_recordings_dir)
        )
        known_sha.update(h for h in body.sha256 if h[:12] in staged)

    if body.call_id:
        rows = await session.scalars(
            sa.select(Interaction.media["call_id"].astext)
            .where(Interaction.source == InteractionSource.phone_call)
            .where(Interaction.media["call_id"].astext.in_(body.call_id))
        )
        known_call.update(r for r in rows if r)

    return {
        "known_sha256": sorted(known_sha),
        "known_call_id": sorted(known_call),
    }


def _staged_hash_prefixes(directory: Path) -> set[str]:
    """Hash prefixes of the recordings sitting on disk, read off their names.

    An upload is staged as `<start>-<sha256[:12]><ext>` — deterministic in its
    own content — so one directory listing is an index of everything that has
    arrived but not yet been swept. Only the prefix survives in the name, so
    the caller matches on `sha256[:12]`.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return set()
    return {
        name.rsplit(".", 1)[0].rsplit("-", 1)[-1]
        for name in names
        if Path(name).suffix.lower() in call_recordings.AUDIO_SUFFIXES
    }


app.include_router(api)
