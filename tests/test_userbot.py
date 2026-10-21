"""Userbot safety and classification (spec §7B).

Telethon is never connected here: the message objects are stand-ins with the
attributes Telethon exposes, which is all the classification code touches. The
first test is the important one — it is what keeps the userbot passive.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from miya.db.enums import ChatType
from miya.services.media_policy import MediaKind
from miya.userbot import main as userbot

USERBOT_PACKAGE = Path(userbot.__file__).parent

# Two different invariants, kept apart on purpose.
#
# Writing to Telegram is forbidden outright and everywhere: the counterparty
# must never see a trace of MIYA in a conversation.
WRITE_CALLS = {
    "send_message",
    "send_file",
    "send_read_acknowledge",
    "edit_message",
    "delete_messages",
    "forward_messages",
}

# Reading history is a weaker rule — it is about *unsolicited* reading. The
# userbot lives on live events and must not trawl backwards on its own, but
# re-reading one message the owner explicitly approved is the opposite of
# unsolicited. The allowlist is by function so the exception stays one
# reviewable line rather than a whole-file exemption.
HISTORY_CALLS = {"get_messages", "iter_messages"}
HISTORY_ALLOWED_IN = {"fetch_approved", "fetch_style_history"}


def _calls_by_function(source: str) -> dict[str | None, set[str]]:
    """Attribute calls in each function; None is module level."""
    tree = ast.parse(source)
    owner: dict[int, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)

    found: dict[str | None, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            found.setdefault(owner.get(id(node)), set()).add(node.func.attr)
    return found


@pytest.mark.parametrize("path", sorted(USERBOT_PACKAGE.glob("*.py")))
def test_the_userbot_never_writes_to_telegram(path):
    calls = _calls_by_function(path.read_text())
    everything = set().union(*calls.values()) if calls else set()
    assert not (everything & WRITE_CALLS), (
        f"{path.name} calls {sorted(everything & WRITE_CALLS)} — the userbot "
        "must stay strictly read-only (spec §7B)"
    )


@pytest.mark.parametrize("path", sorted(USERBOT_PACKAGE.glob("*.py")))
def test_history_is_only_read_where_the_owner_asked_for_it(path):
    for function, called in _calls_by_function(path.read_text()).items():
        offending = called & HISTORY_CALLS
        if not offending or function in HISTORY_ALLOWED_IN:
            continue
        pytest.fail(
            f"{path.name}:{function or '<module>'} calls {sorted(offending)} — "
            "the userbot follows live events and must not pull history on its "
            f"own. Only {sorted(HISTORY_ALLOWED_IN)} may, to re-read a message "
            "the owner approved."
        )


def test_the_kill_switch_stops_the_process_before_it_connects(monkeypatch):
    """USERBOT_ENABLED=false must return without touching Telethon at all."""
    import asyncio

    from miya.config import settings

    monkeypatch.setattr(settings, "userbot_enabled", False)

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("TelegramClient was constructed with the switch off")

    monkeypatch.setattr(userbot, "TelegramClient", _explode)

    idled = []

    async def _no_idle():
        idled.append(True)

    # With the switch off the process must IDLE, not exit: the container is
    # `restart: unless-stopped`, so a clean exit would make Docker restart it
    # in a tight loop forever. Asserting the idle actually happened is the
    # point — a stub that merely lets run() return would pass either way.
    monkeypatch.setattr(userbot, "_idle_forever", _no_idle)
    asyncio.run(userbot.run())
    assert idled, "the kill switch must idle the process, not let it exit"


def test_missing_credentials_fail_loudly_instead_of_prompting(monkeypatch):
    import asyncio

    from miya.config import settings

    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_session", "")

    with pytest.raises(SystemExit):
        asyncio.run(userbot.run())


# --- classification ----------------------------------------------------------


def _message(**kwargs) -> SimpleNamespace:
    fields = {
        "sticker": None,
        "gif": None,
        "voice": None,
        "audio": None,
        "video_note": None,
        "photo": None,
        "video": None,
        "document": None,
    }
    return SimpleNamespace(**{**fields, **kwargs})


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_message(), MediaKind.text),
        (_message(voice=object()), MediaKind.voice),
        (_message(video_note=object()), MediaKind.video_note),
        (_message(photo=object()), MediaKind.photo),
        (_message(video=object(), document=object()), MediaKind.video),
        (_message(document=object()), MediaKind.document),
        (_message(sticker=object()), MediaKind.sticker),
        # A GIF arrives as a document with an animation attribute; it must not
        # be mistaken for a business document.
        (_message(gif=object(), document=object()), MediaKind.sticker),
    ],
)
def test_message_classification(message, expected):
    assert userbot.kind_of(message) is expected


def test_chat_types_map_from_telethon_entities():
    from telethon.tl.types import Channel, Chat, User

    user = User(id=1)
    group = Chat(
        id=2, title="Ish", photo=None, participants_count=3, date=None, version=1
    )
    supergroup = Channel(id=3, title="Katta guruh", photo=None, date=None, megagroup=True)
    channel = Channel(id=4, title="Kanal", photo=None, date=None, broadcast=True)

    assert userbot.chat_type_of(user) is ChatType.private
    assert userbot.chat_type_of(group) is ChatType.group
    assert userbot.chat_type_of(supergroup) is ChatType.group
    assert userbot.chat_type_of(channel) is ChatType.channel


def test_display_names_fall_back_through_what_telegram_gave_us():
    assert userbot.display_name_of(
        SimpleNamespace(first_name="Akmal", last_name="G")
    ) == ("Akmal G")
    assert (
        userbot.display_name_of(
            SimpleNamespace(first_name=None, last_name=None, username="akmal_gz")
        )
        == "akmal_gz"
    )
    anonymous = SimpleNamespace(first_name=None, last_name=None, username=None, id=77)
    assert userbot.display_name_of(anonymous) == "tg:77"


# --- the two-phase media path ------------------------------------------------


async def test_media_work_happens_with_no_transaction_held(monkeypatch, tmp_path):
    """fetch_media must not touch the database — that is the whole point.

    Any session it opened would hold a pooled connection (and the person
    advisory lock) for the length of a Scribe call.
    """
    from miya.db import session as db_session
    from miya.services.media_policy import MediaPlan
    from miya.services.transcription import Transcript
    from miya.userbot import main as ub

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fetch_media opened a database session")

    monkeypatch.setattr(db_session, "SessionLocal", _explode)
    monkeypatch.setattr(ub, "userbot_media_dir", lambda: tmp_path)

    class _Client:
        async def download_media(self, message, file):
            Path(file).write_bytes(b"audio")

    class _Scribe:
        name = "elevenlabs"
        model = "scribe_v1"

        async def transcribe(self, path, *, language_hint=None):
            return Transcript(text="Akmalga 5 mln berdim", language="uz", duration=12.0)

    monkeypatch.setattr(ub, "get_transcriber", lambda: _Scribe())

    outcome = await ub.fetch_media(
        _Client(),
        SimpleNamespace(id=1),
        MediaPlan(kind=MediaKind.voice, download=True, transcribe=True),
        ".ogg",
    )

    assert outcome.transcript.text == "Akmalga 5 mln berdim"
    assert outcome.failed is False


async def test_a_transcription_failure_is_carried_back_not_raised(monkeypatch, tmp_path):
    from miya.services.media_policy import MediaPlan
    from miya.services.transcription import TranscriptionError
    from miya.userbot import main as ub

    monkeypatch.setattr(ub, "userbot_media_dir", lambda: tmp_path)

    class _Client:
        async def download_media(self, message, file):
            Path(file).write_bytes(b"audio")

    class _Broken:
        model = "scribe_v1"

        async def transcribe(self, path, *, language_hint=None):
            raise TranscriptionError("scribe is down")

    monkeypatch.setattr(ub, "get_transcriber", lambda: _Broken())

    outcome = await ub.fetch_media(
        _Client(),
        SimpleNamespace(id=1),
        MediaPlan(kind=MediaKind.voice, download=True, transcribe=True),
        ".ogg",
    )

    assert outcome.failed is True
    assert outcome.transcript is None


async def test_a_declined_download_records_why(monkeypatch):
    from miya.services.media_policy import MediaPlan
    from miya.userbot import main as ub

    outcome = await ub.fetch_media(
        None,
        SimpleNamespace(id=1),
        MediaPlan(kind=MediaKind.photo, download=False, skip_reason="vision_disabled"),
        ".jpg",
    )
    assert outcome.skip_reason == "vision_disabled"
    assert outcome.path is None


async def test_persist_media_writes_the_transcript_and_the_bill(session, monkeypatch):
    from datetime import datetime

    import sqlalchemy as sa

    from miya.config import settings
    from miya.db import models as m
    from miya.db.enums import Direction, InteractionSource
    from miya.services.transcription import Transcript
    from miya.userbot import main as ub

    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=-1,
        occurred_at=datetime.now(settings.tz),
        media={"type": "voice", "processed": False},
    )
    session.add(interaction)
    await session.flush()

    class _Scribe:
        model = "scribe_v1"

    monkeypatch.setattr(ub, "get_transcriber", lambda: _Scribe())

    await ub.persist_media(
        session,
        interaction,
        ub.MediaOutcome(
            path=Path("/data/x.ogg"),
            transcript=Transcript(text="salom", language="uz", duration=30.0),
        ),
    )
    await session.flush()

    assert interaction.transcript == "salom"
    assert interaction.media["processed"] is True
    assert interaction.media["path"] == "/data/x.ogg"
    assert interaction.meta["asr_language"] == "uz"

    usage = await session.scalar(
        sa.select(m.UsageLog).where(m.UsageLog.operation == "transcribe")
    )
    assert usage is not None and usage.audio_seconds == 30


async def test_persist_media_flags_a_failure_for_review(session):
    from datetime import datetime

    from miya.config import settings as s
    from miya.db import models as m
    from miya.db.enums import Direction, InteractionSource
    from miya.userbot import main as ub

    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=-1,
        occurred_at=datetime.now(s.tz),
        media={"type": "voice", "processed": False},
    )
    session.add(interaction)
    await session.flush()

    await ub.persist_media(session, interaction, ub.MediaOutcome(failed=True))

    assert interaction.needs_review is True


async def test_a_group_voice_note_naming_the_owner_is_rechecked(session, monkeypatch):
    from datetime import datetime

    from miya.config import settings
    from miya.db import models as m
    from miya.db.enums import Direction, InteractionSource
    from miya.services.transcription import Transcript
    from miya.userbot import main as ub

    monkeypatch.setattr(settings, "owner_aliases", "Bekzod, Bekzod aka")
    session.add(m.ChatMonitor(tg_chat_id=-4242, chat_type=ChatType.group, title="Ish"))
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=-4242,
        occurred_at=datetime.now(settings.tz),
        media={"type": "voice", "processed": False},
        meta={"tg_message_id": 1},
    )
    session.add(interaction)
    await session.flush()

    class _Scribe:
        model = "scribe_v1"

    monkeypatch.setattr(ub, "get_transcriber", lambda: _Scribe())
    await ub.persist_media(
        session,
        interaction,
        ub.MediaOutcome(
            transcript=Transcript(
                text="Bekzod aka, yuk keldi", language="uz", duration=3.0
            )
        ),
    )
    assert interaction.meta["to_me"] is True
    assert interaction.meta["to_me_via"] == "transcript"


# --- the style history import (JV-14) -------------------------------------------------

BACKFILL_TOOL = Path(userbot.__file__).parents[1] / "tools" / "backfill.py"
# The functions of miya/tools/backfill.py that may read history: each one
# the owner asked for (a named backfill, a switched-on chat's catch-up, the
# archive import, the style import).
TOOL_HISTORY_ALLOWED_IN = {
    "backfill_chat",
    "catch_up_chat",
    "archive_chat",
    "fetch_style_history",
}


def test_style_history_allowed_function_only():
    """The style import reads history in exactly one named function of the
    backfill tool and never writes; the userbot's sweep only delegates."""
    tool = _calls_by_function(BACKFILL_TOOL.read_text())
    for function, called in tool.items():
        assert not (called & WRITE_CALLS), function
        if called & HISTORY_CALLS:
            assert function in TOOL_HISTORY_ALLOWED_IN, function
    assert "iter_messages" in tool["fetch_style_history"]
    sweep = _calls_by_function(Path(userbot.__file__).read_text())
    assert "fetch_style_history" in sweep["fetch_style_imports"]
    assert not (sweep["fetch_style_imports"] & (HISTORY_CALLS | WRITE_CALLS))


