"""Claude Haiku extraction: raw text in, validated structured facts out (spec §5).

The response shape is normally enforced by the API through structured outputs.
That can fail on the server before the model is ever sampled — a schema this
size can exceed the grammar compiler's time budget — so there is a second,
prompted-JSON path that carries the schema in the system block instead. The
retry and the ``needs_review`` fallback cover the rest: transport errors,
refusals, and truncation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Literal

import anthropic
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from miya.config import settings
from miya.services.prompts import EXTRACTION_SYSTEM_PROMPT

log = logging.getLogger(__name__)

CurrencyCode = Literal["UZS", "USD", "CNY", "KRW", "RUB"]

_client: anthropic.AsyncAnthropic | None = None


class AnthropicUnavailable(RuntimeError):
    """Anthropic cannot be called at all — typically no API key configured."""


# What every caller must treat as "the model is not reachable right now" and
# fall back from. AnthropicError is the SDK's *base* class, so this covers
# transport failures, HTTP errors and the SDK's own bare errors alike.
API_FAILURES = (anthropic.AnthropicError, AnthropicUnavailable)


def get_client() -> anthropic.AsyncAnthropic:
    """The shared Anthropic client.

    Raises `AnthropicUnavailable` when no key is configured. That check lives
    here rather than at each call site because the SDK's own failure is a bare
    ``TypeError`` raised deep inside header building — which none of the
    fallback paths expect. A blank or mistyped ANTHROPIC_API_KEY therefore
    took the daily report down entirely instead of degrading to its
    deterministic data block. Failing here, with a type every caller already
    handles, turns a misconfiguration into the same branch as an outage.
    """
    if not settings.anthropic_api_key.strip():
        raise AnthropicUnavailable("ANTHROPIC_API_KEY is not set")
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
    # Which side was repaid. The owner can have open debts in both directions
    # with the same person, and paying down the wrong one silently inverts his
    # books — so this is asked for explicitly, and left null when the text
    # genuinely does not say (the persistence layer then refuses to guess).
    direction: Literal["they_owe_me", "i_owe_them"] | None = None


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


MAX_EXTRACTION_TOKENS = 4096


# Set once the API tells us it cannot compile this schema into a sampling
# grammar ("Grammar compilation timed out"). That is a server-side failure
# decided before the model runs, so retrying the identical request cannot
# help and every later extraction would pay the same latency to fail the same
# way. Remembering it turns a hard failure into one degraded-but-working
# request. It is deliberately not reset: a process restart re-enables
# structured outputs, which is the right cadence for a platform-side limit.
_structured_output_unusable = False


def structured_output_available() -> bool:
    return not _structured_output_unusable


def note_grammar_failure(exc: Exception) -> None:
    global _structured_output_unusable
    if not _structured_output_unusable:
        log.warning(
            "structured outputs unavailable for this schema (%s); "
            "falling back to prompted JSON for the rest of this process",
            exc,
        )
    _structured_output_unusable = True


def is_grammar_failure(exc: Exception) -> bool:
    """Did the server refuse the *schema* rather than the request?"""
    return isinstance(exc, anthropic.BadRequestError) and "grammar" in str(exc).lower()


@lru_cache(maxsize=1)
def _schema_instructions() -> str:
    """The schema as prompt text, for when the grammar path is unavailable.

    Generated from the same model the structured path uses, so the two can
    never drift apart.
    """
    schema = TypeAdapter(ExtractionResult).json_schema()
    return (
        "\n\nReturn a JSON object valid against this JSON Schema. "
        "Emit the object only — no prose, no markdown fences.\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def extraction_system_block(*, with_schema: bool = False) -> list[dict]:
    """The cached system block shared by the real-time and batch paths.

    ``with_schema`` appends the JSON Schema for the prompted fallback. Both
    variants stay byte-stable, so each keeps its own cache entry.
    """
    text = EXTRACTION_SYSTEM_PROMPT
    if with_schema:
        text += _schema_instructions()
    return [
        {
            "type": "text",
            "text": text,
            # Engages once the cached prefix clears the model's minimum (4096
            # tokens on Haiku 4.5); harmless and forward-looking below that.
            "cache_control": {"type": "ephemeral"},
        }
    ]


@lru_cache(maxsize=1)
def extraction_output_config() -> dict:
    """Structured-output config for ``ExtractionResult``.

    ``messages.parse`` builds this internally from ``output_format``; the Batch
    API takes raw params, so the batch path builds the identical object here
    rather than hand-writing a second copy of the schema.
    """
    schema = TypeAdapter(ExtractionResult).json_schema()
    return {"format": {"type": "json_schema", "schema": transform_schema(schema)}}


def build_request_params(text: str, *, now: datetime) -> dict:
    """One extraction request, in Messages API shape (used by the batch path).

    The batch path reads its results with ``parse_extraction_json`` either way,
    so dropping ``output_config`` once the grammar is known to be uncompilable
    costs nothing and keeps a whole batch from failing on a schema the server
    has already rejected.
    """
    structured = structured_output_available()
    params = {
        "model": settings.extract_model,
        "max_tokens": MAX_EXTRACTION_TOKENS,
        "system": extraction_system_block(with_schema=not structured),
        "messages": [{"role": "user", "content": build_user_content(text, now=now)}],
    }
    if structured:
        params["output_config"] = extraction_output_config()
    return params


def parse_extraction_json(text: str) -> ExtractionResult | None:
    """Validate a model-written JSON body. None means it was not usable.

    Used by the batch path and by the prompted-JSON fallback — anywhere the
    API did not already constrain the output to the schema.
    """
    try:
        return ExtractionResult.model_validate_json(text)
    except ValidationError as exc:
        log.warning("extraction returned an invalid object: %s", exc)
        return None


class UsageTally:
    """Tokens billed across every attempt of one extraction (spec §9).

    A retried extraction is two paid calls, not one: an attempt that hits
    ``max_tokens`` or fails validation is still charged. Reporting only the
    last attempt would quietly understate what MIYA costs, so the attempts are
    summed and recorded as a single usage row.
    """

    __slots__ = (
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "calls",
        "input_tokens",
        "output_tokens",
    )

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0
        self.calls = 0

    def add(self, usage: object) -> None:
        if usage is None:
            return
        self.calls += 1
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            setattr(self, field, getattr(self, field) + (getattr(usage, field, 0) or 0))

    def or_none(self) -> UsageTally | None:
        return self if self.calls else None


@dataclass(slots=True)
class _Attempt:
    """One request's outcome, normalised across the two response shapes."""

    usage: object
    stop_reason: str | None
    parsed: ExtractionResult | None


