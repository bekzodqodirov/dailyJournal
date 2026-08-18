"""The userbot resolves the person and then holds that transaction across the
Telegram download + the (up to 300 s) Scribe call. resolve_person takes a
pg_advisory_xact_lock keyed on the name, so the assistant-bot process blocks on
the same name for the whole transcription."""
from __future__ import annotations

import asyncio
import time

import sqlalchemy as sa

from miya.db import models as m
from miya.db.session import session_scope
from miya.services.people import resolve_person


async def _userbot_voice_message(hold_seconds: float, ready: asyncio.Event) -> None:
    async with session_scope() as session:
        await resolve_person(session, "Akmal aka")   # advisory lock taken here
        ready.set()
        await asyncio.sleep(hold_seconds)            # download + Scribe transcription


async def _assistant_bot_text(ready: asyncio.Event) -> float:
    await ready.wait()
    start = time.monotonic()
    async with session_scope() as session:
        await resolve_person(session, "Akmal")
    return time.monotonic() - start


async def test_bot_blocks_while_userbot_transcribes(session):
    await session.commit()
    ready = asyncio.Event()
    _, waited = await asyncio.gather(
        _userbot_voice_message(2.0, ready), _assistant_bot_text(ready)
    )
    print(f"assistant bot handler blocked for {waited:.2f}s")
    await session.execute(sa.delete(m.Person))
    await session.commit()
    assert waited < 0.5, f"bot handler blocked {waited:.2f}s on the userbot's lock"
