"""API smoke tests. These pass whether or not a database is reachable."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from miya.api.main import app
from miya.config import settings


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_status_tracks_database_reachability(client):
    """`ok`/200 when the database answers, `degraded`/503 when it does not."""
    resp = client.get("/health")
    body = resp.json()

    assert body["timezone"] == "Asia/Tashkent"
    assert body["now"].endswith("+05:00")  # Asia/Tashkent has no DST
    if body["database"] == "ok":
        assert (resp.status_code, body["status"]) == (200, "ok")
    else:
        assert (resp.status_code, body["status"]) == (503, "degraded")
        assert body["database"] == "unreachable"
        assert "database_error" in body


def test_config_requires_a_bearer_token(client):
    assert client.get("/v1/config").status_code in (401, 503)


def test_config_accepts_the_configured_token(client, monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", "s3cret")
    resp = client.get("/v1/config", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.json()["extract_model"] == "claude-haiku-4-5"


def test_config_rejects_a_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", "s3cret")
    resp = client.get("/v1/config", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_embed_endpoint_serves_vectors(client, monkeypatch):
    from miya.api import main as api_main

    class _Stub:
        async def embed(self, texts):
            return [[0.0] * settings.embed_dim for _ in texts]

    monkeypatch.setattr(settings, "api_bearer_token", "s3cret")
    monkeypatch.setattr(api_main, "get_local_embedder", lambda: _Stub())

    resp = client.post(
        "/v1/embed",
        headers={"Authorization": "Bearer s3cret"},
        json={"texts": ["salom", "yuk"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["vectors"]) == 2
    assert body["dim"] == settings.embed_dim


def test_embed_endpoint_requires_auth_and_texts(client, monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", "s3cret")
    assert client.post("/v1/embed", json={"texts": ["x"]}).status_code == 401
    resp = client.post(
        "/v1/embed", headers={"Authorization": "Bearer s3cret"}, json={"texts": []}
    )
    assert resp.status_code == 422


# --- the startup check (WP-03) ----------------------------------------------


@pytest.mark.parametrize("token", ["", "s3cret"])
def test_startup_refuses_a_blank_or_short_token(monkeypatch, token):
    from miya.api.main import assert_startup_config

    monkeypatch.setattr(settings, "api_bearer_token", token)
    with pytest.raises(SystemExit) as caught:
        assert_startup_config()
    assert "API_BEARER_TOKEN" in str(caught.value)


def test_startup_accepts_a_strong_token(monkeypatch):
    from miya.api.main import assert_startup_config

    monkeypatch.setattr(settings, "api_bearer_token", "ab" * 32)
    assert assert_startup_config() is None


def test_lifespan_does_not_check_the_token(monkeypatch):
    """The check lives in `python -m miya.api`, not the lifespan, so the app
    still starts (and answers 503) when served some other way."""
    monkeypatch.setattr(settings, "api_bearer_token", "")
    monkeypatch.setattr(settings, "upload_tokens", "")
    with TestClient(app) as c:
        assert c.get("/v1/config").status_code == 503


def test_api_main_module_exits_before_uvicorn(monkeypatch):
    import runpy
    import sys

    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(settings, "api_bearer_token", "short")
    monkeypatch.delitem(sys.modules, "miya.api.__main__", raising=False)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("miya.api", run_name="__main__")
    assert "API_BEARER_TOKEN" in str(caught.value)
    assert calls == []
