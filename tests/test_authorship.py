"""JV-10: MIYA-sent messages are never the owner's words.

One predicate (miya/services/authorship.py) tells the owner's own outgoing
messages from the replies MIYA sent in their name, and every consumer that
reads "what the owner said" goes through it: the window transcript, the
open-loop scan, the recap counts, the passage index, recall citations and the
userbot's echo tagging.
"""

from __future__ import annotations

import ast
import itertools
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from telethon.tl.types import User

from miya.bot import recap_text
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource, PromiseMadeBy
from miya.db.session import session_scope
from miya.services import (
    authorship,
    batch,
    loops,
    passages,
    prompts,
    queries,
    recall,
    recaps,
    windows,
)
from miya.services import extraction as ex
from miya.userbot import main as userbot

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]
DM, GROUP = 6101, -6102
_ids = itertools.count(1)

AI_TAG = {"sent_by": "miya", "authored_by": "ai", "outbox_id": 41}
OWNER_TAG = {"sent_by": "miya", "authored_by": "owner", "outbox_id": 42}
STALL = "Assalomu alaykum, aniqlashtirib xabar beraman"


def _ago(**kwargs) -> datetime:
    return datetime.now(TZ) - timedelta(**kwargs)


def _unsaved(text="salom", *, out=False, meta=None, source=None) -> m.Interaction:
    return m.Interaction(
        source=source or InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=7,
        tg_chat_id=DM,
        occurred_at=datetime(2026, 10, 1, 10, 0, tzinfo=TZ),
        raw_text=text,
        meta=meta,
    )


async def _person(session, name="Akmal") -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _monitor(session, chat=DM, chat_type=ChatType.private) -> m.ChatMonitor:
    monitor = m.ChatMonitor(
        tg_chat_id=chat,
        chat_type=chat_type,
        title="Akmal" if chat_type is ChatType.private else "Yuk",
        monitor_enabled=True,
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def _msg(
    session, text, *, at, out=False, person=None, chat=DM, meta=None
) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        meta={"tg_message_id": next(_ids), **(meta or {})},
    )
    session.add(row)
    await session.flush()
    return row


# --- the one predicate ------------------------------------------------------------------

# (direction, metadata, is it MIYA's own reply, is it the owner's words)
CASES = [
    (Direction.out, None, False, True),
    (Direction.out, {}, False, True),
    (Direction.out, {"tg_message_id": 5}, False, True),
    (Direction.out, AI_TAG, True, False),
    (Direction.out, OWNER_TAG, False, True),
    # A tag without authored_by is not MIYA's text, exactly as the plan's
    # predicate reads: NOT (has sent_by AND authored_by = 'ai').
    (Direction.out, {"sent_by": "miya"}, False, True),
    # authored_by alone is not a MIYA echo.
    (Direction.out, {"authored_by": "ai"}, False, True),
    (Direction.in_, AI_TAG, False, False),
    (Direction.in_, None, False, False),
    (Direction.na, {"kind": "window"}, False, False),
]


@pytest.mark.parametrize(("direction", "meta", "ai", "owner"), CASES)
def test_the_python_predicates(direction, meta, ai, owner):
    row = SimpleNamespace(direction=direction, meta=meta)
    assert authorship.is_ai_reply(row) is ai
    assert authorship.is_owner_words(row) is owner


async def test_the_sql_predicates_agree_with_python_on_every_row(session):
    rows = []
    for direction, meta, _, _ in CASES:
        row = m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=direction,
            tg_chat_id=DM,
            occurred_at=_ago(hours=1),
            raw_text="x",
            meta=meta,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    ids = [r.id for r in rows]
    owner = set(
        await session.scalars(
            sa.select(m.Interaction.id).where(
                m.Interaction.id.in_(ids), authorship.owner_words_clause()
            )
        )
    )
    ai = set(
        await session.scalars(
            sa.select(m.Interaction.id).where(
                m.Interaction.id.in_(ids), authorship.ai_reply_clause()
            )
        )
    )
    assert owner == {r.id for r, case in zip(rows, CASES, strict=True) if case[3]}
    assert ai == {r.id for r, case in zip(rows, CASES, strict=True) if case[2]}


