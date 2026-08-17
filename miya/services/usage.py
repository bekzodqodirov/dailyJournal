"""Cost accounting for every paid API call (spec §9).

Prices are list rates in USD per million tokens, and per hour for audio. They
are only used to render `/xarajat`; the authoritative numbers are the token and
duration counts, which are recorded verbatim.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.models import UsageLog

log = logging.getLogger(__name__)

# USD per million tokens: (input, output). Cache reads bill at ~0.1x input,
# cache writes at ~1.25x.
MODEL_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
}

# ElevenLabs Scribe, USD per hour of audio.
SCRIBE_USD_PER_HOUR = Decimal("0.22")

_MILLION = Decimal(1_000_000)


def anthropic_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> Decimal | None:
    price = MODEL_PRICES.get(model)
    if price is None:
        return None
    in_price, out_price = price
    cost = (
        Decimal(input_tokens) * in_price
        + Decimal(output_tokens) * out_price
        + Decimal(cache_read_tokens) * in_price / 10
        + Decimal(cache_write_tokens) * in_price * Decimal("1.25")
    ) / _MILLION
    if batch:
        cost /= 2  # Message Batches API is half price.
    return cost.quantize(Decimal("0.000001"))


def scribe_cost_usd(seconds: float | Decimal) -> Decimal:
    hours = Decimal(str(seconds)) / Decimal(3600)
    return (hours * SCRIBE_USD_PER_HOUR).quantize(Decimal("0.000001"))


async def record_anthropic_usage(
    session: AsyncSession,
    *,
    model: str,
    operation: str,
    usage: object,
    batch: bool = False,
    source_interaction_id: int | None = None,
) -> None:
    """Persist one Anthropic call. `usage` is the SDK's response.usage object."""
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    session.add(
        UsageLog(
            provider="anthropic",
            model=model,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            batch=batch,
            cost_usd=anthropic_cost_usd(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                batch=batch,
            ),
            source_interaction_id=source_interaction_id,
        )
    )
    if cache_read == 0 and cache_write == 0:
        # Worth noticing: below a model's minimum cacheable prefix, caching is
        # silently skipped rather than reported as an error.
        log.debug("no prompt cache activity on %s/%s", model, operation)


async def record_transcription_usage(
    session: AsyncSession,
    *,
    provider: str,
    model: str | None,
    seconds: float,
    source_interaction_id: int | None = None,
) -> None:
    session.add(
        UsageLog(
            provider=provider,
            model=model,
            operation="transcribe",
            audio_seconds=Decimal(str(round(seconds, 2))),
            cost_usd=scribe_cost_usd(seconds) if provider == "elevenlabs" else None,
            source_interaction_id=source_interaction_id,
        )
    )
