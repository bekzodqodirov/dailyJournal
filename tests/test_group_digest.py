"""Groups: what was said to the owner, and what each chat was about (spec §7B).

A busy supplier group is mostly other people talking to each other. Two things
have to come out of it and they are not the same: the handful of messages aimed
at the owner, and a sense of what the room spent the day on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from miya.bot import replies
from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import ChatMonitor, ConversationWindow, Interaction
from miya.services import queries
from miya.services.windows import render_window

CHAT = -1001
OTHER = -1002


async def _monitor(session, chat_id=CHAT, title="Yuk guruhi"):
    monitor = ChatMonitor(
        tg_chat_id=chat_id,
        chat_type=ChatType.group,
        title=title,
        monitor_enabled=True,
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def _window(session, chat_id=CHAT):
    at = datetime.now(settings.tz)
    window = ConversationWindow(
        tg_chat_id=chat_id,
        started_at=at,
        ended_at=at,
        message_count=2,
        char_count=20,
        text="(window)",
        custom_id=f"w-{chat_id}",
    )
    session.add(window)
    await session.flush()
    return window


async def _msg(
    session,
    *,
    chat_id=CHAT,
    text="salom",
    to_me=False,
    window_id=None,
    summary=None,
    at=None,
):
    meta = {"tg_message_id": 1}
    if to_me:
        meta["to_me"] = True
    interaction = Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=chat_id,
        occurred_at=at or datetime.now(settings.tz),
        raw_text=text,
        summary=summary,
        window_id=window_id,
        meta=meta,
    )
    session.add(interaction)
    await session.flush()
    return interaction


# --- who a message was aimed at ----------------------------------------------


async def test_only_messages_aimed_at_the_owner_are_listed(session):
    await _monitor(session)
    await _msg(session, text="ular o'zaro gaplashyapti")
    mine = await _msg(session, text="Bekzod aka, konteyner qachon?", to_me=True)

    found = await queries.messages_to_me(session)

    assert [i.id for i in found] == [mine.id]


async def test_yesterdays_mentions_are_not_todays(session):
    await _monitor(session)
    await _msg(
        session,
        text="kecha so'ralgan",
        to_me=True,
        at=datetime.now(settings.tz) - timedelta(days=1),
    )

    assert await queries.messages_to_me(session) == []


async def test_a_row_with_no_metadata_is_not_mistaken_for_a_mention(session):
    """`metadata` is JSON null for rows written without one — a predicate that
    treated that as a match would list every message in every chat."""
    await _monitor(session)
    session.add(
        Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=datetime.now(settings.tz),
            raw_text="botga yozilgan eslatma",
        )
    )
    await session.flush()

    assert await queries.messages_to_me(session) == []


def test_the_window_text_marks_what_was_addressed_to_the_owner(session=None):
    """The extractor cannot tell a commitment to the owner from one between two
    other people unless the transcript says so."""
    at = datetime.now(settings.tz)
    to_room = Interaction(
        direction=Direction.in_,
        person_id=7,
        occurred_at=at,
        raw_text="ertaga yuk keladi",
        meta={},
    )
    to_owner = Interaction(
        direction=Direction.in_,
        person_id=7,
        occurred_at=at,
        raw_text="sizga 5 mln qarzim bor",
        meta={"to_me": True},
    )

    text = render_window([to_room, to_owner], {7: "Akmal"})

    assert "[THEM (Akmal)]" in text
    assert "[THEM (Akmal) → ME]" in text


# --- what the room was about --------------------------------------------------


async def test_a_digest_counts_messages_and_carries_the_summaries(session):
    await _monitor(session)
    await _msg(session)
    await _msg(session)
    # The window row: written by the extractor, holding the summary.
    window = await _window(session)
    await _msg(
        session, text="(window)", window_id=window.id, summary="Yuk narxi kelishildi"
    )

    digests = await queries.chat_digests(session)

    assert len(digests) == 1
    assert digests[0].title == "Yuk guruhi"
    # Member messages are counted; the window row is not one of them.
    assert digests[0].messages == 2
    assert digests[0].summaries == ["Yuk narxi kelishildi"]


async def test_the_busiest_chat_comes_first(session):
    await _monitor(session, CHAT, "Sekin guruh")
    await _monitor(session, OTHER, "Shovqinli guruh")
    await _msg(session, chat_id=CHAT)
    for _ in range(3):
        await _msg(session, chat_id=OTHER)

    digests = await queries.chat_digests(session)

    assert [d.title for d in digests] == ["Shovqinli guruh", "Sekin guruh"]


async def test_a_chat_with_no_registered_title_still_appears(session):
    """A message can arrive before the dialog sync has named the chat."""
    await _msg(session, chat_id=-99)

    digests = await queries.chat_digests(session)

    assert [d.title for d in digests] == ["-99"]


# --- how they read ------------------------------------------------------------


def test_the_report_says_where_each_request_came_from():
    at = datetime.now(settings.tz)
    body = replies.to_me_report(
        [
            Interaction(
                tg_chat_id=CHAT, occurred_at=at, raw_text="konteyner qachon?", meta={}
            )
        ],
        {CHAT: "Yuk guruhi"},
    )
    assert "Yuk guruhi" in body
    assert "konteyner qachon?" in body


def test_an_open_conversation_says_so_rather_than_looking_empty():
    """A window only closes once the chat goes quiet, so an active group
    legitimately has messages but no summary yet."""
    digest = queries.ChatDigest(
        tg_chat_id=CHAT, title="Yuk guruhi", messages=12, summaries=[]
    )
    body = replies.chat_digest_report([digest])
    assert "12 ta xabar" in body
    assert "hali umumlashtirilmadi" in body


def test_chat_titles_are_escaped():
    """A group title is set by whoever runs the group, not by the owner."""
    digest = queries.ChatDigest(
        tg_chat_id=CHAT, title="<b>Yuk", messages=1, summaries=["<i>xulosa"]
    )
    body = replies.chat_digest_report([digest])
    assert "<b>Yuk" not in body
    assert "&lt;b&gt;Yuk" in body
    assert "&lt;i&gt;xulosa" in body


def test_quiet_days_say_so():
    assert "yozilmadi" in replies.to_me_report([], {})
    assert "bo'lmadi" in replies.chat_digest_report([])