def test_the_sql_predicate_is_the_planned_one():
    sql = str(
        authorship.owner_words_clause().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "interactions.direction = 'out'" in sql
    assert "interactions.metadata ? 'sent_by'" in sql
    assert "(interactions.metadata ->> 'authored_by') = 'ai'" in sql
    assert "NOT coalesce(" in sql


def test_an_unknown_author_is_never_the_owner():
    assert authorship.author_of("owner") == "owner"
    assert authorship.author_of("ai") == "ai"
    assert authorship.author_of(None) == "ai"
    assert authorship.author_of("OWNER") == "ai"


# --- the window transcript --------------------------------------------------------------


def test_ai_row_rendered_as_miya():
    text = windows.render_window(
        [
            _unsaved("narxi qancha?"),
            _unsaved(STALL, out=True, meta=AI_TAG),
            _unsaved("rahmat", out=True),
        ],
        {7: "Akmal"},
    )
    lines = text.splitlines()
    assert lines[0].endswith(' [THEM (Akmal)] "narxi qancha?"')
    assert lines[1].endswith(f' [MIYA] "{STALL}"')
    assert lines[2].endswith(' [ME] "rahmat"')
    assert "[ME]" not in lines[1]


def test_owner_command_row_is_me():
    """The owner approved this exact text (an owner command or an edited
    card), so it stays their words everywhere."""
    row = _unsaved("ertaga yuboraman", out=True, meta=OWNER_TAG)
    assert authorship.is_owner_words(row)
    assert not authorship.is_ai_reply(row)
    [line] = windows.render_window([row], {7: "Akmal"}).splitlines()
    assert line.endswith(' [ME] "ertaga yuboraman"')
    assert passages.attribution(row, ChatType.private) == (None, 7, True)
    assert recall._who(row, {7: "Akmal"}) == "Siz"


def test_the_prompt_says_miya_lines_are_never_the_owner():
    prompt = prompts.EXTRACTION_SYSTEM_PROMPT
    assert "MIYA (an automatic reply" in prompt
    rule = prompt[prompt.index("- A [MIYA] line") :]
    assert "never the owner's own words" in rule
    assert 'asserted_by "me"' in rule
    assert "[MIYA] line" in recaps.RECAP_SYSTEM_PROMPT


def _fake_model_reading_labels(seen: list[str]):
    """A stand-in for EXTRACT_MODEL that follows the transcript contract the
    prompt states: a first-person commitment is the owner's promise only on a
    line labelled [ME]."""

    async def _extract(text, *, now=None):
        seen.append(text)
        promises = []
        for line in text.splitlines():
            match = re.match(r'^\[[^\]]+\] \[([^\]]+)\] "(.*)"$', line)
            if match and match.group(1) == "ME" and "xabar beraman" in match.group(2):
                promises.append(
                    ex.ExtractedPromise(
                        made_by="me",
                        person="Akmal",
                        description="Akmalga xabar berish",
                        asserted_by="me",
                    )
                )
        return ex.ExtractionOutcome(
            result=ex.ExtractionResult(summary="narx so'raldi", promises=promises),
            model=settings.extract_model,
        )

    return _extract


async def test_ai_stall_not_extracted_as_owner_promise(session, monkeypatch):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(minutes=20), person=akmal)
    await _msg(session, STALL, at=_ago(minutes=19), out=True, person=akmal, meta=AI_TAG)
    [window] = await windows.flush_ready_windows(session)
    assert f'[MIYA] "{STALL}"' in window.text
    assert "[ME]" not in window.text
    await session.commit()

    seen: list[str] = []
    monkeypatch.setattr(batch, "extract", _fake_model_reading_labels(seen))
    outcome = await batch.extract_instant(session)

    assert outcome.applied == 1
    assert len(seen) == 1 and "[MIYA]" in seen[0]
    promises = list(await session.scalars(sa.select(m.Promise)))
    assert promises == []


async def test_the_same_words_typed_by_the_owner_are_still_a_promise(
    session, monkeypatch
):
    """The control: nothing changes for a row without the tag."""
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(minutes=20), person=akmal)
    await _msg(session, STALL, at=_ago(minutes=19), out=True, person=akmal)
    await windows.flush_ready_windows(session)
    await session.commit()

    monkeypatch.setattr(batch, "extract", _fake_model_reading_labels([]))
    await batch.extract_instant(session)

    [promise] = list(await session.scalars(sa.select(m.Promise)))
    assert promise.made_by is PromiseMadeBy.me


