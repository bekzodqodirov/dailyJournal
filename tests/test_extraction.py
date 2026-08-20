"""Extraction schema and pipeline. The Anthropic client is stubbed — no network."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import pytest

from miya.config import settings
from miya.services import extraction as ex

TZ = ZoneInfo("Asia/Tashkent")


# --- pure schema behaviour --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5000000, Decimal("5000000.00")),
        (5000000.0, Decimal("5000000.00")),
        (1234.56, Decimal("1234.56")),
        ("2500", Decimal("2500.00")),
    ],
)
def test_amounts_convert_to_exact_money(raw, expected):
    """JSON has no decimal type; going via str keeps the digits the model wrote."""
    assert ex.to_money(raw) == expected


@pytest.mark.parametrize("raw", [0, -5, "abc", None])
def test_non_positive_or_garbage_amounts_are_rejected(raw):
    assert ex.to_money(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-25", date(2026, 8, 25)),
        ("2026-08-25T10:00", date(2026, 8, 25)),
        (None, None),
        ("null", None),
        ("", None),
        ("ertaga", None),
        ("25/08/2026", None),
    ],
)
def test_due_dates_are_parsed_leniently(raw, expected):
    debt = ex.ExtractedDebt(
        direction="they_owe_me", person="Akmal", amount=1, due_date=raw
    )
    assert debt.due == expected


def test_event_start_is_interpreted_as_local_tashkent_time():
    event = ex.ExtractedEvent(title="Uchrashuv", start_at="2026-08-25T14:30")
    start = event.start(TZ)
    assert start == datetime(2026, 8, 25, 14, 30, tzinfo=TZ)


def test_event_with_a_bare_date_starts_at_midnight():
    event = ex.ExtractedEvent(title="Reys", start_at="2026-08-25")
    assert event.start(TZ) == datetime(2026, 8, 25, 0, 0, tzinfo=TZ)


def test_event_without_a_time_has_no_start():
    assert ex.ExtractedEvent(title="Bir kun").start(TZ) is None


def test_empty_result_is_detected():
    assert ex.ExtractionResult(summary="salom").is_empty()
    assert not ex.ExtractionResult(facts=["Akmal — yetkazib beruvchi"]).is_empty()


def test_defaults_match_the_spec_schema():
    result = ex.ExtractionResult()
    assert result.language == "mixed"
    assert result.debts == [] and result.tags == []
    assert (
        ex.ExtractedDebt(direction="i_owe_them", person="X", amount=1).currency == "UZS"
    )
    assert ex.ExtractedTask(description="X").priority == "med"


# --- prompt construction ----------------------------------------------------


def test_current_date_goes_in_the_user_turn_not_the_cached_system_prompt():
    """A date in the system prompt would invalidate the cache on every request."""
    now = datetime(2026, 8, 17, 19, 30, tzinfo=TZ)
    content = ex.build_user_content("Akmalga 5 mln berdim", now=now)
    assert "CURRENT_DATE: 2026-08-17" in content
    assert "2026-08-17T19:30" in content
    assert "Akmalga 5 mln berdim" in content

    from miya.services.prompts import EXTRACTION_SYSTEM_PROMPT

    assert "2026" not in EXTRACTION_SYSTEM_PROMPT


# --- pipeline with a stubbed client -----------------------------------------


class _StubMessages:
    def __init__(self, responses, created=()):
        self._responses = list(responses)
        self._created = list(created)
        self.calls: list[dict] = []
        self.create_calls: list[dict] = []

    @staticmethod
    def _next(queue):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._next(self._responses)

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._next(self._created)


class _StubClient:
    def __init__(self, responses, created=()):
        self.messages = _StubMessages(responses, created)


def _response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def stub(monkeypatch):
    def _install(responses, created=()):
        client = _StubClient(responses, created)
        monkeypatch.setattr(ex, "get_client", lambda: client)
        return client

    return _install


@pytest.fixture(autouse=True)
def _structured_output_enabled(monkeypatch):
    """The grammar flag is process-wide; keep it from leaking between tests."""
    monkeypatch.setattr(ex, "_structured_output_unusable", False)


async def test_blank_input_short_circuits_without_calling_the_api(stub):
    client = stub([])
    outcome = await ex.extract("   ")
    assert outcome.ok and outcome.result.is_empty()
    assert client.messages.calls == []


async def test_successful_extraction_returns_the_parsed_result(stub):
    parsed = ex.ExtractionResult(
        summary="Akmalga 5 mln berildi",
        debts=[
            ex.ExtractedDebt(
                direction="they_owe_me",
                person="Akmal",
                amount=5_000_000,
                currency="UZS",
                due_date="2026-08-25",
            )
        ],
    )
    client = stub([_response(parsed)])
    outcome = await ex.extract("Akmalga 5 mln berdim")

    assert outcome.ok
    assert outcome.result.debts[0].due == date(2026, 8, 25)
    assert len(client.messages.calls) == 1


async def test_the_system_prompt_carries_a_cache_breakpoint(stub):
    client = stub([_response(ex.ExtractionResult())])
    await ex.extract("salom")
    system = client.messages.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


async def test_a_transport_error_is_retried_once(stub):
    error = anthropic.APIConnectionError(request=None)
    client = stub([error, _response(ex.ExtractionResult(summary="ok"))])

    outcome = await ex.extract("salom")

    assert outcome.ok and outcome.result.summary == "ok"
    assert len(client.messages.calls) == 2
    # The retry nudges the model toward valid JSON.
    assert "ONLY a JSON object" in client.messages.calls[1]["messages"][-1]["content"]


async def test_two_failures_surface_as_an_error_not_an_exception(stub):
    error = anthropic.APIConnectionError(request=None)
    stub([error, error])

    outcome = await ex.extract("salom")

    assert not outcome.ok
    assert outcome.result is None
    assert "APIConnectionError" in outcome.error


async def test_a_refusal_is_reported_without_retrying(stub):
    client = stub([_response(None, stop_reason="refusal")])
    outcome = await ex.extract("salom")

    assert not outcome.ok and outcome.error == "refusal"
    assert len(client.messages.calls) == 1


async def test_truncated_output_is_retried(stub):
    client = stub(
        [_response(None, stop_reason="max_tokens"), _response(ex.ExtractionResult())]
    )
    outcome = await ex.extract("uzun matn")

    assert outcome.ok
    assert len(client.messages.calls) == 2


# --- when the API cannot compile the schema ---------------------------------
#
# "Grammar compilation timed out" is decided server-side, before the model is
# sampled. Every real-time extraction hit it, so nothing was extracted at all
# and the owner's messages produced no reply.


def _grammar_timeout():
    return anthropic.BadRequestError(
        message=(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'Grammar compilation timed out.'}}"
        ),
        response=SimpleNamespace(status_code=400, headers={}, request=None),
        body=None,
    )


def _text_response(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=payload)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


async def test_an_uncompilable_schema_falls_back_to_prompted_json(stub):
    body = (
        '{"summary":"Akmalga 5 mln berildi","debts":[{"direction":"they_owe_me",'
        '"person":"Akmal","amount":5000000,"currency":"UZS"}]}'
    )
    stub([_grammar_timeout()], created=[_text_response(body)])

    outcome = await ex.extract("Akmalga 5 mln berdim")

    assert outcome.ok, outcome.error
    assert outcome.result.debts[0].person == "Akmal"
    assert outcome.result.debts[0].amount == 5_000_000


async def test_the_fallback_carries_the_schema_the_grammar_would_have_enforced(stub):
    """Without the grammar the model only knows the shape from the prompt."""
    client = stub([_grammar_timeout()], created=[_text_response('{"summary":"x"}')])
    await ex.extract("Akmalga 5 mln berdim")

    system_text = client.messages.create_calls[0]["system"][0]["text"]
    assert "debt_settlements" in system_text
    assert "output_config" not in client.messages.create_calls[0]


async def test_the_rejection_is_remembered_so_it_is_not_paid_for_twice(stub):
    """One wasted call per process, not one per message."""
    client = stub(
        [_grammar_timeout()],
        created=[_text_response('{"summary":"a"}'), _text_response('{"summary":"b"}')],
    )

    first = await ex.extract("birinchi xabar")
    second = await ex.extract("ikkinchi xabar")

    assert first.ok and second.ok
    # The structured route was tried exactly once, by the first message.
    assert len(client.messages.calls) == 1
    assert len(client.messages.create_calls) == 2


async def test_the_batch_path_stops_sending_a_schema_the_server_rejected(stub):
    """A whole batch must not fail on a grammar already known to be uncompilable."""
    before = ex.build_request_params("test", now=datetime(2026, 8, 20, tzinfo=TZ))
    assert "output_config" in before

    ex.note_grammar_failure(_grammar_timeout())
    after = ex.build_request_params("test", now=datetime(2026, 8, 20, tzinfo=TZ))

    assert "output_config" not in after
    assert "debt_settlements" in after["system"][0]["text"]


async def test_a_bad_request_that_is_not_about_the_grammar_still_fails(stub):
    """The fallback is for one specific server-side refusal, not a blanket retry."""
    other = anthropic.BadRequestError(
        message="Error code: 400 - {'error': {'message': 'max_tokens too large'}}",
        response=SimpleNamespace(status_code=400, headers={}, request=None),
        body=None,
    )
    client = stub([other, other])

    outcome = await ex.extract("Akmalga 5 mln berdim")

    assert not outcome.ok
    assert client.messages.create_calls == []
    assert ex.structured_output_available()


async def test_a_missing_api_key_comes_back_as_an_outcome_not_an_exception(monkeypatch):
    """`extract` promises never to raise; callers rely on that to reach needs_review."""
    monkeypatch.setattr(settings, "anthropic_api_key", "   ")
    ex._client = None

    outcome = await ex.extract("Akmalga 5 mln berdim")

    assert not outcome.ok
    assert "AnthropicUnavailable" in outcome.error


# --- what the model actually returns without a grammar ----------------------


@pytest.mark.parametrize(
    "wrapping",
    [
        '```json\n{"summary":"Akmalga 5 mln"}\n```',
        '```\n{"summary":"Akmalga 5 mln"}\n```',
        '```JSON\n{"summary":"Akmalga 5 mln"}```',
        'Mana natija:\n{"summary":"Akmalga 5 mln"}',
        '{"summary":"Akmalga 5 mln"}',
        '  {"summary":"Akmalga 5 mln"}  ',
    ],
)
def test_json_survives_the_wrapping_models_add(wrapping):
    """Without a grammar the model fences its JSON however plainly it is asked not to."""
    result = ex.parse_extraction_json(wrapping)
    assert result is not None
    assert result.summary == "Akmalga 5 mln"


@pytest.mark.parametrize("body", ["sorry, I cannot", "", "{not json at all}"])
def test_unwrapping_never_rescues_a_genuinely_bad_object(body):
    """Only presentation is forgiven; the object still has to validate."""
    assert ex.parse_extraction_json(body) is None


async def test_a_fenced_fallback_reply_still_produces_a_debt(stub):
    fenced = (
        '```json\n{"summary":"Akmalga 5 mln berildi","debts":'
        '[{"direction":"they_owe_me","person":"Akmal","amount":5000000}]}\n```'
    )
    stub([_grammar_timeout()], created=[_text_response(fenced)])

    outcome = await ex.extract("Akmalga 5 mln berdim")

    assert outcome.ok, outcome.error
    assert outcome.result.debts[0].person == "Akmal"
