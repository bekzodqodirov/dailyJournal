"""Oversized media: ask the owner instead of dropping it (spec §6).

The old policy silently skipped a video or an outsized file. Silence is the
one outcome the owner cannot act on — he never learns the attachment existed.
Now the message is stored, nothing is fetched, and he gets two buttons.

Three processes share this and none can call the others: the userbot sees the
message, the worker asks over the assistant bot, the bot records the answer.
So the tests exercise the state, not the transport.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.bot import replies
from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Interaction
from miya.services import approvals
from miya.services.media_policy import MediaKind, forced_plan, plan_for


def plan(kind, *, vision=True, docs=True, size=None, filename=None):
    return plan_for(
        kind,
        vision_enabled=vision,
        docs_enabled=docs,
        size=size,
        filename=filename,
    )


async def _pending(session, *, kind="video", size=90_000_000, occurred_at=None):
    interaction = Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=-100,
        occurred_at=occurred_at or datetime.now(settings.tz),
        raw_text=None,
        media={
            "type": kind,
            "size": size,
            "filename": "yuk.mp4",
            "caption": "konteyner",
            "processed": False,
            "approval": {"state": approvals.PENDING, "reason": kind},
        },
        meta={"tg_message_id": 77},
    )
    session.add(interaction)
    await session.flush()
    return interaction


# --- the policy --------------------------------------------------------------


def test_an_oversized_document_is_offered_rather_than_dropped():
    result = plan(
        MediaKind.document,
        size=settings.doc_max_bytes + 1,
        filename="shartnoma.pdf",
    )
    assert result.download is False
    assert (result.ask, result.ask_reason) == (True, "too_large")


def test_an_oversized_audio_file_is_offered():
    result = plan(MediaKind.audio, size=settings.audio_max_bytes + 1)
    assert (result.ask, result.ask_reason) == (True, "too_large")


def test_something_beyond_any_sane_size_is_not_even_offered():
    """A round trip the owner cannot sensibly say yes to is not worth asking."""
    result = plan(MediaKind.video, size=settings.media_ask_max_bytes + 1)
    assert result.ask is False
    assert result.skip_reason == "beyond_ask_limit"


def test_an_unknown_size_is_still_offered():
    """Telegram omits the size on some forwards. Refusing to ask would put the
    decision back where it was — nowhere."""
    assert plan(MediaKind.video, size=None).ask is True


def test_normal_media_is_untouched_by_any_of_this():
    assert plan(MediaKind.voice).download is True
    assert plan(MediaKind.voice).ask is False
    assert plan(MediaKind.photo, vision=True).vision is True
    assert plan(MediaKind.sticker).ignore is True


# --- what a yes means --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (MediaKind.video, ("download", "transcribe", "extract_audio")),
        (MediaKind.audio, ("download", "transcribe")),
        (MediaKind.photo, ("download", "vision")),
    ],
)
def test_approval_overrules_every_gate(kind, expected):
    result = forced_plan(kind)
    for field in expected:
        assert getattr(result, field) is True, field


def test_approval_cannot_conjure_a_reader_for_an_unreadable_format():
    """The caps are the owner's to overrule. A format nothing can parse is not."""
    result = forced_plan(MediaKind.document, filename="arxiv.zip")
    assert result.download is False
    assert result.skip_reason == "unsupported_type"


# --- the state machine -------------------------------------------------------


async def test_a_pending_item_is_found_then_stops_being_found(session):
    interaction = await _pending(session)
    assert [i.id for i in await approvals.awaiting_question(session)] == [interaction.id]

    approvals.set_state(interaction, approvals.ASKED)
    await session.flush()
    assert await approvals.awaiting_question(session) == []


async def test_only_a_yes_reaches_the_fetch_queue(session):
    yes = await _pending(session)
    no = await _pending(session)
    approvals.set_state(yes, approvals.APPROVED)
    approvals.set_state(no, approvals.DECLINED)
    await session.flush()

    assert [i.id for i in await approvals.approved_for_fetch(session)] == [yes.id]


async def test_state_changes_survive_the_orm(session):
    """A nested dict mutated in place never reaches the database — the whole
    reason set_state rebinds `media` instead of assigning into it."""
    interaction = await _pending(session)
    approvals.set_state(interaction, approvals.APPROVED)
    await session.flush()

    # Read the column itself rather than the instance: what the identity map
    # holds proves nothing about what was written.
    stored = await session.scalar(
        sa.select(Interaction.media).where(Interaction.id == interaction.id)
    )
    assert stored["approval"]["state"] == approvals.APPROVED
    assert stored["filename"] == "yuk.mp4"  # the rest of the dict is intact


async def test_an_unanswered_question_expires(session):
    fresh = await _pending(session)
    old = await _pending(
        session,
        occurred_at=datetime.now(settings.tz)
        - timedelta(hours=settings.media_ask_expiry_hours + 1),
    )
    await session.flush()

    assert await approvals.expire_stale(session) == 1
    assert approvals.state_of(old) == approvals.EXPIRED
    assert approvals.state_of(fresh) == approvals.PENDING


async def test_an_answered_question_is_never_expired(session):
    """Approved-but-not-yet-fetched must survive; the download may be slow."""
    interaction = await _pending(
        session,
        occurred_at=datetime.now(settings.tz)
        - timedelta(hours=settings.media_ask_expiry_hours + 5),
    )
    approvals.set_state(interaction, approvals.APPROVED)
    await session.flush()

    assert await approvals.expire_stale(session) == 0
    assert approvals.state_of(interaction) == approvals.APPROVED


async def test_rows_without_media_are_not_mistaken_for_pending(session):
    """`media` is JSON null for a plain text message, not SQL NULL — a
    comparison that treats the two alike would sweep up every note."""
    session.add(
        Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=datetime.now(settings.tz),
            raw_text="oddiy xabar",
        )
    )
    await session.flush()

    assert await approvals.awaiting_question(session) == []
    assert await approvals.expire_stale(session) == 0


# --- the question itself -----------------------------------------------------


def test_the_question_carries_what_the_answer_depends_on():
    body = replies.media_question(
        who="Akmal",
        media={
            "type": "video",
            "size": 94_371_840,
            "filename": "yuk.mp4",
            "caption": "konteyner rasmlari",
        },
        reason="video",
    )
    assert "Akmal" in body
    assert "90 MB" in body
    assert "konteyner rasmlari" in body


def test_the_question_escapes_a_caption_the_sender_controls():
    body = replies.media_question(
        who="<b>Akmal", media={"caption": "<script>", "type": "video"}, reason="video"
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