# --- open loops -------------------------------------------------------------------------


async def test_ai_reply_not_closing_loop(session):
    akmal = await _person(session)
    await _monitor(session)
    asked = await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)
    await _msg(session, STALL, at=_ago(hours=5), out=True, person=akmal, meta=AI_TAG)

    [loop] = await loops.unanswered_questions(session)

    assert loop.ref == f"q{asked.id}"


async def test_an_owner_approved_reply_still_closes_the_loop(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)
    await _msg(session, "ertaga aytaman", at=_ago(hours=5), out=True, meta=OWNER_TAG)

    assert await loops.unanswered_questions(session) == []


# --- recaps -----------------------------------------------------------------------------


async def test_recap_counts_miya_replies_apart_from_the_owner(session):
    start = datetime.now(TZ) - timedelta(hours=10)
    end = datetime.now(TZ) + timedelta(minutes=1)
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=3), person=akmal)
    await _msg(
        session,
        STALL,
        at=_ago(hours=3) + timedelta(minutes=1),
        out=True,
        person=akmal,
        meta=AI_TAG,
    )
    await _msg(session, "ha", at=_ago(hours=2), out=True, person=akmal)
    await _msg(session, "ok", at=_ago(hours=2), out=True, person=akmal, meta=OWNER_TAG)

    day = await recaps.gather_activity(session, start, end, now=end)

    [person] = day.people
    assert (person.messages_in, person.messages_out, person.messages_ai) == (1, 2, 1)
    [question] = person.questions
    assert question.answered is True  # the owner's "ha" came later


async def test_a_question_only_miya_answered_stays_unanswered_in_the_recap(session):
    start = datetime.now(TZ) - timedelta(hours=10)
    end = datetime.now(TZ) + timedelta(minutes=1)
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=3), person=akmal)
    await _msg(session, STALL, at=_ago(hours=2), out=True, person=akmal, meta=AI_TAG)

    day = await recaps.gather_activity(session, start, end, now=end)

    [person] = day.people
    assert (person.messages_in, person.messages_out, person.messages_ai) == (1, 0, 1)
    [question] = person.questions
    assert question.answered is False


def test_the_recap_header_shows_miya_replies_on_their_own():
    person = m.Person(id=1, display_name="Akmal", aliases=[])
    day = recaps.PersonDay(person=person, messages_in=3, messages_out=1, messages_ai=2)
    header = recap_text._person_header(day, {})
    assert "💬 4 xabar" in header
    assert "MIYA: 2 ta avtomatik javob" in header
    plain = recaps.PersonDay(person=person, messages_in=3, messages_out=1)
    assert "MIYA" not in recap_text._person_header(plain, {})


def test_the_recap_prose_input_labels_miya_lines():
    row = _unsaved(STALL, out=True, meta=AI_TAG)
    row.id = 1
    _, line = recaps._line_of(row)
    assert f'[MIYA] "{STALL}"' in line
    _, own = recaps._line_of(_unsaved("rahmat", out=True))
    assert '[ME] "rahmat"' in own


# --- passages and recall ----------------------------------------------------------------


async def test_passage_not_from_owner(session):
    akmal = await _person(session)
    await _monitor(session)
    await _monitor(session, GROUP, ChatType.group)
    await _msg(session, "narxi qancha?", at=_ago(hours=3), person=akmal)
    stall = await _msg(
        session, STALL, at=_ago(hours=2), out=True, person=akmal, meta=AI_TAG
    )
    in_group = await _msg(
        session, STALL, at=_ago(hours=2), out=True, chat=GROUP, meta=AI_TAG
    )
    own = await _msg(session, "rahmat", at=_ago(hours=1), out=True, person=akmal)

    await passages.index_pending(session)

    [p_stall] = list(
        await session.scalars(
            sa.select(m.Passage).where(m.Passage.interaction_id == stall.id)
        )
    )
    assert p_stall.from_owner is False
    assert p_stall.speaker_person_id is None
    assert p_stall.chat_person_id == akmal.id
    assert f"Akmal: MIYA: {STALL}" in p_stall.embed_text
    [p_group] = list(
        await session.scalars(
            sa.select(m.Passage).where(m.Passage.interaction_id == in_group.id)
        )
    )
    assert (p_group.from_owner, p_group.speaker_person_id) == (False, None)
    [p_own] = list(
        await session.scalars(
            sa.select(m.Passage).where(m.Passage.interaction_id == own.id)
        )
    )
    assert p_own.from_owner is True
    # The owner's line carries the stall as context, labelled MIYA, not ME.
    assert f"MIYA: {STALL}" in p_own.embed_text
    assert f"ME: {STALL}" not in p_own.embed_text


