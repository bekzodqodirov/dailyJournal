"""Delivering counterparty claims (build step 3): the question reaches him.

A claim is a row nobody wrote yet, so the only thing that makes it real is
the question. The receipt for a window carries it with ✅ / ✖️ / ✏️ buttons;
what no receipt shows — the batch overflow, a bot or call interaction, a
clipped keyboard — the worker asks on its own after CLAIM_ASK_AFTER_MINUTES, a few
per sweep, never inside quiet hours, never twice. The brief lists them too.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.bot import keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import batch, claims
from miya.services import extraction as ex
from miya.worker import main as worker
from tests.test_batch import _window
from tests.test_chat_notices import _Bot, _monitored, _parked, _result

TZ = settings.tz
ASK_AFTER = timedelta(minutes=settings.claim_ask_after_minutes)


class _KeyboardBot(_Bot):
    """The chat-notice bot, also keeping the keyboard each message carried."""

    def __init__(self, *, reachable: bool = True) -> None:
        super().__init__(reachable=reachable)
        self.markups: list = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        await super().send_message(chat_id, text, **kwargs)
        self.markups.append(kwargs.get("reply_markup"))


def _buttons(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _them_debt(person: str = "Akmal") -> ex.ExtractedDebt:
    return ex.ExtractedDebt(
        direction="i_owe_them",
        person=person,
        amount=5_000_000,
        reason="yuk haqi",
        asserted_by="them",
    )


async def _interaction(session) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(TZ),
        raw_text="Akmal: sen menga 5 mln qarzsan",
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _old_claims(session, count: int, *, age: timedelta | None = None) -> list[int]:
    """Pending claims created ``age`` ago (by default just past the ask delay)."""
    age = ASK_AFTER + timedelta(minutes=1) if age is None else age
    interaction = await _interaction(session)
    created = datetime.now(TZ) - age
    ids = []
    for index in range(count):
        claim = await claims.create(
            session,
            interaction,
            claims.KIND_DEBT,
            _them_debt(),
            now=created + timedelta(seconds=index),
        )
        ids.append(claim.id)
    await session.commit()
    return ids


async def _asked_at(session, claim_id: int) -> datetime | None:
    # The column, not the instance: the job wrote through its own session.
    return await session.scalar(sa.select(m.Claim.asked_at).where(m.Claim.id == claim_id))


async def _landed_them_window(session, person: str = "Akmal") -> batch.AppliedWindow:
    await _monitored(session)
    window = await _window(session)
    landed = await batch._apply_result(
        session, window, _result(debts=[_them_debt(person)])
    )
    await session.commit()
    return landed


# --- the receipt asks --------------------------------------------------------


async def test_a_counterparty_debt_rides_on_the_receipt_as_a_question(
    session, monkeypatch
):
    landed = await _landed_them_window(session)
    [claim] = landed.applied.claims
    assert claim.state == claims.PENDING and claim.asked_at is None
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 0
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _KeyboardBot()

    await worker.chat_notice_job(bot)

    [text] = bot.sent
    assert "to'g'rimi" in text and "Akmal" in text
    [markup] = bot.markups
    assert _buttons(markup) == [
        f"cl:y:{claim.id}",
        f"cl:n:{claim.id}",
        f"cl:e:{claim.id}",
    ]
    assert await _asked_at(session, claim.id) is not None
    assert await batch.pending_notices(session) == []
    # Asked with the receipt, so the question queue has nothing left to ask.
    await worker.question_job(bot, now=datetime.now(TZ))
    assert len(bot.sent) == 1


async def test_an_unreachable_owner_leaves_the_claim_unasked(session, monkeypatch):
    landed = await _landed_them_window(session)
    [claim] = landed.applied.claims
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)

    await worker.chat_notice_job(_KeyboardBot(reachable=False))

    assert await _asked_at(session, claim.id) is None
    assert len(await batch.pending_notices(session)) == 1


async def test_a_receipt_without_claims_carries_no_keyboard(session, monkeypatch):
    await _monitored(session)
    window = await _window(session)
    await batch._apply_result(session, window, _result())
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _KeyboardBot()

    await worker.chat_notice_job(bot)

    assert bot.markups == [None]


async def test_only_the_claims_the_budget_allows_ride_on_the_receipt(
    session, monkeypatch
):
    """Each claim row is a tap-request (WP-18): the receipt carries what
    today's budget allows, says the rest are queued, and the question job
    asks the rest later — nothing is marked asked that was not shown."""
    now = datetime.now(TZ)
    interaction = _parked(0, "GZ logistika", now=now)
    session.add(interaction)
    await session.flush()
    total = keyboards.MAX_ROWS + 2
    ids = [
        (
            await claims.create(
                session,
                interaction,
                claims.KIND_DEBT,
                _them_debt(),
                now=now - ASK_AFTER - timedelta(seconds=total - i),
            )
        ).id
        for i in range(total)
    ]
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _KeyboardBot()

    await worker.chat_notice_job(bot)

    [markup] = bot.markups
    batch_max = settings.question_batch_max
    assert len(markup.inline_keyboard) == batch_max
    assert replies.CLAIMS_QUEUED in bot.sent[0]
    asked = [await _asked_at(session, i) for i in ids]
    assert sum(1 for a in asked if a is not None) == batch_max

    await worker.question_job(bot, now=datetime.now(TZ))
    asked = [await _asked_at(session, i) for i in ids]
    assert sum(1 for a in asked if a is not None) == 2 * batch_max


# --- the sweep asks the rest -------------------------------------------------


# --- the brief and the receipt's source line ---------------------------------


async def test_the_brief_keyboard_carries_the_claim_rows(session, monkeypatch):
    ids = await _old_claims(session, 2)
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _KeyboardBot()

    assert await worker.brief_job(bot) is True

    [text] = bot.sent
    assert replies.BRIEF_CLAIMS in text
    [markup] = bot.markups
    buttons = _buttons(markup)
    for claim_id in ids:
        assert f"cl:y:{claim_id}" in buttons and f"cl:n:{claim_id}" in buttons


async def test_a_claim_shown_on_the_brief_is_not_asked_again(session, monkeypatch):
    """The brief's Ha / Yo'q row is an ask: the sweep must not repeat it."""
    ids = await _old_claims(session, 2)
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _KeyboardBot()
    assert await worker.brief_job(bot) is True
    for claim_id in ids:
        assert await _asked_at(session, claim_id) is not None

    await worker.question_job(bot, now=datetime.now(TZ))
    assert len(bot.sent) == 1


async def test_a_claim_already_asked_does_not_ride_on_the_receipt_again(
    session, monkeypatch
):
    """After quiet hours the sweep may beat the receipt; the owner already
    holds that row, so the receipt carries no second one."""
    landed = await _landed_them_window(session)
    [claim] = landed.applied.claims
    claims.mark_asked(claim)
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _KeyboardBot()

    await worker.chat_notice_job(bot)

    [text] = bot.sent
    assert "to'g'rimi?" in text  # the line stays; only the buttons are gone
    assert _buttons(bot.markups[0]) == []


async def test_the_receipt_names_the_claimant_not_who_spoke_first(session):
    sardor = m.Person(display_name="Sardor")
    session.add(sardor)
    await session.flush()
    await _monitored(session)
    window = await _window(session)
    window.person_id = sardor.id

    landed = await batch._apply_result(session, window, _result(debts=[_them_debt()]))

    assert landed.people == ["Akmal"]
    assert landed.notice is not None and "Akmal" in landed.notice


async def test_people_named_lists_rows_then_claimants_without_repeats(session):
    await _monitored(session)
    window = await _window(session)
    result = _result(
        debts=[
            ex.ExtractedDebt(
                direction="they_owe_me", person="Akmal", amount=1_000_000, reason="yuk"
            ),
            _them_debt("Akmal"),
            _them_debt("Bobur"),
            ex.ExtractedDebt(
                direction="i_owe_them",
                person="",
                amount=2_000_000,
                reason="nameless",
                asserted_by="them",
            ),
        ]
    )

    landed = await batch._apply_result(session, window, result)

    assert landed.people == ["Akmal", "Bobur"]
    assert len(landed.applied.claims) == 3
