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

# Anything that would write to Telegram, mark chats read, or bulk-download
# history. The spec is explicit: the userbot reads, and does nothing else.
FORBIDDEN_CALLS = {
    "send_message",
    "send_file",
    "send_read_acknowledge",
    "edit_message",
    "delete_messages",
    "forward_messages",
    "get_messages",  # would pull history; only live events are allowed
    "iter_messages",
}


def _called_attributes(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


@pytest.mark.parametrize("path", sorted(USERBOT_PACKAGE.glob("*.py")))
def test_the_userbot_never_writes_to_telegram(path):
    called = _called_attributes(path.read_text())
    assert not (called & FORBIDDEN_CALLS), (
        f"{path.name} calls {sorted(called & FORBIDDEN_CALLS)} — the userbot "
        "must stay strictly read-only (spec §7B)"
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