async def test_recall_citation_says_miya_sent_it(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=3), person=akmal)
    stall = await _msg(
        session, STALL, at=_ago(hours=2), out=True, person=akmal, meta=AI_TAG
    )
    await _msg(session, "rahmat", at=_ago(hours=1), out=True, person=akmal)

    source = await recall.source_of(session, f"m{stall.id}")

    who = {line.text: line.who for line in source.lines}
    assert who[STALL] == "🤖 MIYA nomidan (avtomatik)"
    assert who["rahmat"] == "Siz"
    assert who["narxi qancha?"] == "Akmal"


# --- the person timeline ---------------------------------------------------------------


async def test_timeline_of_owner_lines_leaves_out_miya_replies(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=4), person=akmal)
    await _msg(session, STALL, at=_ago(hours=3), out=True, person=akmal, meta=AI_TAG)
    approved = await _msg(
        session,
        "ertaga aytaman",
        at=_ago(hours=2),
        out=True,
        person=akmal,
        meta=OWNER_TAG,
    )
    typed = await _msg(session, "rahmat", at=_ago(hours=1), out=True, person=akmal)

    mine = await queries.timeline(session, akmal.id, direction=Direction.out)
    his = await queries.timeline(session, akmal.id, direction=Direction.in_)

    assert [e.interaction.id for e in mine] == [typed.id, approved.id]
    assert [e.text for e in his] == ["narxi qancha?"]


# --- echo tagging at ingest -------------------------------------------------------------

PEER = User(id=DM, first_name="Akmal", username="akmal_test")


def _tg_text(mid: int, text: str, *, out: bool):
    async def get_chat():
        return PEER

    async def get_sender():
        return PEER

    return SimpleNamespace(
        id=mid,
        chat_id=DM,
        date=_ago(minutes=1),
        message=text,
        out=out,
        mentioned=False,
        file=None,
        sticker=None,
        gif=None,
        voice=None,
        audio=None,
        video_note=None,
        photo=None,
        video=None,
        document=None,
        get_chat=get_chat,
        get_sender=get_sender,
    )


async def _stored(session, mid: int) -> m.Interaction:
    rows = await session.scalars(
        sa.select(m.Interaction).where(m.Interaction.tg_chat_id == DM)
    )
    [row] = [r for r in rows if r.meta["tg_message_id"] == mid]
    await session.refresh(row)
    return row


async def _private_monitor() -> None:
    async with session_scope() as s:
        s.add(
            m.ChatMonitor(
                tg_chat_id=DM,
                chat_type=ChatType.private,
                title="Akmal",
                monitor_enabled=True,
                monitoring_since=_ago(days=30),
            )
        )


async def test_ingest_tags_an_outgoing_message_sent_from_the_outbox(session, monkeypatch):
    await _private_monitor()
    asked: list[tuple[str, str]] = []

    async def _find(session, *, channel, external_id):
        asked.append((channel, external_id))
        if external_id == f"{DM}:31":
            return authorship.Echo(outbox_id=9, authored_by="ai")
        return None

    monkeypatch.setattr(authorship, "find_echo", _find)

    assert await userbot.ingest_message(None, _tg_text(31, STALL, out=True)) is True
    assert await userbot.ingest_message(None, _tg_text(32, "rahmat", out=True)) is True
    assert await userbot.ingest_message(None, _tg_text(33, "salom", out=False)) is True

    sent = await _stored(session, 31)
    assert sent.meta["sent_by"] == "miya"
    assert sent.meta["authored_by"] == "ai"
    assert sent.meta["outbox_id"] == 9
    assert sent.meta["tg_message_id"] == 31
    assert authorship.is_ai_reply(sent)
    typed = await _stored(session, 32)
    assert "sent_by" not in typed.meta and authorship.is_owner_words(typed)
    incoming = await _stored(session, 33)
    assert "sent_by" not in incoming.meta
    # Only outgoing messages are looked up.
    assert asked == [("telegram", f"{DM}:31"), ("telegram", f"{DM}:32")]


