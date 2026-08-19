"""Batch API extraction of userbot windows (spec §5, §9).

The Anthropic client is stubbed end to end; the window lifecycle, the retry
ladder and the rows that come out the other side are real.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import DebtDirection, InteractionSource, WindowStatus
from miya.services import batch
from miya.services import extraction as ex
from miya.services import usage as usage_costs

TZ = settings.tz
CHAT = -100999


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=800,
        output_tokens=120,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _api_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")
    )


DEBT_JSON = json.dumps(
    {
        "summary": "Akmal 5 mln oldi",
        "language": "uz",
        "debts": [
            {
                "direction": "they_owe_me",
                "person": "Akmal",
                "amount": 5000000,
                "currency": "UZS",
                "reason": "yuk uchun",
            }
        ],
    }
)


class _Batches:
    def __init__(self, owner: _StubClient) -> None:
        self._owner = owner

    async def create(self, *, requests):
        self._owner.submitted.append(requests)
        if self._owner.submit_error:
            raise self._owner.submit_error
        return SimpleNamespace(id=self._owner.batch_id)

    async def retrieve(self, batch_id):
        return SimpleNamespace(
            id=batch_id, processing_status=self._owner.processing_status
        )

    async def results(self, batch_id):
        entries = self._owner.results

        async def _gen():
            for entry in entries:
                yield entry

        return _gen()


class _StubClient:
    def __init__(self, *, batch_id="batch_1") -> None:
        self.messages = SimpleNamespace(batches=_Batches(self))
        self.batch_id = batch_id
        self.submitted: list[list[dict]] = []
        self.submit_error: Exception | None = None
        self.processing_status = "ended"
        self.results: list[SimpleNamespace] = []


def _succeeded(custom_id: str, body: str) -> SimpleNamespace:
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=body)], usage=_usage()
    )
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="succeeded", message=message),
    )


def _errored(custom_id: str, kind: str = "errored") -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind))


async def _window(session, text="[2026-08-18 10:00] [THEM (Akmal)] 5 mln oldim"):
    now = datetime.now(TZ)
    window = m.ConversationWindow(
        tg_chat_id=CHAT,
        started_at=now - timedelta(minutes=10),
        ended_at=now,
        message_count=1,
        char_count=len(text),
        text=text,
        status=WindowStatus.pending,
        custom_id=f"w-test-{now.timestamp()}",
    )
    session.add(window)
    await session.flush()
    return window


# --- submit ------------------------------------------------------------------


async def test_submit_sends_one_request_per_window(session, monkeypatch):
    window = await _window(session)
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)

    batch_id = await batch.submit_pending(session)

    assert batch_id == "batch_1"
    [requests] = stub.submitted
    assert len(requests) == 1
    assert requests[0]["custom_id"] == window.custom_id
    params = requests[0]["params"]
    assert params["model"] == settings.extract_model
    # Structured output travels with the batch request, same as real time.
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert window.text in params["messages"][0]["content"]

    assert window.status is WindowStatus.submitted
    assert window.batch_id == "batch_1"


async def test_a_submit_failure_leaves_windows_pending(session, monkeypatch):
    window = await _window(session)
    stub = _StubClient()
    stub.submit_error = _api_error()
    monkeypatch.setattr(batch, "get_client", lambda: stub)

    assert await batch.submit_pending(session) is None
    assert window.status is WindowStatus.pending
    assert window.batch_id is None


async def test_submitting_nothing_never_calls_the_api(session, monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    assert await batch.submit_pending(session) is None
    assert stub.submitted == []


# --- collect -----------------------------------------------------------------


async def test_a_finished_batch_lands_rows_and_costs(session, monkeypatch):
    window = await _window(session)
    member = m.Interaction(
        source=InteractionSource.telegram_userbot,
        tg_chat_id=CHAT,
        occurred_at=window.ended_at,
        raw_text="5 mln oldim",
        window_id=window.id,
    )
    session.add(member)
    await session.flush()

    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_succeeded(window.custom_id, DEBT_JSON)]

    outcome = await batch.collect_batch(session, "batch_1")
    await session.commit()

    assert (outcome.applied, outcome.retried, outcome.failed) == (1, 0, 0)
    assert window.status is WindowStatus.applied
    assert window.applied_at is not None

    debt = await session.scalar(sa.select(m.Debt))
    assert debt.amount == 5_000_000
    assert debt.direction is DebtDirection.they_owe_me

    # The debt hangs off a synthetic window interaction, and the member message
    # it came from is marked processed.
    owner = await session.get(m.Interaction, debt.source_interaction_id)
    assert owner.meta["kind"] == "window"
    await session.refresh(member)
    assert member.processed is True

    usage = await session.scalar(
        sa.select(m.UsageLog).where(m.UsageLog.operation == "extract_window")
    )
    assert usage.batch is True
    # Batch pricing is half of real time — that discount is the whole point.
    realtime = usage_costs.anthropic_cost_usd(
        settings.extract_model, input_tokens=800, output_tokens=120
    )
    assert usage.cost_usd == realtime / 2


async def test_a_batch_still_running_changes_nothing(session, monkeypatch):
    window = await _window(session)
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.processing_status = "in_progress"

    assert await batch.collect_batch(session, "batch_1") is None
    assert window.status is WindowStatus.submitted


async def test_an_errored_result_goes_back_in_the_queue(session, monkeypatch):
    window = await _window(session)
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_errored(window.custom_id, "expired")]

    outcome = await batch.collect_batch(session, "batch_1")

    assert outcome.retried == 1
    assert window.status is WindowStatus.pending
    assert window.attempts == 1
    assert window.batch_id is None


async def test_invalid_json_is_retried_rather_than_applied(session, monkeypatch):
    window = await _window(session)
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_succeeded(window.custom_id, "not json at all")]

    outcome = await batch.collect_batch(session, "batch_1")

    assert outcome.retried == 1
    assert window.status is WindowStatus.pending
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 0


async def test_a_window_the_api_never_answered_is_not_left_stuck(session, monkeypatch):
    window = await _window(session)
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = []  # the result stream simply omits it

    outcome = await batch.collect_batch(session, "batch_1")

    assert outcome.retried == 1
    assert window.status is WindowStatus.pending


async def test_repeated_batch_failures_fall_back_to_real_time(session, monkeypatch):
    window = await _window(session)
    window.attempts = settings.batch_max_attempts - 1
    await session.flush()

    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_errored(window.custom_id)]

    async def _fake_extract(text, *, now=None):
        return ex.ExtractionOutcome(
            result=ex.ExtractionResult(
                summary="fallback",
                debts=[
                    ex.ExtractedDebt(
                        direction="they_owe_me", person="Akmal", amount=1_000_000
                    )
                ],
            ),
            model=settings.extract_model,
            usage=_usage(),
        )

    monkeypatch.setattr(batch, "extract", _fake_extract)

    outcome = await batch.collect_batch(session, "batch_1")
    await session.commit()

    assert outcome.applied == 1
    assert window.status is WindowStatus.applied
    debt = await session.scalar(sa.select(m.Debt))
    assert debt.amount == 1_000_000
    fallback_usage = await session.scalar(
        sa.select(m.UsageLog).where(m.UsageLog.operation == "extract_window_fallback")
    )
    assert fallback_usage is not None


async def test_a_window_that_fails_everywhere_is_flagged_not_dropped(
    session, monkeypatch
):
    window = await _window(session, text="tushunarsiz matn")
    window.attempts = settings.batch_max_attempts - 1
    await session.flush()

    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_errored(window.custom_id)]

    async def _failing_extract(text, *, now=None):
        return ex.ExtractionOutcome(error="refusal", model=settings.extract_model)

    monkeypatch.setattr(batch, "extract", _failing_extract)

    outcome = await batch.collect_batch(session, "batch_1")
    await session.commit()

    assert outcome.failed == 1
    assert window.status is WindowStatus.failed
    flagged = await session.scalar(
        sa.select(m.Interaction).where(m.Interaction.needs_review.is_(True))
    )
    # The conversation text survives for the owner to read in /tekshir.
    assert flagged.raw_text == "tushunarsiz matn"


async def test_collect_submitted_walks_every_in_flight_batch(session, monkeypatch):
    first = await _window(session, text="birinchi oyna")
    stub = _StubClient(batch_id="batch_a")
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)

    second = await _window(session, text="ikkinchi oyna")
    stub.batch_id = "batch_b"
    await batch.submit_pending(session)

    stub.results = [
        _succeeded(first.custom_id, DEBT_JSON),
        _succeeded(second.custom_id, DEBT_JSON),
    ]

    outcome = await batch.collect_submitted(session)

    # Each batch only claims its own window, so two batches → two applications.
    assert outcome.applied == 2
    assert first.status is WindowStatus.applied
    assert second.status is WindowStatus.applied


async def test_an_interrupted_result_stream_never_double_applies(session, monkeypatch):
    """A results stream that dies mid-way must not re-extract what landed.

    The next poll replays the batch from the beginning; without filtering to
    still-submitted windows, every debt in the already-applied ones would be
    written a second time. Reproduced live before the fix (3 debts for 2
    windows).
    """
    first = await _window(session, text="birinchi oyna")
    second = await _window(session, text="ikkinchi oyna")

    class _Flaky(_StubClient):
        def __init__(self):
            super().__init__()
            self.fail_after: int | None = None

    stub = _Flaky()

    async def _results(batch_id):
        entries, fail_after = stub.results, stub.fail_after

        async def _gen():
            for index, entry in enumerate(entries):
                if fail_after is not None and index >= fail_after:
                    raise _api_error()
                yield entry

        return _gen()

    stub.messages.batches.results = _results
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)

    stub.results = [
        _succeeded(first.custom_id, DEBT_JSON),
        _succeeded(second.custom_id, DEBT_JSON),
    ]
    stub.fail_after = 1  # the stream dies right after the first result

    await batch.collect_batch(session, "batch_1")
    # The stream is drained *before* anything is applied (so the person
    # advisory lock is never held across network reads): a dead stream
    # therefore applies nothing and both windows stay safely submitted.
    assert first.status is WindowStatus.submitted
    assert second.status is WindowStatus.submitted
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 0

    stub.fail_after = None  # next poll tick: the stream works
    await batch.collect_submitted(session)
    await session.commit()

    assert first.status is WindowStatus.applied
    assert second.status is WindowStatus.applied
    # One debt per window — nothing was applied twice across the two polls.
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 2


async def test_a_batch_with_no_results_url_does_not_wedge_the_poller(
    session, monkeypatch
):
    """Repro: the SDK raises AnthropicError — the BASE class, not APIError.

    A batch older than 29 days (or a canceled one) still retrieves fine with
    processing_status="ended", but has no results_url. Catching only APIError
    let that escape collect_submitted, roll back everything applied in the
    same poll, and re-fire every 15 minutes forever with the windows never
    progressing.
    """
    window = await _window(session, text="eskirgan oyna")
    window.attempts = settings.batch_max_attempts - 1
    await session.flush()

    stub = _StubClient()

    async def _no_results(batch_id):
        raise anthropic.AnthropicError("No `results_url` for the given batch")

    stub.messages.batches.results = _no_results
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)

    async def _fake_extract(text, *, now=None):
        return ex.ExtractionOutcome(
            result=ex.ExtractionResult(summary="fallback"),
            model=settings.extract_model,
            usage=None,
        )

    monkeypatch.setattr(batch, "extract", _fake_extract)

    outcome = await batch.collect_batch(session, "batch_1")

    # It neither raised nor left the window stuck: the ladder took over.
    assert outcome.applied == 1
    assert window.status is WindowStatus.applied


async def test_one_unreadable_batch_does_not_roll_back_a_healthy_one(
    session, monkeypatch
):
    healthy = await _window(session, text="sog'lom oyna")
    stub = _StubClient(batch_id="batch_ok")
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_succeeded(healthy.custom_id, DEBT_JSON)]
    await batch.collect_batch(session, "batch_ok")
    await session.commit()

    broken = await _window(session, text="buzilgan oyna")
    stub.batch_id = "batch_bad"
    await batch.submit_pending(session)

    async def _explode(batch_id):
        if batch_id == "batch_bad":
            raise anthropic.AnthropicError("no results_url")
        return stub.results

    stub.messages.batches.results = _explode

    async def _failing_extract(text, *, now=None):
        return ex.ExtractionOutcome(error="refusal", model=settings.extract_model)

    monkeypatch.setattr(batch, "extract", _failing_extract)
    broken.attempts = settings.batch_max_attempts - 1
    await session.flush()

    await batch.collect_submitted(session)
    await session.commit()

    # The healthy batch's debt survived the broken one.
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 1
    assert healthy.status is WindowStatus.applied
    assert broken.status is WindowStatus.failed
