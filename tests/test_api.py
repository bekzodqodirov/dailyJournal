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
