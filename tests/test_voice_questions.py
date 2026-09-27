"""WP-60: a spoken question is answered, not logged as a note."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.services import loops
from miya.services.persistence import Applied
from miya.services.rag import RagAnswer
from miya.services.text import fold_apostrophes
from tests.test_close_and_correct import _Callback, _Message, bound  # noqa: F401

NOW = datetime.now(settings.tz).replace(microsecond=0)


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


def _media_message(kind: str):
    message = SimpleNamespace(
        caption=None,
        date=NOW,
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        voice=None,
        audio=None,
        video_note=None,
        sent=[],
    )
    media = SimpleNamespace(mime_type="audio/ogg", file_size=10, duration=3)
    setattr(message, kind, media)

    async def _answer(text, **kwargs):
        message.sent.append((text, kwargs.get("reply_markup")))
        return _Sent(900 + len(message.sent))

    message.answer = _answer
    return message


@pytest.fixture
def harness(bound, monkeypatch, tmp_path):  # noqa: F811
    """Download, transcription, the answer and extraction are all stubs."""
    state = SimpleNamespace(heard="", answered=[], extracted=[])

    async def _download(bot, message, media, path):
        return True

    async def _transcribe(session, interaction, path):
        interaction.transcript = state.heard
        return state.heard

    async def _answer_full(session, text, *, mode=None, **kwargs):
        state.answered.append((text, mode))
        answer = "Akmal 3-kuni «konteyner» dedi <code>m1</code>"
        return RagAnswer(text=answer, refs=["m1"])

    async def _process(session, interaction):
        state.extracted.append(interaction.id)
        interaction.processed = True
        return SimpleNamespace(ok=True, applied=Applied())

    async def _extract_audio(src, dst):
        return True

    async def _typing(message):
        return None

    monkeypatch.setattr(handlers, "_download", _download)
    monkeypatch.setattr(handlers, "_typing", _typing)
    monkeypatch.setattr(handlers, "_media_path", lambda suffix: tmp_path / f"a{suffix}")
    monkeypatch.setattr(handlers, "transcribe_into", _transcribe)
    monkeypatch.setattr(handlers.rag, "answer_full", _answer_full)
    monkeypatch.setattr(handlers, "process_interaction", _process)
    monkeypatch.setattr(handlers.audio, "extract_audio", _extract_audio)
    return state


async def _only_row(session) -> m.Interaction:
    [row] = list(await session.scalars(sa.select(m.Interaction)))
    return row


async def test_a_spoken_question_is_answered_not_extracted(bound, harness):  # noqa: F811
    harness.heard = "Akmal bilan konteyner masalasi nima bo'lgandi"
    message = _media_message("voice")

    await handlers.on_voice(message, bot=None)

    [(text, markup)] = message.sent
    assert text.startswith(
        "🎙 <i>Savolingiz:</i> «Akmal bilan konteyner masalasi nima bo'lgandi»\n\n"
    )
    assert "<code>m1</code>" in text
    assert harness.extracted == []
    row = await _only_row(bound)
    assert row.processed is True and row.media["processed"] is True
    assert row.meta["kind"] == "question" and row.meta["via"] == "voice"
    assert row.meta["refs"] == ["m1"] and row.meta["answer_message_id"] == 901
    assert markup.inline_keyboard[0][0].callback_data == f"vq:n:{row.id}"


async def test_a_spoken_note_is_still_extracted(bound, harness):  # noqa: F811
    harness.heard = "Akmalga ertaga 5 mln beraman"
    message = _media_message("voice")

    await handlers.on_voice(message, bot=None)

    row = await _only_row(bound)
    assert harness.answered == [] and harness.extracted == [row.id]
    assert (row.meta or {}).get("kind") != "question"


async def test_question_particle_without_question_mark_routes_to_answer(
    bound,  # noqa: F811
    harness,
):
    harness.heard = "Akmal pulni berdimi"
    assert loops.ends_in_question_particle(fold_apostrophes(harness.heard))
    message = _media_message("voice")

    await handlers.on_voice(message, bot=None)

    assert harness.answered == [("Akmal pulni berdimi", "question")]
    assert harness.extracted == []


async def test_save_as_note_button_extracts_once(bound, harness):  # noqa: F811
    harness.heard = "Akmal pulni berdimi"
    await handlers.on_voice(_media_message("voice"), bot=None)
    row = await _only_row(bound)

    first = _Message()
    await handlers.on_save_as_note(_Callback(f"vq:n:{row.id}", first))
    again = _Message()
    await handlers.on_save_as_note(_Callback(f"vq:n:{row.id}", again))
    gone = _Message()
    await handlers.on_save_as_note(_Callback("vq:n:99999999", gone))

    assert first.sent[0][0].startswith(replies.SAVED_AS_NOTE + "\n")
    assert again.sent[0][0] == replies.ALREADY_SAVED
    assert gone.sent[0][0] == replies.QUESTION_GONE
    assert harness.extracted == [row.id]
    await bound.refresh(row)
    assert row.meta["kind"] == "note_from_question" and row.meta["converted_at"]
    assert row.search_indexed_at is None


async def test_video_note_question_is_answered(bound, harness):  # noqa: F811
    harness.heard = "Sardor nima deb o'ylaysan, ishonsa bo'ladimi?"
    message = _media_message("video_note")

    await handlers.on_video_note(message, bot=None)

    [(text, _)] = message.sent
    assert text.startswith("🎙 <i>Savolingiz:</i>")
    assert harness.answered and harness.answered[0][1] == "opinion"
    assert harness.extracted == []
