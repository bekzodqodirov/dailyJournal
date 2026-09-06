"""The one-time Telethon login tool.

Its whole job is to hand over one string. The first version printed the
account's name first and died there on an un-awaited coroutine — signing in
successfully, then throwing the credential away. Logging in mints a new
authorised session on the account, so a lost string cannot be re-fetched,
only re-minted, leaving an orphan session behind. Hence: credential first,
cosmetics after, and nothing cosmetic may be fatal.
"""

from __future__ import annotations

import asyncio

import pytest

from miya.config import settings
from miya.tools import userbot_login

SESSION = "1BQAN-pretend-session-string"


class _FakeSession:
    def save(self) -> str:
        return SESSION


class _FakeClient:
    """Stands in for TelegramClient. Every method is a real coroutine, so a
    forgotten `await` fails here exactly as it did in production."""

    def __init__(self, *args, **kwargs):
        self.session = _FakeSession()
        self.disconnected = False
        self.me_error: Exception | None = None

    async def start(self):
        return self

    async def get_me(self):
        if self.me_error:
            raise self.me_error
        return type("Me", (), {"first_name": "Bekzod", "username": None, "id": 1})()

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_client(monkeypatch):
    import telethon

    made: list[_FakeClient] = []

    def factory(*args, **kwargs):
        client = _FakeClient()
        made.append(client)
        return client

    monkeypatch.setattr(telethon, "TelegramClient", factory)
    return made


async def test_the_session_string_is_printed(fake_client, capsys):
    await userbot_login._login()

    out = capsys.readouterr().out
    assert f"TELETHON_SESSION={SESSION}" in out
    assert "Bekzod" in out


async def test_a_failing_name_lookup_does_not_cost_the_session(
    fake_client, capsys, monkeypatch
):
    """The exact regression: get_me blew up and took the credential with it."""
    import telethon

    def factory(*args, **kwargs):
        client = _FakeClient()
        client.me_error = AttributeError("'coroutine' object has no attribute ...")
        fake_client.append(client)
        return client

    monkeypatch.setattr(telethon, "TelegramClient", factory)

    await userbot_login._login()

    out = capsys.readouterr().out
    assert f"TELETHON_SESSION={SESSION}" in out
    assert "could not read the account name" in out


async def test_the_client_is_always_disconnected(fake_client):
    await userbot_login._login()
    assert fake_client[0].disconnected


def test_it_refuses_without_api_credentials(monkeypatch, capsys):
    monkeypatch.setattr(settings, "telethon_api_id", None)
    monkeypatch.setattr(settings, "telethon_api_hash", "")

    assert userbot_login.main() == 1
    assert "my.telegram.org" in capsys.readouterr().err


# --- a malformed session string is a mechanical mistake, not a crash ---------


@pytest.mark.parametrize(
    ("value", "mistake"),
    [
        ("TELETHON_SESSION=1BQANOtherStuff", "the prefix pasted twice"),
        ('"1BQANOtherStuff"', "surviving quotes"),
        ("1BQAN", "a truncated paste"),
        ("   1BQANOtherStuff", "leading whitespace"),
    ],
)
def test_a_bad_session_string_is_explained_not_tracebacked(monkeypatch, value, mistake):
    """Telethon says only `ValueError: Not a valid string`, under twenty lines
    of traceback. The owner needs to know which mechanical slip to look for."""
    from miya.userbot import main as userbot_main

    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_api_id", 12345)
    monkeypatch.setattr(settings, "telethon_api_hash", "hash")
    monkeypatch.setattr(settings, "telethon_session", value)

    with pytest.raises(SystemExit) as exc:
        asyncio.run(userbot_main.run())

    message = str(exc.value)
    assert "TELETHON_SESSION" in message
    # It must say what it actually got, or the owner cannot tell which slip.
    assert str(len(value)) in message
    assert repr(value[:6]) in message
