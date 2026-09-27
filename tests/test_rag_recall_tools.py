"""WP-57: the reasoning model's toolbox — search_history, fixed
recent_interactions, deterministic date hints, `now` flowing into tools."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import passages, rag

TZ = settings.tz
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=TZ)
DM = 7101
_ids = itertools.count(1)


async def _setup(session, name="Akmal Karimov"):
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    session.add(m.ChatMonitor(tg_chat_id=DM, chat_type=ChatType.private, title=name))
    await session.flush()
    return person


async def _voice(session, person, text, at=NOW - timedelta(days=2)):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id,
        tg_chat_id=DM,
        occurred_at=at,
        transcript=text,
        media={"type": "voice", "processed": True},
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    return row


async def _tool(session, name, args, now=NOW):
    return json.loads(await rag._run_tool(session, None, name, args, now=now))


async def test_search_history_returns_episodes_with_refs_where_and_who(session):
    akmal = await _setup(session)
    row = await _voice(session, akmal, "konteyner ertaga chiqadi")
    await passages.index_pending(session)
    out = await _tool(
        session, "search_history", {"query": "konteyner", "person": "Akmal Karimov"}
    )
    [episode] = out["episodes"]
    assert episode["where"] == "Akmal Karimov bilan shaxsiy chat · ovozli xabar"
    [line] = [x for x in episode["lines"] if x["hit"]]
    assert line["who"] == "Akmal Karimov" and line["ref"] == f"m{row.id}"
    assert line["text"] == "konteyner ertaga chiqadi"


async def test_search_history_date_args_are_tashkent_days(session):
    akmal = await _setup(session)
    await _voice(
        session, akmal, "konteyner kechqurun", at=datetime(2026, 9, 20, 23, 30, tzinfo=TZ)
    )
    await passages.index_pending(session)
    out = await _tool(
        session,
        "search_history",
        {"query": "konteyner", "date_from": "2026-09-20", "date_to": "2026-09-20"},
    )
    assert len(out["episodes"]) == 1


async def test_search_history_refuses_an_ambiguous_person(session):
    session.add_all(
        [
            m.Person(display_name="Akmal", aliases=[]),
            m.Person(display_name="Akmal", aliases=[]),
        ]
    )
    await session.flush()
    out = await _tool(session, "search_history", {"query": "x", "person": "Akmal"})
    assert "ambiguous" in out["error"]


async def test_recent_interactions_shows_voice_transcripts_and_hides_questions(session):
    akmal = await _setup(session)
    voice = await _voice(session, akmal, "yuk qachon keladi", at=NOW - timedelta(hours=2))
    session.add(
        m.Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=NOW - timedelta(hours=1),
            raw_text="yuk qayerda?",
            meta={"kind": "question"},
        )
    )
    await session.flush()
    out = await _tool(session, "recent_interactions", {})
    assert [r["ref"] for r in out["results"]] == [f"m{voice.id}"]
    assert "yuk qachon keladi" in out["results"][0]["summary"]


async def test_tools_use_the_answer_now(session):
    akmal = await _setup(session)
    await _voice(session, akmal, "eski gap", at=NOW - timedelta(days=3))
    within = await _tool(session, "recent_interactions", {"days": 7})
    assert len(within["results"]) == 1
    later = await _tool(
        session, "recent_interactions", {"days": 7}, now=NOW + timedelta(days=30)
    )
    assert later["results"] == []


def test_date_hints_for_friday_2026_09_25():
    assert rag.date_hints(NOW) == (
        "CURRENT_DATE: 2026-09-25 (juma), Asia/Tashkent\n"
        "bugun=2026-09-25; kecha=2026-09-24; o'tgan kuni=2026-09-23\n"
        "bu hafta=2026-09-21..2026-09-25; o'tgan hafta=2026-09-14..2026-09-20\n"
        "bu oy=2026-09-01..2026-09-25; o'tgan oy=2026-08-01..2026-08-31"
    )


async def test_search_history_carries_the_untrusted_note(session):
    out = await _tool(session, "search_history", {"query": "hech narsa"})
    assert out["note"] == rag.UNTRUSTED_NOTE and out["episodes"] == []
