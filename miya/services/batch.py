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

Nothing is *written* silently either. Every applied window comes back as an
``AppliedWindow`` carrying what actually landed, and anything the owner cares
about (a debt, a settlement, a promise, a transaction, an event, a task) is
queued as a notice on the window's interaction until the worker has told him
where it came from. The owner decided nothing lands from a chat without him
being told; facts-only windows are not worth a ping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.bot import notices
from miya.config import settings
from miya.db.enums import Direction, InteractionSource, WindowStatus
from miya.db.models import ChatMonitor, ConversationWindow, Interaction, Person
from miya.services import usage as usage_service
from miya.services.extraction import (
    API_FAILURES,
    ExtractionResult,
    build_request_params,
    extract,
    parse_extraction_json,
)
from miya.services.persistence import Applied, apply_extraction
from miya.services.windows import pending_windows

log = logging.getLogger(__name__)

# One batch may hold up to 100k requests; this cap keeps a single submit small
# enough to retry cheaply and keeps the JSON body well under the size limit.
MAX_REQUESTS_PER_BATCH = 500

_TERMINAL_STATUS = "ended"

# Keys on the window interaction's metadata that hold the owner notice: the
# rendered receipt waiting to go out, and when it went. Metadata rather than a
# column so that a worker restart, or eight hours of quiet hours, delays the
# notice instead of losing it.
NOTICE_KEY = "notice"
NOTIFIED_KEY = "notified_at"


@dataclass(slots=True)
class AppliedWindow:
    """One window's landed rows, plus where the conversation came from.

    ``chat_title`` is the monitor's title (None for a chat the userbot has not
    synced yet). ``people`` names everyone the landed rows point at — in a
    group that is the person who actually owes, not whoever spoke first.
    """

    window: ConversationWindow
    interaction: Interaction
    applied: Applied
    chat_title: str | None = None
    people: list[str] = field(default_factory=list)
    # The receipt parked for the owner; None when there was nothing worth
    # telling (facts only, or nothing at all).
    notice: str | None = None


@dataclass(slots=True)
class BatchOutcome:
    retried: int = 0
    failed: int = 0
    windows: list[AppliedWindow] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return len(self.windows)

    def absorb(self, other: BatchOutcome) -> None:
        self.retried += other.retried
        self.failed += other.failed
        self.windows.extend(other.windows)


@dataclass(slots=True)
class QueuedNotice:
    """A receipt waiting for the owner, read back from the interaction."""

    interaction: Interaction
    text: str
    chat: str
    counts: dict[str, int]


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
    except API_FAILURES as exc:
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
) -> AppliedWindow:
    interaction = await _window_interaction(session, window)
    applied = await apply_extraction(session, interaction, result)
    await _mark_members_processed(session, window)
    window.status = WindowStatus.applied
    window.applied_at = datetime.now(settings.tz)
    landed = AppliedWindow(window=window, interaction=interaction, applied=applied)
    # Parked here, in the same transaction as the rows, rather than by the
    # worker afterwards: the per-batch commit in collect_submitted would
    # otherwise leave a gap where the debt is durable and the receipt is not,
    # and the rollback of a later broken batch expires every object in the
    # session, so nothing rendered after it could be trusted.
    #
    # Everything the receipt needs — the chat title, the names — is looked
    # up inside the guard: a receipt must never cost the ledger, and the
    # rows are already in.
    try:
        landed.chat_title = await session.scalar(
            sa.select(ChatMonitor.title).where(
                ChatMonitor.tg_chat_id == window.tg_chat_id
            )
        )
        landed.people = await _people_named(session, window, applied)
        landed.notice = queue_notice(landed)
    except Exception:
        log.exception("could not park the owner notice for window %s", window.id)
    return landed


