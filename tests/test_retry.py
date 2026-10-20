"""JV-02: one backoff helper, and today's API calls retried exactly once over.

No database, no network and no real waiting: the sleep is a recorder and the
HTTP calls are fakes.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from miya.config import settings
from miya.services import embeddings, extraction, gcal, retry, transcription
from miya.services.embeddings import EmbeddingError, RemoteEmbedder
from miya.services.transcription import ElevenLabsScribe, TranscriptionError

PACKAGE = Path(__file__).resolve().parents[1] / "miya"
REQUEST = httpx.Request("POST", "https://example.invalid/")


class Sleeps:
    """Stands in for asyncio.sleep and remembers each wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class Flaky:
    """Fails with each of `errors` in turn, then returns `result`."""

    def __init__(self, errors: list[Exception], result: str = "ok") -> None:
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


# --- the helper ---------------------------------------------------------------


async def test_backoff_retries_transient_then_succeeds(monkeypatch):
    # The jitter is pinned to its upper bound, so the waits show the curve.
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: high)
    sleeps = Sleeps()
    fn = Flaky([httpx.ConnectError("down", request=REQUEST), httpx.ReadTimeout("slow")])

    result = await retry.with_backoff(
        fn,
        attempts=4,
        base=1.0,
        cap=30.0,
        retry_on=retry.is_transient_http,
        sleep=sleeps,
    )

    assert result == "ok"
    assert fn.calls == 3
    assert sleeps.waits == [1.0, 2.0]


async def test_backoff_gives_up_on_permanent():
    sleeps = Sleeps()
    fn = Flaky([ValueError("bad request")])

    with pytest.raises(ValueError):
        await retry.with_backoff(
            fn,
            attempts=5,
            base=1.0,
            cap=30.0,
            retry_on=retry.is_transient_http,
            sleep=sleeps,
        )

    assert fn.calls == 1
    assert sleeps.waits == []


async def test_backoff_stops_after_the_last_attempt():
    sleeps = Sleeps()
    errors: list[Exception] = [httpx.ConnectError("down") for _ in range(5)]
    fn = Flaky(errors)

    with pytest.raises(httpx.ConnectError):
        await retry.with_backoff(
            fn,
            attempts=3,
            base=1.0,
            cap=30.0,
            retry_on=(httpx.ConnectError,),
            sleep=sleeps,
        )

    assert fn.calls == 3
    assert len(sleeps.waits) == 2


async def test_backoff_never_retries_cancellation():
    async def cancelled() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry.with_backoff(
            cancelled,
            attempts=3,
            base=1.0,
            cap=1.0,
            retry_on=lambda exc: True,
            sleep=Sleeps(),
        )


def test_backoff_delay_is_full_jitter_and_capped(monkeypatch):
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high

    monkeypatch.setattr(retry.random, "uniform", uniform)
    delays = [retry.backoff_delay(n, base=0.5, cap=3.0) for n in range(1, 6)]

    assert delays == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert all(low == 0.0 for low, _ in bounds)


def test_backoff_delay_survives_a_huge_attempt_number():
    assert 0.0 <= retry.backoff_delay(10_000, base=1.0, cap=5.0) <= 5.0


async def test_backoff_refuses_zero_attempts():
    with pytest.raises(ValueError):
        await retry.with_backoff(
            Flaky([]), attempts=0, base=1.0, cap=1.0, retry_on=(OSError,)
        )


async def test_backoff_logs_only_the_attempt_and_the_error_class(caplog):
    fn = Flaky([httpx.ConnectError("secret body 998877"), httpx.ConnectError("again")])

    with (
        caplog.at_level(logging.INFO, logger="miya.services.retry"),
        pytest.raises(httpx.ConnectError),
    ):
        await retry.with_backoff(
            fn,
            attempts=2,
            base=1.0,
            cap=1.0,
            retry_on=retry.is_transient_http,
            sleep=Sleeps(),
        )

    records = [r for r in caplog.records if r.name == "miya.services.retry"]
    assert [r.attempt for r in records] == [1, 2]
    assert {r.error for r in records} == {"ConnectError"}
    assert "secret body" not in caplog.text
    assert "998877" not in caplog.text


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (httpx.ConnectError("x"), True),
        (httpx.ReadTimeout("x"), True),
        (httpx.RemoteProtocolError("x"), True),
        (retry.TransientStatus(httpx.Response(503)), True),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(429)),
            True,
        ),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(500)),
            True,
        ),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(400)),
            False,
        ),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(404)),
            False,
        ),
        (httpx.UnsupportedProtocol("x"), False),
        (ValueError("x"), False),
    ],
)
def test_is_transient_http(exc, transient):
    assert retry.is_transient_http(exc) is transient


@pytest.mark.parametrize(("code", "raises"), [(200, False), (400, False), (429, True)])
def test_raise_for_transient_status(code, raises):
    response = httpx.Response(code)
    if raises:
        with pytest.raises(retry.TransientStatus) as caught:
            retry.raise_for_transient_status(response)
        assert caught.value.response is response
    else:
        assert retry.raise_for_transient_status(response) is response


# --- the shared model client retries by itself -------------------------------


