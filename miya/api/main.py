"""Internal FastAPI app (spec §11). Bound to localhost — never exposed publicly."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from miya import __version__
from miya.api.deps import require_token
from miya.config import settings
from miya.db.enums import Currency, DebtDirection, DebtStatus
from miya.db.models import Debt, DebtPayment, Person
from miya.db.session import engine, get_session
from miya.services import planner, queries, rag, reports
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
) -> dict[str, Any]:
    balances = await queries.open_debts(session, direction=direction, person_id=person_id)
    return {"debts": [_balance_json(b) for b in balances]}


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
    session: SessionDep, person_id: int | None = None
) -> dict[str, Any]:
    items = await queries.open_promises(session, person_id=person_id)
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


app.include_router(api)