async def _people_named(
    session: AsyncSession, window: ConversationWindow, applied: Applied
) -> list[str]:
    """Display names behind the landed rows, first mention first.

    Falls back to the window's own person, so a private chat is still named
    when the extraction only produced, say, an event with no counterparty.
    """
    ids: list[int] = [d.person_id for d in applied.debts]
    ids += [p.person_id for p in applied.promises]
    ids += [t.counterparty_person_id for t in applied.transactions]
    ids += [person.id for person, _ in applied.settlements]
    if not any(ids) and window.person_id:
        ids.append(window.person_id)
    wanted = [i for i in dict.fromkeys(ids) if i]
    names: dict[int, str] = {}
    if wanted:
        rows = await session.execute(
            sa.select(Person.id, Person.display_name).where(Person.id.in_(wanted))
        )
        names = dict(rows.all())
    ordered = [names[i] for i in wanted if i in names]
    # Settlements that matched nothing carry the name, not the row.
    ordered += [name for name, _, _ in applied.unmatched_settlements]
    ordered += [name for name, _, _ in applied.ambiguous_settlements]
    return list(dict.fromkeys(ordered))


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
        applied = await _apply_result(session, window, outcome.result)
        log.info("window %s extracted in real time after batch failures", window.id)
        return BatchOutcome(windows=[applied])

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
        #
        # But not forever: a stream that fails on every poll would otherwise
        # wedge these windows in `submitted` for good. After the same number
        # of attempts the rest of the ladder allows, they go through it and
        # end up extracted in real time.
        log.warning("streaming results of batch %s failed: %s", batch_id, exc)
        for window in windows.values():
            # Counted against the same ladder the rest of the failure modes
            # use. Below the cap the window simply stays submitted and the
            # next poll re-reads the batch — the results are already paid for.
            # At the cap it goes through the ladder and ends up extracted in
            # real time, so a permanently unreadable stream cannot wedge a
            # conversation in `submitted` for good.
            window.attempts += 1
            if window.attempts >= settings.batch_max_attempts:
                # _retry_or_fallback bumps attempts itself; undo this one so
                # the cap means the same number of tries here as everywhere.
                window.attempts -= 1
                outcome.absorb(
                    await _retry_or_fallback(session, window, "stream_unreadable")
                )
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
            outcome.absorb(
                await _retry_or_fallback(session, window, "results_unavailable")
            )
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
                partial = BatchOutcome(
                    windows=[await _apply_result(session, window, parsed)]
                )
        outcome.absorb(partial)

    # Results are only ever missing if the API omitted a request we sent —
    # treat that like any other failure rather than leaving the window stuck.
    for window in windows.values():
        outcome.absorb(await _retry_or_fallback(session, window, "missing_result"))

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
        total.absorb(outcome)
    return total


# --- owner notices -----------------------------------------------------------


def queue_notice(landed: AppliedWindow, *, now: datetime | None = None) -> str | None:
    """Park the receipt for a window on its interaction until it is sent.

    Returns the text, or None for a window with nothing worth telling — the
    owner tolerates twenty-odd confirmations a day, not a ping for every
    remembered detail, so facts-only windows stay quiet.

    The text is stored ready to send rather than rebuilt later: a settlement
    that matched no open debt, or one the owner has to disambiguate, exists
    only in the ``Applied`` of the moment — there is no row to rebuild it from.
    """
    if landed.applied.is_empty():
        return None
    now = now or datetime.now(settings.tz)
    chat, _ = notices.source_of(landed.chat_title, landed.people)
    text = notices.window_notice(
        chat_title=landed.chat_title,
        people=landed.people,
        ended_at=landed.window.ended_at,
        applied=landed.applied,
    )
    interaction = landed.interaction
    interaction.meta = {
        **(interaction.meta or {}),
        NOTICE_KEY: {
            "text": text,
            "chat": chat,
            "counts": notices.counts_of(landed.applied),
            "queued_at": now.isoformat(),
        },
    }
    return text


async def pending_notices(session: AsyncSession) -> list[QueuedNotice]:
    """Receipts queued but not yet delivered, oldest conversation first."""
    rows = await session.scalars(
        sa.select(Interaction)
        .where(Interaction.meta.has_key(NOTICE_KEY))
        .where(sa.not_(Interaction.meta.has_key(NOTIFIED_KEY)))
        .order_by(Interaction.occurred_at, Interaction.id)
    )
    queued: list[QueuedNotice] = []
    for interaction in rows:
        notice: dict[str, Any] = (interaction.meta or {}).get(NOTICE_KEY) or {}
        queued.append(
            QueuedNotice(
                interaction=interaction,
                text=str(notice.get("text") or ""),
                chat=str(notice.get("chat") or ""),
                counts={k: int(v) for k, v in (notice.get("counts") or {}).items()},
            )
        )
    return queued


def mark_notified(interaction: Interaction, *, now: datetime | None = None) -> None:
    """Record delivery. The receipt stays on the row for the record."""
    now = now or datetime.now(settings.tz)
    interaction.meta = {**(interaction.meta or {}), NOTIFIED_KEY: now.isoformat()}
