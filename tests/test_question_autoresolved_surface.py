"""WP-45: whatever MIYA settles by itself is said, and one tap undoes it."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType
from miya.services import approvals, chats, claims, reports
from tests.test_claims_autoresolve import _claim as _debt_claim
from tests.test_claims_bank_evidence import NOW, _claimed, _sms, _txn_claim
from tests.test_close_and_correct import (  # noqa: F401
    _Callback,
    _command,
    _Message,
    bound,
)


async def _bank_closed(session):
    await _sms(session, NOW - timedelta(minutes=10))
    claim, _ = await _claimed(session, _txn_claim())
    assert claim.state == claims.AUTO
    return claim


async def test_undo_returns_the_claim_to_the_queue_and_no_rule_closes_it_again(bound):  # noqa: F811
    claim = await _bank_closed(bound)
    message = _Message()
    await handlers.on_claim_button(_Callback(f"cl:u:{claim.id}", message))
    assert claim.state == claims.PENDING and claim.evidence_txn_id is None
    [(text, keyboard)] = message.sent
    assert text.startswith(f"↩️ <b>c{claim.id} yana ochiq</b>")
    assert f"cl:y:{claim.id}" in [
        b.callback_data for row in keyboard.inline_keyboard for b in row
    ]
    assert await claims.match_bank_evidence(bound, now=NOW) == []
    assert await claims.resolve_superseded(bound, now=NOW) == []
    assert claim.state == claims.PENDING


async def test_undo_is_refused_for_a_claim_the_owner_answered(session):
    claim = await _debt_claim(session)
    await claims.decline(session, claim.id, by=claims.BY_BUTTON)
    with pytest.raises(claims.AlreadyAnswered):
        await claims.reopen_auto(session, claim.id, by=claims.BY_BUTTON)


async def test_evening_report_counts_todays_auto_resolutions(session):
    await _bank_closed(session)
    today = datetime.now(settings.tz).date()
    data = await reports.gather(session, today)
    assert data.auto_resolved == 1
    assert reports.AUTO_RESOLVED_LINE.format(n=1) in reports.render_data_block(data)


async def test_savollar_hal_lists_bank_duplicate_own_channel_ignored_and_expired(
    bound,  # noqa: F811
    monkeypatch,
):
    now = datetime.now(settings.tz)
    bank = await _bank_closed(bound)
    # A duplicate closed by the primary's Ha.
    akmal = m.Person(display_name="Akmal", aliases=[])
    bound.add(akmal)
    await bound.flush()
    first = await _debt_claim(bound, minutes_ago=10)
    second = await _debt_claim(bound)
    await claims.accept(bound, first.id, by=claims.BY_BUTTON, now=now)
    # One the owner wrote himself.
    own = await _debt_claim(bound, 7_000_000)
    bound.add(
        m.Debt(
            person_id=akmal.id, direction="i_owe_them", amount=7_000_000, currency="UZS"
        )
    )
    await bound.flush()
    await claims.resolve_superseded(bound, now=now)
    # A channel and an ignored group, decided by rule.
    monkeypatch.setattr(settings, "question_ask_channels", False)
    bound.add(m.ChatMonitor(tg_chat_id=-1, chat_type=ChatType.channel, title="Kanal"))
    bound.add(
        m.ChatMonitor(
            tg_chat_id=-2,
            chat_type=ChatType.group,
            title="Guruh",
            digest_shows=settings.question_group_max_shows,
            offered_at=now - timedelta(days=2),
        )
    )
    await bound.flush()
    await chats.apply_default_rules(bound, now=now)
    # A file question nobody answered.
    photo = m.Interaction(
        source="telegram_userbot",
        direction="in",
        occurred_at=now - timedelta(days=5),
        media={"type": "photo", "approval": {"state": approvals.ASKED, "reason": "size"}},
    )
    bound.add(photo)
    await bound.flush()
    await approvals.expire_stale(bound, now=now)
    await bound.flush()

    message = _Message()
    await handlers.cmd_questions(message, _command("savollar", "hal"))
    [(text, keyboard)] = message.sent
    assert text.startswith(replies.AUTO_RESOLVED_HEADER)
    assert "bank SMS bilan tasdiqlandi" in text and f"c{bank.id}" in text
    assert f"takror da'vo, <code>c{first.id}</code>" in text and f"c{second.id}" in text
    assert f"c{own.id}" in text and "allaqachon yozgansan" in text
    assert "📢 Kanal — kanal, so'ramadim" in text
    assert "👥 Guruh — ikki marta javobsiz qoldi" in text
    assert "so'ralmay eskirdi" in text
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert f"cl:u:{bank.id}" in data and any(d.startswith("ng:y:") for d in data)


def test_undo_payload_fits_64_bytes():
    [button] = keyboards.undo_row(2_147_483_647)
    assert len(button.callback_data.encode()) <= 64


async def test_empty_week(bound):  # noqa: F811
    message = _Message()
    await handlers.cmd_questions(message, _command("savollar", "hal"))
    assert message.sent == [(replies.AUTO_RESOLVED_EMPTY, None)]
    assert await bound.scalar(sa.select(sa.func.count(m.Claim.id))) == 0
