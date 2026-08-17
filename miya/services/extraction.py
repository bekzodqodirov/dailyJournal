"""Claude Haiku extraction: raw text in, validated structured facts out (spec §5).

The response shape is enforced by the API through structured outputs, so a
malformed payload is not a normal outcome. The retry and the ``needs_review``
fallback below cover what structured outputs cannot: transport errors, refusals,
and truncation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, field_validator

from miya.config import settings
from miya.services.prompts import EXTRACTION_SYSTEM_PROMPT

log = logging.getLogger(__name__)

CurrencyCode = Literal["UZS", "USD", "CNY", "KRW", "RUB"]

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _parse_iso_date(value: str | None) -> date | None:
    """Tolerant date parsing — a bad date is dropped, never allowed to fail a batch."""
    if not value:
        return None
    text = value.strip()
    if text.lower() in {"null", "none", "unknown", ""}:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        log.warning("unparseable due_date from extraction: %r", value)
        return None


def to_money(value: float | int | str) -> Decimal | None:
    """Convert an extracted amount to exact money.

    JSON Schema has no decimal type, so amounts arrive as JSON numbers. Going
    through ``str`` keeps the digits the model actually wrote instead of the
    binary float approximation.
    """
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


class ExtractedPerson(BaseModel):
    name: str
    context: str = ""


class ExtractedDebt(BaseModel):
    direction: Literal["they_owe_me", "i_owe_them"]
    person: str
    amount: float
    currency: CurrencyCode = "UZS"
    reason: str = ""
    due_date: str | None = None

    @property
    def due(self) -> date | None:
        return _parse_iso_date(self.due_date)


class ExtractedSettlement(BaseModel):
    person: str
    amount: float
    currency: CurrencyCode = "UZS"
    note: str = ""


class ExtractedPromise(BaseModel):
    made_by: Literal["me", "them"]
    person: str
    description: str
    due_date: str | None = None

    @property
    def due(self) -> date | None:
        return _parse_iso_date(self.due_date)


class ExtractedTransaction(BaseModel):
    type: Literal["income", "expense"]
    amount: float
    currency: CurrencyCode = "UZS"
    category: str = "other"
    description: str = ""
    counterparty: str | None = None


class ExtractedEvent(BaseModel):
    title: str
    start_at: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)

    def start(self, tz) -> datetime | None:
        """Parse `YYYY-MM-DDTHH:MM` (or a bare date) as local Tashkent time."""
        if not self.start_at:
            return None
        text = self.start_at.strip()
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=tz)
            except ValueError:
                continue
        parsed_date = _parse_iso_date(text)
        if parsed_date is None:
            return None
        return datetime.combine(parsed_date, datetime.min.time()).replace(tzinfo=tz)


class ExtractedTask(BaseModel):
    description: str
    due_date: str | None = None
    priority: Literal["low", "med", "high"] = "med"

    @property
    def due(self) -> date | None:
        return _parse_iso_date(self.due_date)


class ExtractionResult(BaseModel):
    """The exact schema of spec §5."""

    summary: str = ""
    language: Literal["uz", "ru", "en", "zh", "mixed"] = "mixed"
    people: list[ExtractedPerson] = Field(default_factory=list)
    debts: list[ExtractedDebt] = Field(default_factory=list)
    debt_settlements: list[ExtractedSettlement] = Field(default_factory=list)
    promises: list[ExtractedPromise] = Field(default_factory=list)
    transactions: list[ExtractedTransaction] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)
    tasks: list[ExtractedTask] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("summary", mode="before")
    @classmethod
    def _no_none_summary(cls, v):
        return v or ""

    def is_empty(self) -> bool:
        """True when nothing structured was found — only a summary is worth keeping."""
        return not any(
            (
                self.debts,
                self.debt_settlements,
                self.promises,
                self.transactions,
                self.events,
                self.tasks,
                self.facts,
            )
        )


class ExtractionOutcome(BaseModel):
    """What the pipeline hands back: the result, or the reason there isn't one."""

    result: ExtractionResult | None = None
    error: str | None = None
    model: str = ""
    usage: object | None = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def ok(self) -> bool:
        return self.result is not None


def build_user_content(text: str, *, now: datetime) -> str:
    """The volatile half of the prompt. Kept out of the cached system block."""
    return (
        f"CURRENT_DATE: {now.date().isoformat()}\n"
        f"CURRENT_DATETIME: {now.strftime('%Y-%m-%dT%H:%M')} ({settings.timezone})\n"
        f"---\n{text}"
    )


async def extract(text: str, *, now: datetime | None = None) -> ExtractionOutcome:
    """Run one extraction. Never raises — failures come back as `error`."""
    if not text or not text.strip():
        return ExtractionOutcome(result=ExtractionResult(), model=settings.extract_model)

    now = now or datetime.now(settings.tz)
    client = get_client()
    system = [
        {
            "type": "text",
            "text": EXTRACTION_SYSTEM_PROMPT,
            # Engages once the cached prefix clears the model's minimum (4096
            # tokens on Haiku 4.5); harmless and forward-looking below that.
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = [{"role": "user", "content": build_user_content(text, now=now)}]

    last_error: str | None = None
    for attempt in (1, 2):
        if attempt == 2:
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": "Your previous reply was not valid. Return ONLY a JSON "
                    "object matching the schema — no prose, no markdown fences.",
                },
            ]
        try:
            response = await client.messages.parse(
                model=settings.extract_model,
                max_tokens=4096,
                system=system,
                messages=messages,
                output_format=ExtractionResult,
            )
        except anthropic.APIError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("extraction attempt %d failed: %s", attempt, last_error)
            continue

        if response.stop_reason == "refusal":
            return ExtractionOutcome(
                error="refusal", model=settings.extract_model, usage=response.usage
            )
        if response.stop_reason == "max_tokens":
            last_error = "max_tokens: extraction truncated"
            log.warning("extraction attempt %d hit max_tokens", attempt)
            continue
        if response.parsed_output is None:
            last_error = "schema validation returned no object"
            continue

        return ExtractionOutcome(
            result=response.parsed_output,
            model=settings.extract_model,
            usage=response.usage,
        )

    return ExtractionOutcome(error=last_error or "unknown", model=settings.extract_model)
