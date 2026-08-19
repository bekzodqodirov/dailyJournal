"""Message Batches extraction for the userbot stream (spec §5, §9).

The userbot produces far more text than the assistant bot, and none of it is
urgent — nobody is waiting for a reply. So its windows go through Anthropic's
Message Batches API at half price, submitted every ``BATCH_FLUSH_HOURS`` and
collected when the job ends.

Failure policy, in one place:
  * a submit failure leaves windows ``pending`` — retried on the next tick;
  * an errored/expired/canceled result increments ``attempts`` and returns the
    window to ``pending``;
  * after ``BATCH_MAX_ATTEMPTS`` the window is extracted in real time instead,
    and only if that also fails does it end up ``failed`` + ``needs_review``.
Nothing is ever dropped silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import anthropic
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource, WindowStatus
from miya.db.models import ConversationWindow, Interaction
from miya.services import usage as usage_service
from miya.services.extraction import (
    ExtractionResult,
    build_request_params,
    extract,
    parse_extraction_json,
)
from miya.services.persistence import apply_extraction
from miya.services.windows import pending_windows

log = logging.getLogger(__name__)

# One batch may hold up to 100k requests; this cap keeps a single submit small
# enough to retry cheaply and keeps the JSON body well under the size limit.
MAX_REQUESTS_PER_BATCH = 500

_TERMINAL_STATUS = "ended"


@dataclass(slots=True)
class BatchOutcome:
    applied: int = 0
    retried: int = 0
    failed: int = 0


def get_client() -> anthropic.AsyncAnthropic:
    # Imported lazily through extraction so both paths share one client and
    # tests can monkeypatch a single place.
    from miya.services.extraction import get_client as _get_client

    return _get_client()


async def submit_pending(
    session: AsyncSession, *, now: datetime | None = None
) -> str | None:
    """Send every pending window as one batch. Returns the batch id, if any."""
    windows = await pending_windows(session, limit=MAX_REQUESTS_PER_BATCH)
    if not windows:
        return None

    now = now or datetime.now(settings.tz)
    requests = [
        {
            "custom_id": window.custom_id,
            # Windows are historical: date them by when the conversation ended,
            # so "ertaga" inside an old window still resolves correctly.
            "params": build_request_params(
                window.text, now=window.ended_at.astimezone(settings.tz)
            ),
        }
        for window in windows
    ]

    try:
        batch = await get_client().messages.batches.create(requests=requests)
    except anthropic.APIError as exc:
        # Windows stay pending; the next tick retries the whole set.
        log.warning(
            "batch submit failed (%d windows stay pending): %s", len(windows), exc
        )
        return None

    for window in windows:
        window.status = WindowStatus.submitted
        window.batch_id = batch.id
        window.submitted_at = now
    await session.flush()
    log.info("submitted batch %s with %d window(s)", batch.id, len(windows))
    return batch.id


async def _window_interaction(
    session: AsyncSession, window: ConversationWindow
) -> Interaction:
    """The synthetic interaction that owns a window's extracted rows.

    Derived rows hang off ``source_interaction_id``, so a window needs one row
    of its own; the member messages keep their raw text and stay linked to the
    window through ``window_id``.
    """
    interaction = Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.na,
        person_id=window.person_id,
        tg_chat_id=window.tg_chat_id,
        occurred_at=window.ended_at,
        raw_text=window.text,
        meta={
            "kind": "window",
            "window_id": window.id,
            "message_count": window.message_count,
        },
        window_id=window.id,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _apply_result(
    session: AsyncSession, window: ConversationWindow, result: ExtractionResult
) -> None:
    interaction = await _window_interaction(session, window)
    await apply_extraction(session, interaction, result)
    await _mark_members_processed(session, window)
    window.status = WindowStatus.applied
    window.applied_at = datetime.now(settings.tz)


async def _mark_members_processed(
    session: AsyncSession, window: ConversationWindow
) -> None:
    await session.execute(
        sa.update(Interaction)
        .where(Interaction.window_id == window.id)
        .values(processed=True)
    )


async def _fail_window(
    session: AsyncSession, window: ConversationWindow, reason: str
) -> None:
    """Last resort: keep the text, flag it, and stop spending money on it."""
    interaction = await _window_interaction(session, window)
    interaction.needs_review = True
    interaction.meta = {**(interaction.meta or {}), "error": reason}
    window.status = WindowStatus.failed
    log.error(
        "window %s gave up after %d attempt(s): %s",
        window.id,
        window.attempts,
        reason,
    )


async def _retry_or_fallback(
    session: AsyncSession, window: ConversationWindow, reason: str
) -> BatchOutcome:
    """Bump the attempt counter; fall back to real-time once batches give up."""
    window.attempts += 1
    window.batch_id = None
    window.submitted_at = None

    if window.attempts < settings.batch_max_attempts:
        window.status = WindowStatus.pending
        log.warning(
            "window %s returned %s; retrying in the next batch (attempt %d)",
            window.id,
            reason,
            window.attempts,
        )
        return BatchOutcome(retried=1)

    # Batches keep failing for this window — pay full price rather than lose it.
    outcome = await extract(window.text, now=window.ended_at.astimezone(settings.tz))
    if outcome.usage is not None:
        await usage_service.record_anthropic_usage(
            session,
            model=outcome.model,
            operation="extract_window_fallback",
            usage=outcome.usage,
        )
    if outcome.ok:
        await _apply_result(session, window, outcome.result)
        log.info("window %s extracted in real time after batch failures", window.id)
        return BatchOutcome(applied=1)

    await _fail_window(session, window, f"{reason}; fallback: {outcome.error}")
    return BatchOutcome(failed=1)


def _result_text(message) -> str:
    return "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )


async def collect_batch(session: AsyncSession, batch_id: str) -> BatchOutcome | None:
    """Apply one finished batch. None means it is still running."""
    client = get_client()
    try:
        batch = await client.messages.batches.retrieve(batch_id)
    except anthropic.AnthropicError as exc:
        # The base class, so this covers both transport failures and the SDK's
        # own errors; either way the batch is simply read again next tick.
        log.warning("could not retrieve batch %s: %s", batch_id, exc)
        return None

    if batch.processing_status != _TERMINAL_STATUS:
        log.debug("batch %s still %s", batch_id, batch.processing_status)
        return None

    # Only windows still awaiting their result. A result stream that dies
    # half way leaves some windows applied and the rest submitted, and the
    # next poll replays the stream from the beginning — without this filter
    # the already-applied ones would be extracted and persisted a second
    # time, duplicating every debt in them.
    windows = {
        w.custom_id: w
        for w in await session.scalars(
            sa.select(ConversationWindow)
            .where(ConversationWindow.batch_id == batch_id)
            .where(ConversationWindow.status == WindowStatus.submitted)
        )
    }
    if not windows:
        return BatchOutcome()

    outcome = BatchOutcome()
    try:
        # Drain the stream *before* touching the database. Applying a window
        # takes the person-resolution advisory lock, and a lock held while the
        # network dribbles the rest of the stream in would block the bot and
        # the call-scan from creating people for the whole download.
        results = await client.messages.batches.results(batch_id)
        entries = [entry async for entry in results]
    except anthropic.APIError as exc:
        # Transport trouble. The batch still holds these windows' results and
        # they are already paid for, so leave them `submitted` and re-read the
        # same batch next tick rather than paying to extract them again.
        log.warning("streaming results of batch %s failed: %s", batch_id, exc)
        await session.flush()
        return outcome
    except anthropic.AnthropicError as exc:
        # AnthropicError is the *base* class, and the SDK raises it bare when a
        # batch has no results_url — what a batch older than 29 days, or a
        # canceled one, looks like. `except APIError` never caught it, so it
        # escaped the poll, rolled back every batch applied alongside it, and
        # re-fired every 15 minutes forever with the windows never moving.
        #
        # Unlike a transport error this never recovers, so the windows go
        # through the normal ladder and end up extracted in real time.
        log.warning("results of batch %s are unavailable: %s", batch_id, exc)
        for window in windows.values():
            partial = await _retry_or_fallback(session, window, "results_unavailable")
            outcome.applied += partial.applied
            outcome.retried += partial.retried
            outcome.failed += partial.failed
        await session.flush()
        return outcome

    for entry in entries:
        window = windows.pop(entry.custom_id, None)
        if window is None:
            continue
        kind = entry.result.type
        if kind != "succeeded":
            partial = await _retry_or_fallback(session, window, kind)
        else:
            message = entry.result.message
            await usage_service.record_anthropic_usage(
                session,
                model=settings.extract_model,
                operation="extract_window",
                usage=message.usage,
                batch=True,
            )
            parsed = parse_extraction_json(_result_text(message))
            if parsed is None:
                partial = await _retry_or_fallback(session, window, "invalid_json")
            else:
                await _apply_result(session, window, parsed)
                partial = BatchOutcome(applied=1)
        outcome.applied += partial.applied
        outcome.retried += partial.retried
        outcome.failed += partial.failed

    # Results are only ever missing if the API omitted a request we sent —
    # treat that like any other failure rather than leaving the window stuck.
    for window in windows.values():
        partial = await _retry_or_fallback(session, window, "missing_result")
        outcome.retried += partial.retried
        outcome.applied += partial.applied
        outcome.failed += partial.failed

    await session.flush()
    log.info(
        "batch %s collected: %d applied, %d retried, %d failed",
        batch_id,
        outcome.applied,
        outcome.retried,
        outcome.failed,
    )
    return outcome


async def collect_submitted(session: AsyncSession) -> BatchOutcome:
    """Poll every in-flight batch and apply the ones that finished."""
    batch_ids = list(
        await session.scalars(
            sa.select(ConversationWindow.batch_id)
            .where(ConversationWindow.status == WindowStatus.submitted)
            .where(ConversationWindow.batch_id.isnot(None))
            .distinct()
        )
    )
    total = BatchOutcome()
    for batch_id in batch_ids:
        try:
            outcome = await collect_batch(session, batch_id)
        except Exception:
            # One unreadable batch must not cost the batches already applied
            # in this sweep — the surrounding session_scope would roll them
            # all back together.
            log.exception("collecting batch %s failed; rolling back only it", batch_id)
            await session.rollback()
            continue
        # Each batch is its own unit of paid work: commit it before touching
        # the next one.
        await session.commit()
        if outcome is None:
            continue
        total.applied += outcome.applied
        total.retried += outcome.retried
        total.failed += outcome.failed
    return total
