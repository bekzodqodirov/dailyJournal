"""WP-84: answering a question batch by typing "1 ha 2 yo'q"."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.services import claims, question_replies, questions
from tests.test_claims_worker import _old_claims
from tests.test_close_and_correct import bound  # noqa: F401

TZ = settings.tz


def _reply(text: str, to: int = 900):
    message = SimpleNamespace(
        text=text,
        date=datetime.now(TZ),
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        reply_to_message=SimpleNamespace(message_id=to),
        sent=[],
    )

    async def answer(body, **kwargs):
        message.sent.append(body)

    message.answer = answer
    return message


async def _typing(message):
    return None


@pytest.fixture
def quiet_handlers(monkeypatch):
    monkeypatch.setattr(handlers, "_typing", _typing)


async def _batch(session, ids, message_id: int = 900) -> None:
    for claim_id in ids:
        session.add(
            m.QuestionLog(
                kind=questions.KIND_CLAIM,
                ref=claims.ref(claim_id),
                via="push",
                tg_message_id=message_id,
                sent_at=datetime.now(TZ),
            )
        )
    await session.flush()


async def _state(session, claim_id) -> str:
    return await session.scalar(sa.select(m.Claim.state).where(m.Claim.id == claim_id))


async def test_numbers_and_verbs_route_like_buttons(bound, quiet_handlers):  # noqa: F811
    first, second = await _old_claims(bound, 2)
    await _batch(bound, [first, second])

    message = _reply("1 ha 2 yo'q")
    await handlers.on_text(message)

    assert await _state(bound, first) == claims.ACCEPTED
    assert await _state(bound, second) == claims.DECLINED
    [text] = message.sent
    assert text.startswith(replies.QUESTION_REPLY_DONE.format(list="1 ha, 2 yoq"))


async def test_hammasi_ha(bound, quiet_handlers):  # noqa: F811
    ids = await _old_claims(bound, 3)
    await _batch(bound, ids)

    await handlers.on_text(_reply("hammasi ha"))

    assert [await _state(bound, i) for i in ids] == [claims.ACCEPTED] * 3


async def test_amount_edit_then_ha(bound, quiet_handlers):  # noqa: F811
    [only] = await _old_claims(bound, 1)
    await _batch(bound, [only])

    await handlers.on_text(_reply("1 4 mln ha"))

    assert await _state(bound, only) == claims.ACCEPTED
    debt = await bound.scalar(sa.select(m.Debt))
    assert debt.amount == Decimal("4000000")


async def test_unparsed_text_is_a_normal_note(bound, quiet_handlers, monkeypatch):  # noqa: F811
    [only] = await _old_claims(bound, 1)
    await _batch(bound, [only])
    seen = []

    async def _process(session, interaction):
        seen.append(interaction.raw_text)
        return SimpleNamespace(ok=True, applied=handlers.persistence.Applied())

    monkeypatch.setattr(handlers, "process_interaction", _process)
    monkeypatch.setattr(handlers.rag, "classify", lambda text: "note")

    await handlers.on_text(_reply("Akmal ertaga keladi"))

    assert seen == ["Akmal ertaga keladi"]
    assert await _state(bound, only) == claims.PENDING


async def test_reply_to_a_non_question_message_is_ignored(
    bound,  # noqa: F811
    quiet_handlers,
    monkeypatch,
):
    [only] = await _old_claims(bound, 1)
    await _batch(bound, [only])
    seen = []

    async def _process(session, interaction):
        seen.append(interaction.raw_text)
        return SimpleNamespace(ok=True, applied=handlers.persistence.Applied())

    monkeypatch.setattr(handlers, "process_interaction", _process)
    monkeypatch.setattr(handlers.rag, "classify", lambda text: "note")

    await handlers.on_text(_reply("1 ha", to=12345))

    assert seen == ["1 ha"]
    assert await _state(bound, only) == claims.PENDING


def test_the_parser_never_guesses():
    assert question_replies.parse("1 5 mln yo'q", 2) is None
    assert question_replies.parse("3 ha", 2) is None
    assert question_replies.parse("1 ha 1 yo'q", 2) is None
    assert question_replies.parse("salom", 2) is None