def _text_of(response) -> str:
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


async def _one_attempt(client, messages: list[dict]) -> _Attempt:
    """Ask once, by whichever route is currently usable.

    Prefers structured outputs. If the server rejects the *schema* rather than
    the request, it retries the same turn as prompted JSON and remembers the
    rejection, so this costs one wasted call per process rather than one per
    message.
    """
    if structured_output_available():
        try:
            response = await client.messages.parse(
                model=settings.extract_model,
                max_tokens=MAX_EXTRACTION_TOKENS,
                system=extraction_system_block(),
                messages=messages,
                output_format=ExtractionResult,
            )
        except API_FAILURES as exc:
            if not is_grammar_failure(exc):
                raise
            note_grammar_failure(exc)
        else:
            return _Attempt(response.usage, response.stop_reason, response.parsed_output)

    response = await client.messages.create(
        model=settings.extract_model,
        max_tokens=MAX_EXTRACTION_TOKENS,
        system=extraction_system_block(with_schema=True),
        messages=messages,
    )
    return _Attempt(
        response.usage, response.stop_reason, parse_extraction_json(_text_of(response))
    )


async def extract(text: str, *, now: datetime | None = None) -> ExtractionOutcome:
    """Run one extraction. Never raises — failures come back as `error`."""
    if not text or not text.strip():
        return ExtractionOutcome(result=ExtractionResult(), model=settings.extract_model)

    now = now or datetime.now(settings.tz)
    try:
        client = get_client()
    except API_FAILURES as exc:
        # Documented above as never raising, and every caller relies on that to
        # reach its needs_review fallback — so an unconfigured key has to come
        # back as an outcome, not as an exception thrown past them.
        return ExtractionOutcome(
            error=f"{type(exc).__name__}: {exc}", model=settings.extract_model
        )
    messages = [{"role": "user", "content": build_user_content(text, now=now)}]

    tally = UsageTally()
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
            got = await _one_attempt(client, messages)
        except API_FAILURES as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("extraction attempt %d failed: %s", attempt, last_error)
            continue

        tally.add(got.usage)

        if got.stop_reason == "refusal":
            return ExtractionOutcome(
                error="refusal", model=settings.extract_model, usage=tally.or_none()
            )
        if got.stop_reason == "max_tokens":
            last_error = "max_tokens: extraction truncated"
            log.warning("extraction attempt %d hit max_tokens", attempt)
            continue
        if got.parsed is None:
            last_error = "schema validation returned no object"
            continue

        return ExtractionOutcome(
            result=got.parsed,
            model=settings.extract_model,
            usage=tally.or_none(),
        )

    return ExtractionOutcome(
        error=last_error or "unknown",
        model=settings.extract_model,
        # Two failed-but-billed attempts still cost money; record them.
        usage=tally.or_none(),
    )