def _scope_of(session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    return _scope


async def _style_chats(session):
    from datetime import datetime

    from miya.config import settings
    from miya.db import models as m
    from miya.jarvis import policy
    from miya.services import chats

    now = datetime(2026, 10, 20, 12, 0, tzinfo=settings.tz)
    made = {}
    for chat_id, chat_type in (
        (8101, ChatType.private),
        (8102, ChatType.private),
        (8103, ChatType.private),
        (-8104, ChatType.group),
    ):
        monitor = m.ChatMonitor(
            tg_chat_id=chat_id,
            chat_type=chat_type,
            title=str(chat_id),
            monitor_enabled=True,
        )
        session.add(monitor)
        made[chat_id] = monitor
    await session.flush()
    # A chat the owner marked personal is never read for style.
    await policy.mark_chat(session, made[8103], by="command", now=now)
    asked = await chats.request_style_import(session, now=now)
    return made, asked


async def test_style_import_request_done_once(session, monkeypatch):
    """`/uslub yukla` asks for every monitored private chat that is not
    personal; the sweep reads each once through the backfill tool and stamps
    it done, and the next sweep reads nothing."""
    from miya.tools import backfill as backfill_tool

    made, asked = await _style_chats(session)
    assert asked == 2
    assert made[8103].style_import_requested_at is None
    assert made[-8104].style_import_requested_at is None
    monkeypatch.setattr(userbot, "session_scope", _scope_of(session))
    monkeypatch.setattr(userbot, "_style_import_resume_at", 0.0)
    calls: list[int] = []

    async def _fetch(client, chat_id, *, limit=backfill_tool.STYLE_HISTORY_LIMIT):
        calls.append(chat_id)
        return backfill_tool.StyleHistory(stored=3, seen=4)

    monkeypatch.setattr(backfill_tool, "fetch_style_history", _fetch)
    assert await userbot.fetch_style_imports(client=object()) == 2
    assert sorted(calls) == [8101, 8102]
    assert made[8101].style_import_done_at is not None
    assert made[8102].style_import_done_at is not None
    assert await userbot.fetch_style_imports(client=object()) == 0
    assert sorted(calls) == [8101, 8102]


async def test_style_import_failure_bounded_and_contained(session, monkeypatch):
    """A chat whose import fails is counted and retried a bounded number of
    sweeps while the others are read; a long FloodWait ends the sweep, is no
    failure, and no sweep reads anything until it is over; a failing sweep
    never stops the userbot's loop."""
    from miya.services import chats
    from miya.tools import backfill as backfill_tool

    made, _ = await _style_chats(session)
    monkeypatch.setattr(userbot, "session_scope", _scope_of(session))
    monkeypatch.setattr(userbot, "_style_import_resume_at", 0.0)
    calls: list[int] = []
    flood = {"waits": 1}

    async def _fetch(client, chat_id, *, limit=backfill_tool.STYLE_HISTORY_LIMIT):
        calls.append(chat_id)
        if chat_id == 8101 and flood["waits"]:
            flood["waits"] -= 1
            return backfill_tool.StyleHistory(seen=2, paused=True, wait_seconds=600)
        if chat_id == 8101:
            raise RuntimeError("no access")
        return backfill_tool.StyleHistory(stored=1, seen=1)

    monkeypatch.setattr(backfill_tool, "fetch_style_history", _fetch)
    assert await userbot.fetch_style_imports(client=object()) == 0
    assert calls == [8101]
    assert made[8101].style_import_attempts == 0
    assert made[8101].style_import_done_at is None
    assert made[8102].style_import_done_at is None
    # The wait is still running: the next sweeps ask Telegram for nothing.
    for _ in range(chats.STYLE_IMPORT_MAX_ATTEMPTS + 1):
        assert await userbot.fetch_style_imports(client=object()) == 0
    assert calls == [8101]
    assert made[8101].style_import_attempts == 0

    # The wait is over: the paused chat is read again with the others.
    monkeypatch.setattr(userbot, "_style_import_resume_at", 0.0)
    calls.clear()
    assert await userbot.fetch_style_imports(client=object()) == 1
    assert sorted(calls) == [8101, 8102]
    assert (
        made[8101].style_import_attempts == 1 and made[8101].style_import_done_at is None
    )
    assert made[8102].style_import_done_at is not None
    for _ in range(chats.STYLE_IMPORT_MAX_ATTEMPTS):
        await userbot.fetch_style_imports(client=object())
    assert made[8101].style_import_attempts == chats.STYLE_IMPORT_MAX_ATTEMPTS
    assert calls.count(8101) == chats.STYLE_IMPORT_MAX_ATTEMPTS
    assert await chats.pending_style_imports(session) == []
    progress = await chats.style_import_progress(session)
    assert (progress.done, progress.failed, progress.pending) == (1, 1, 0)
    assert progress.finished

    class _Stop(BaseException):
        pass

    async def _boom(client):
        raise RuntimeError("database down")

    async def _noop(*args, **kwargs):
        return None

    async def _sleep(seconds):
        raise _Stop

    monkeypatch.setattr(userbot, "fetch_style_imports", _boom)
    monkeypatch.setattr(userbot, "fetch_approved", _noop)
    monkeypatch.setattr(userbot, "fetch_backfills", _noop)
    monkeypatch.setattr(userbot, "beat_userbot", _noop)
    monkeypatch.setattr(userbot.asyncio, "sleep", _sleep)
    with pytest.raises(_Stop):
        await userbot.approved_media_loop(SimpleNamespace(is_connected=lambda: True))


async def test_style_history_reads_newest_with_floodwait_and_bad_rows(monkeypatch):
    """fetch_style_history stores what it reads as archive rows (no voice
    transcribed), sleeps through a short FloodWait and resumes where it
    stopped, ends the pass on a long one, and keeps going past a message
    that fails to store."""
    from telethon.errors import FloodWaitError

    from miya.tools import backfill as backfill_tool

    stored: list[tuple[int, dict]] = []

    async def _ingest(client, message, **kwargs):
        if message.id == 7:
            raise RuntimeError("bad row")
        stored.append((message.id, kwargs))
        return True

    monkeypatch.setattr(userbot, "ingest_message", _ingest)
    slept: list[int] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(backfill_tool.asyncio, "sleep", _sleep)

    class _Client:
        def __init__(self, waits):
            self.waits = list(waits)
            self.calls: list[dict] = []

        def iter_messages(self, chat_id, **kwargs):
            self.calls.append(kwargs)
            client = self

            async def _gen():
                start = kwargs["offset_id"] or 11
                for message_id in range(start - 1, 0, -1):
                    if client.waits and message_id == 8:
                        raise FloodWaitError(request=None, capture=client.waits.pop(0))
                    yield SimpleNamespace(id=message_id)

            return _gen()

    client = _Client([5])
    result = await backfill_tool.fetch_style_history(client, 8101, limit=10)
    assert slept == [5]
    assert [c["offset_id"] for c in client.calls] == [0, 9]
    assert result.seen == 10 and result.failed == 1 and result.stored == 9
    assert not result.paused and result.wait_seconds == 0
    assert all(
        kw == {"archive": True, "transcribe_media": False, "read_media": False}
        for _, kw in stored
    )

    long_wait = _Client([backfill_tool.FLOOD_WAIT_MAX_SECONDS + 1])
    paused = await backfill_tool.fetch_style_history(long_wait, 8101, limit=10)
    assert paused.paused and paused.seen == 2
    assert paused.wait_seconds == backfill_tool.FLOOD_WAIT_MAX_SECONDS + 1


async def test_style_import_never_downloads_or_reads_media(session, monkeypatch):
    """A photo or a document in the style import is stored as metadata only:
    nothing is downloaded, read by the vision model or parsed, while the row
    is still kept so the context pairing stays in order."""
    from datetime import datetime, timedelta

    import sqlalchemy as sa
    from telethon.tl.types import User

    from miya.config import settings
    from miya.db import models as m
    from miya.db.session import session_scope
    from miya.tools import backfill as backfill_tool

    chat_id = 8191
    peer = User(id=chat_id, first_name="Akmal", username="akmal_test")
    async with session_scope() as scope:
        scope.add(
            m.ChatMonitor(
                tg_chat_id=chat_id,
                chat_type=ChatType.private,
                title="Akmal",
                monitor_enabled=True,
            )
        )

    async def _no_model(*args, **kwargs):
        raise AssertionError("the style import read media with a model")

    monkeypatch.setattr(userbot, "read_image", _no_model)
    monkeypatch.setattr(userbot, "apply_vision", _no_model)

    def _tg(mid, *, photo=None, document=None, name=None, mime=None, text=""):
        async def _peer():
            return peer

        return SimpleNamespace(
            id=mid,
            chat_id=chat_id,
            date=datetime.now(settings.tz) - timedelta(days=40),
            message=text,
            out=False,
            mentioned=False,
            file=SimpleNamespace(name=name, size=2048, mime_type=mime),
            sticker=None,
            gif=None,
            voice=None,
            audio=None,
            video_note=None,
            photo=photo,
            video=None,
            document=document,
            reply_to=None,
            entities=None,
            get_chat=_peer,
            get_sender=_peer,
        )

    class _Client:
        def __init__(self):
            self.downloads: list[object] = []

        def iter_messages(self, chat, **kwargs):
            messages = [
                _tg(3, photo=object(), mime="image/jpeg", text="chek"),
                _tg(2, document=object(), name="hisob.pdf", mime="application/pdf"),
            ]

            async def _gen():
                for message in messages:
                    yield message

            return _gen()

        async def download_media(self, message, file=None):
            self.downloads.append(file)
            raise AssertionError("the style import downloaded media")

    client = _Client()
    result = await backfill_tool.fetch_style_history(client, chat_id, limit=10)
    assert (result.seen, result.stored, result.failed) == (2, 2, 0)
    assert client.downloads == []
    rows = (
        await session.scalars(
            sa.select(m.Interaction).where(m.Interaction.tg_chat_id == chat_id)
        )
    ).all()
    assert len(rows) == 2
    for row in rows:
        assert row.processed is True
        assert row.media["processed"] is True
        assert row.media["skipped"] == "archive"
        assert "approval" not in row.media
        assert row.meta["archive"] is True
