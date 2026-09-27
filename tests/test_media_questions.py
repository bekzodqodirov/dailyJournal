"""WP-46: file questions are pushed only where they matter and expire honestly."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

from miya.bot import replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import approvals, brief, questions
from miya.userbot import main as userbot

NOW = datetime.now(settings.tz)
_ids = itertools.count(1)


def _monitor(chat_type: ChatType) -> m.ChatMonitor:
    return m.ChatMonitor(tg_chat_id=-100, chat_type=chat_type)


def test_owner_own_video_is_listed_not_pushed():
    own = SimpleNamespace(out=True)
    assert userbot._media_pushable(own, _monitor(ChatType.private)) is False


def test_group_video_is_listed_only_by_default():
    theirs = SimpleNamespace(out=False)
    assert userbot._media_pushable(theirs, _monitor(ChatType.group)) is False


def test_media_ask_in_groups_true_pushes_it(monkeypatch):
    monkeypatch.setattr(settings, "media_ask_in_groups", True)
    theirs = SimpleNamespace(out=False)
    assert userbot._media_pushable(theirs, _monitor(ChatType.group)) is True


async def _file(session, *, pushable=None, state=approvals.PENDING, at=NOW, **approval):
    fields = {"state": state, "reason": "video", **approval}
    if pushable is not None:
        fields["pushable"] = pushable
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=-100,
        occurred_at=at,
        media={"type": "video", "size": 90_000_000, "approval": fields},
        meta={"tg_message_id": next(_ids)},
    )
    session.add(interaction)
    await session.flush()
    return interaction


def _media_items(items) -> list[int]:
    return [i.subject.id for i in items if i.kind == questions.KIND_MEDIA]


async def test_private_video_is_pushed_at_the_lowest_rank(session):
    private = await _file(session, pushable=True)
    group = await _file(session, pushable=False)
    pushed = await questions.collect(session, now=NOW, for_push=True)
    assert _media_items(pushed) == [private.id]
    assert pushed[-1].kind == questions.KIND_MEDIA
    pulled = await questions.collect(session, now=NOW, for_push=False)
    assert set(_media_items(pulled)) == {private.id, group.id}


async def test_never_shown_file_expires_after_7_days_not_48_hours(session):
    three_days = await _file(session, at=NOW - timedelta(days=3))
    eight_days = await _file(session, at=NOW - timedelta(days=8))
    assert await approvals.expire_stale(session, now=NOW) == 1
    assert approvals.state_of(three_days) == approvals.PENDING
    assert approvals.state_of(eight_days) == approvals.EXPIRED


async def test_shown_file_expires_48_hours_after_shown_at(session):
    recent = await _file(
        session,
        state=approvals.ASKED,
        at=NOW - timedelta(days=5),
        shown_at=(NOW - timedelta(hours=10)).isoformat(),
    )
    stale = await _file(
        session,
        state=approvals.ASKED,
        at=NOW - timedelta(days=5),
        shown_at=(NOW - timedelta(hours=49)).isoformat(),
    )
    assert await approvals.expire_stale(session, now=NOW) == 1
    assert approvals.state_of(recent) == approvals.ASKED
    assert approvals.state_of(stale) == approvals.EXPIRED


async def test_brief_says_how_many_files_expired(session):
    await _file(session, at=NOW - timedelta(days=8))
    await approvals.expire_stale(session, now=NOW)
    await session.flush()
    morning = await brief.gather(session)
    assert morning.media_expired == 1
    morning.money_review = 1  # something else to say, so it is not all-clear
    assert replies.MEDIA_EXPIRED_LINE.format(n=1) in replies.morning_brief(morning)


async def test_old_rows_without_pushable_are_still_asked(session):
    legacy = await _file(session)
    assert [r.id for r in await approvals.awaiting_question(session)] == [legacy.id]
