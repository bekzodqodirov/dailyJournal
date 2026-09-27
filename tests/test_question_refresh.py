"""WP-83: a sent question batch loses the buttons of what was answered."""

from __future__ import annotations

from datetime import datetime

from miya.config import settings
from miya.db import models as m
from miya.services import claims, questions
from miya.worker import main as worker
from tests.test_claims_worker import _old_claims

TZ = settings.tz


class _EditBot:
    def __init__(self) -> None:
        self.edits: list[tuple[int, object]] = []

    async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
        self.edits.append((message_id, reply_markup))


def _rows(markup) -> list[list[str]]:
    if markup is None:
        return []
    return [[b.text for b in row] for row in markup.inline_keyboard]


async def _sent_batch(session, ids: list[int], message_id: int = 900) -> None:
    now = datetime.now(TZ)
    for claim_id in ids:
        session.add(
            m.QuestionLog(
                kind=questions.KIND_CLAIM,
                ref=claims.ref(claim_id),
                via="push",
                tg_message_id=message_id,
                sent_at=now,
            )
        )
    await session.commit()


async def test_answered_item_disappears_from_the_earlier_batch(session, monkeypatch):
    monkeypatch.setattr(questions, "_REFRESHED", {})
    first, second = await _old_claims(session, 2)
    await _sent_batch(session, [first, second])
    await claims.decline(session, first, by="button")
    await session.commit()
    bot = _EditBot()

    await worker.refresh_question_messages(bot, session, now=datetime.now(TZ))

    [(message_id, markup)] = bot.edits
    assert message_id == 900
    # The survivor keeps its number: it was 2 in the batch, it stays 2.
    assert _rows(markup) == [["2 ✅ Ha", "2 ✖️ Yo'q", "2 ✏️ Tuzat"]]


async def test_auto_resolved_item_disappears(session, monkeypatch):
    monkeypatch.setattr(questions, "_REFRESHED", {})
    [only] = await _old_claims(session, 1)
    await _sent_batch(session, [only])
    claim = await session.get(m.Claim, only)
    claim.state = claims.AUTO
    await session.commit()
    bot = _EditBot()

    await worker.refresh_question_messages(bot, session, now=datetime.now(TZ))

    assert bot.edits == [(900, None)]


async def test_nothing_is_edited_when_nothing_changed(session, monkeypatch):
    monkeypatch.setattr(questions, "_REFRESHED", {})
    ids = await _old_claims(session, 2)
    await _sent_batch(session, ids)
    bot = _EditBot()

    await worker.refresh_question_messages(bot, session, now=datetime.now(TZ))
    assert bot.edits == []

    await claims.decline(session, ids[0], by="button")
    await session.commit()
    await worker.refresh_question_messages(bot, session, now=datetime.now(TZ))
    await worker.refresh_question_messages(bot, session, now=datetime.now(TZ))
    assert len(bot.edits) == 1  # edited once, not every tick