async def test_ingest_without_an_outbox_leaves_outgoing_rows_as_they_were(session):
    await _private_monitor()
    assert await userbot.ingest_message(None, _tg_text(41, "rahmat", out=True)) is True
    row = await _stored(session, 41)
    assert row.meta == {"tg_message_id": 41}
    assert authorship.is_owner_words(row)


def _sent_row(
    row_id: int, channel: str, external_id: str, author: str
) -> m.OutboxMessage:
    return m.OutboxMessage(
        id=row_id,
        channel=channel,
        chat_key=f"tg:{DM}" if channel == "telegram" else "mail:t1",
        tg_chat_id=DM if channel == "telegram" else None,
        origin="owner_command" if author == "owner" else "auto_reply",
        authored_by=author,
        body="rahmat",
        body_sha256="0" * 64,
        guard={"verdict": "pass"},
        state="sent",
        approved_by="button",
        external_message_id=external_id,
        idempotency_key=f"k{row_id}",
    )


async def test_find_echo_reads_the_outbox_row(session):
    # JV-11: the lookup reads the real outbox_messages table through its model.
    session.add_all(
        [
            _sent_row(5, "telegram", "6101:77", "owner"),
            _sent_row(6, "telegram", "6101:78", "ai"),
            _sent_row(7, "gmail", "6101:79", "ai"),
        ]
    )
    await session.flush()

    def find(external_id):
        return authorship.find_echo(session, channel="telegram", external_id=external_id)

    assert await find(authorship.telegram_external_id(DM, 77)) == authorship.Echo(
        5, "owner"
    )
    assert await find("6101:78") == authorship.Echo(6, "ai")
    assert await find("6101:79") is None
    assert await find("6101:80") is None
    await session.rollback()


def test_the_echo_tag_is_merged_not_replaced():
    meta = authorship.tagged({"tg_message_id": 5}, authorship.Echo(3, "owner"))
    assert meta == {
        "tg_message_id": 5,
        "sent_by": "miya",
        "authored_by": "owner",
        "outbox_id": 3,
    }


# --- every consumer uses the one predicate ----------------------------------------------

CONSUMERS = (
    "miya/services/windows.py",
    "miya/services/loops.py",
    "miya/services/recaps.py",
    "miya/services/passages.py",
    "miya/services/recall.py",
    "miya/services/queries.py",
)


def _outgoing_checks(tree: ast.AST) -> list[int]:
    """Lines that decide "outgoing" by hand: ``Direction.out`` anywhere, or a
    ``.direction`` compared with the string ``'out'``."""
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "out"
            and isinstance(node.value, ast.Name)
            and node.value.id == "Direction"
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(
                isinstance(n, ast.Attribute) and n.attr == "direction" for n in sides
            ) and any(isinstance(n, ast.Constant) and n.value == "out" for n in sides):
                lines.append(node.lineno)
    return sorted(lines)


def _imports_authorship(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "miya.services"
        and any(alias.name == "authorship" for alias in node.names)
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize("path", CONSUMERS)
def test_predicate_used_everywhere(path):
    """Who wrote an outgoing message is decided only in authorship.py: these
    modules import it and never compare a direction with ``out`` themselves."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    assert _imports_authorship(tree), path
    assert _outgoing_checks(tree) == [], path


def test_the_grep_catches_a_hand_written_check():
    tree = ast.parse(
        "a = row.direction is Direction.out\n"
        "b = Interaction.direction == Direction.out\n"
        "c = row.direction == 'out'\n"
        "d = kind == 'out'\n"
    )
    assert _outgoing_checks(tree) == [1, 2, 3]
    assert not _imports_authorship(tree)


def test_the_userbot_and_the_prompt_take_part():
    userbot_source = (ROOT / "miya/userbot/main.py").read_text(encoding="utf-8")
    assert "authorship.find_echo(" in userbot_source
    assert "authorship.tagged(" in userbot_source
    assert "[MIYA]" in prompts.EXTRACTION_SYSTEM_PROMPT
