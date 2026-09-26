"""WP-18: batch-safe keyboards."""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from miya.bot import keyboards

BIG = 2**31 - 1


def _markup(*payloads: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="x", callback_data=p)] for p in payloads
        ]
    )


def test_without_prefixed_does_not_cross_kinds():
    markup = _markup("md:y:55", "cl:y:55", "ng:y:55")
    left = keyboards.without_prefixed(markup, "md", 55)
    assert [r[0].callback_data for r in left.inline_keyboard] == ["cl:y:55", "ng:y:55"]


def _items():
    return [
        SimpleNamespace(kind="claim", subject=SimpleNamespace(id=BIG)),
        SimpleNamespace(kind="missed", subject=SimpleNamespace(interaction_id=BIG)),
        SimpleNamespace(kind="nudge", subject=SimpleNamespace(interaction_id=BIG)),
        SimpleNamespace(kind="media", subject=SimpleNamespace(id=BIG)),
        SimpleNamespace(kind="money", subject=SimpleNamespace(id=BIG)),
        SimpleNamespace(
            kind="still_open", subject=SimpleNamespace(refs=[("promise", BIG)])
        ),
        SimpleNamespace(kind="still_open", subject=SimpleNamespace(refs=[("debt", BIG)])),
    ]


def test_every_batch_payload_fits_64_bytes():
    markup = keyboards.question_batch(_items())
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_numbered_labels():
    markup = keyboards.question_batch(_items())
    assert keyboards.is_numbered(markup)
    firsts = [row[0].text for row in markup.inline_keyboard]
    assert firsts == [
        "1 ✅ Ha",
        "2 ✅ Bog'landim",
        "3 ✅ Javob berdim",
        "4 ✅ O'qi",
        "5 📉 Chiqim",
        "6 Ha, ochiq",
        "7 Ha, ochiq",
    ]
    debt_row = markup.inline_keyboard[-1]
    assert all("Yop" not in b.text for b in debt_row)
    assert not keyboards.is_numbered(keyboards.media_approval(1))