def test_model_client_max_retries_explicit(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(extraction, "_client", None)

    client = extraction.get_client()

    assert extraction.MODEL_CLIENT_MAX_RETRIES == 3
    assert client.max_retries == 3


def test_model_call_sites_are_not_wrapped_again():
    """A module that calls the model client never adds with_backoff on top."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if "get_client" in calls | names and "with_backoff" in calls | names:
            offenders.append(path.relative_to(PACKAGE).as_posix())
    assert offenders == []


# --- the plain-HTTP calls ------------------------------------------------------


class FakePost:
    """Replaces httpx.AsyncClient.post with a queue of status codes."""

    def __init__(self, codes: list[int], body: dict) -> None:
        self.codes = list(codes)
        self.body = body
        self.calls = 0
        self.files_sent: list[bytes] = []

    async def __call__(self, url, **kwargs):
        self.calls += 1
        files = kwargs.get("files")
        if files:
            self.files_sent.append(files["file"][1].read())
        code = self.codes.pop(0)
        if code >= 400:
            return httpx.Response(code, text="no", request=httpx.Request("POST", url))
        return httpx.Response(code, json=self.body, request=httpx.Request("POST", url))


@pytest.fixture
def no_wait(monkeypatch):
    sleeps = Sleeps()
    real = retry.with_backoff

    async def recorded(fn, **kwargs):
        kwargs["sleep"] = sleeps
        return await real(fn, **kwargs)

    monkeypatch.setattr(retry, "with_backoff", recorded)
    return sleeps


async def test_transcription_retries_429_not_400(monkeypatch, tmp_path, no_wait):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"voice")
    fake = FakePost([429, 503, 200], {"text": "salom", "language_code": "uzb"})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake)

    result = await ElevenLabsScribe(api_key="k").transcribe(audio)

    assert result.text == "salom"
    assert fake.calls == 3
    # The file goes out whole on every attempt, not from where the last left off.
    assert fake.files_sent == [b"voice", b"voice", b"voice"]
    assert len(no_wait.waits) == 2

    refused = FakePost([400, 200], {"text": "never"})
    monkeypatch.setattr(httpx.AsyncClient, "post", refused)
    with pytest.raises(TranscriptionError, match="400"):
        await ElevenLabsScribe(api_key="k").transcribe(audio)
    assert refused.calls == 1


async def test_transcription_last_429_keeps_the_error(monkeypatch, tmp_path, no_wait):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"voice")
    attempts = transcription.SCRIBE_ATTEMPTS
    fake = FakePost([429] * attempts, {})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake)

    with pytest.raises(TranscriptionError, match="429"):
        await ElevenLabsScribe(api_key="k").transcribe(audio)
    assert fake.calls == attempts


async def test_transcription_retries_a_dropped_connection(monkeypatch, tmp_path, no_wait):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"voice")
    calls = 0

    async def post(client, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(TranscriptionError, match="request failed"):
        await ElevenLabsScribe(api_key="k").transcribe(audio)
    assert calls == transcription.SCRIBE_ATTEMPTS


async def test_transcription_does_not_retry_a_read_timeout(
    monkeypatch, tmp_path, no_wait
):
    # The file was uploaded whole; another try would bill Scribe again.
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"voice")
    calls = 0

    async def post(client, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(TranscriptionError, match="request failed"):
        await ElevenLabsScribe(api_key="k").transcribe(audio)
    assert calls == 1
    assert no_wait.waits == []


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("x"), httpx.WriteTimeout("x"), httpx.PoolTimeout("x")],
)
def test_scribe_still_retries_timeouts_before_the_upload_ends(exc):
    assert transcription.scribe_retry_on(exc)
    assert not transcription.scribe_retry_on(httpx.ReadTimeout("x"))


async def test_remote_embed_retries_5xx_not_401(monkeypatch, no_wait):
    fake = FakePost([502, 200], {"vectors": [[0.1, 0.2]]})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake)

    vectors = await RemoteEmbedder(base_url="http://api:8000").embed(["salom"])

    assert vectors == [[0.1, 0.2]]
    assert fake.calls == 2

    refused = FakePost([401], {})
    monkeypatch.setattr(httpx.AsyncClient, "post", refused)
    with pytest.raises(EmbeddingError, match="401"):
        await RemoteEmbedder(base_url="http://api:8000").embed(["salom"])
    assert refused.calls == 1
    assert embeddings.EMBED_ATTEMPTS >= 2


# --- Google calls use the client's own retry ----------------------------------


class _Request:
    def __init__(self, response: dict, seen: list) -> None:
        self.response = response
        self.seen = seen

    def execute(self, **kwargs):
        self.seen.append(kwargs)
        return self.response


def test_gcal_calls_use_num_retries():
    seen: list[dict] = []
    events = SimpleNamespace(
        list=lambda **kwargs: _Request({"items": [{"id": "e1"}]}, seen),
        insert=lambda **kwargs: _Request({"id": "e2"}, seen),
    )
    api = gcal.GoogleCalendarAPI(calendar_id="primary")
    api._service = SimpleNamespace(events=lambda: events)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    assert api._list(now, now) == [{"id": "e1"}]
    assert api._insert({"summary": "x"}) == {"id": "e2"}
    assert seen == [{"num_retries": 5}, {"num_retries": 5}]
    assert gcal.GOOGLE_NUM_RETRIES == 5
