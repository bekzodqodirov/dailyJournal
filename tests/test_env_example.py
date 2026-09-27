"""`.env.example` must be copyable to `.env` verbatim and start cleanly.

No database. Docker Compose and python-dotenv both read ``KEY=   # note`` as
the value "# note", which crashed every container on the first `make up`.
Later packages add keys; these tests cover them automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from miya import config
from miya.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_no_value_in_env_example_is_a_comment():
    values = dotenv_values(ENV_EXAMPLE)
    bad = [k for k, v in values.items() if v and v.lstrip().startswith("#")]
    assert bad == []


def test_no_assignment_line_carries_an_inline_comment():
    pattern = re.compile(r"^[A-Z0-9_]+=.*\s#")
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if pattern.match(line)] == []


def test_settings_build_from_env_example_with_blank_credentials(monkeypatch):
    for key in ("TELETHON_API_ID", "API_BEARER_TOKEN", "OWNER_ALIASES"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=str(ENV_EXAMPLE))
    assert s.telethon_api_id is None
    assert s.api_bearer_token == ""
    assert s.owner_aliases == ""


def test_a_comment_shaped_value_is_refused_with_the_key_named():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, api_bearer_token="# generate: openssl rand -hex 32")
    assert "API_BEARER_TOKEN" in str(caught.value)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("age1abc", True), ("", True), ("ssh-ed25519 AAAA", True), ("xyz", False)],
)
def test_backup_recipient_must_look_like_an_age_key(value, ok):
    if ok:
        Settings(_env_file=None, backup_age_recipient=value)
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, backup_age_recipient=value)


def test_get_settings_exits_with_one_line_instead_of_a_traceback(monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setenv("TELETHON_API_ID", "# x")
    try:
        with pytest.raises(SystemExit) as caught:
            config.get_settings()
        assert "TELETHON_API_ID" in str(caught.value)
        assert "Traceback" not in str(caught.value)
    finally:
        monkeypatch.delenv("TELETHON_API_ID")
        config.get_settings.cache_clear()
