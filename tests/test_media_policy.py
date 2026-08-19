"""Media policy (spec §6). Pure decisions — no Telegram, no network."""

from __future__ import annotations

import pytest

from miya.config import settings
from miya.services.media_policy import MediaKind, plan_for


def plan(kind, *, vision=False, docs=True, filename=None, size=None):
    return plan_for(
        kind,
        vision_enabled=vision,
        docs_enabled=docs,
        filename=filename,
        size=size,
    )


def test_voice_is_always_transcribed():
    result = plan(MediaKind.voice)
    assert (result.download, result.transcribe, result.extract_audio) == (
        True,
        True,
        False,
    )


def test_a_video_note_gets_its_audio_track_pulled_first():
    result = plan(MediaKind.video_note)
    assert result.transcribe is True
    assert result.extract_audio is True


def test_photos_are_only_downloaded_where_vision_is_enabled():
    off = plan(MediaKind.photo, vision=False)
    assert (off.download, off.vision) == (False, False)
    assert off.skip_reason == "vision_disabled"

    on = plan(MediaKind.photo, vision=True)
    assert (on.download, on.vision) == (True, True)


def test_video_is_never_processed_automatically():
    result = plan(MediaKind.video)
    assert result.download is False
    assert result.skip_reason == "video_on_demand"


def test_stickers_and_gifs_are_dropped_entirely():
    assert plan(MediaKind.sticker).ignore is True


@pytest.mark.parametrize(
    "filename", ["hisob.pdf", "narxlar.xlsx", "shartnoma.docx", "list.csv"]
)
def test_supported_documents_are_read_locally(filename):
    result = plan(MediaKind.document, filename=filename, size=1024)
    assert (result.download, result.read_document) == (True, True)


def test_documents_are_skipped_when_the_chat_has_docs_off():
    result = plan(MediaKind.document, docs=False, filename="hisob.pdf", size=10)
    assert result.download is False
    assert result.skip_reason == "docs_disabled"


def test_an_oversized_document_is_never_downloaded():
    result = plan(
        MediaKind.document, filename="katalog.pdf", size=settings.doc_max_bytes + 1
    )
    assert result.download is False
    assert result.skip_reason == "too_large"


def test_an_unreadable_format_stays_metadata_only():
    result = plan(MediaKind.document, filename="arxiv.zip", size=1024)
    assert result.download is False
    assert result.skip_reason == "unsupported_type"

    # Legacy binary .xls is deliberately not supported.
    assert plan(MediaKind.document, filename="eski.xls", size=10).download is False


def test_plain_text_needs_no_handling():
    result = plan(MediaKind.text)
    assert (result.download, result.ignore) == (False, False)
