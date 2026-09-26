"""WP-17: one ranked queue of every tap-request, and a budget of 5-10 a day
(owner answer 3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource, PromiseMadeBy
from miya.services import approvals, money_events, phone_events, questions, sms_money
from miya.services.questions import Pending

TZ = settings.tz
BASE = datetime(2026, 9, 25, 10, 0, tzinfo=TZ)


def _item(kind="nudge", ref=None, *, stake="0", age_h=1, urgent=False, subject=None):
    return Pending(
        kind=kind,
        ref=ref or f"{kind}-{age_h}-{stake}",
        stake_rank=Decimal(stake),
        since=BASE - timedelta(hours=age_h),
        urgent=urgent,
        offers=0,
        subject=subject,
    )


def _plan(
    items, *, spent=0, slot=questions.SLOT_DAY, now=None, last_push=None, brief=True
):
    return questions.plan(
        items,
        spent=spent,
        slot=slot,
        now=now or BASE.replace(hour=14),
        last_push=last_push,
        brief_sent=brief,
    )


# --- the pure planner ---------------------------------------------------------------


def test_rank_is_money_then_kind_then_age():
    still_open = _item("still_open", "debt:1", stake=str(1000 * 12_500))
    claim = _item("claim", "c1", stake="5000000")
    missed = _item("missed", "m1", age_h=72)
    media = _item("media", "i1", age_h=120)
    ranked = sorted([media, missed, claim, still_open], key=questions.rank_key)
    assert [p.ref for p in ranked] == ["debt:1", "c1", "m1", "i1"]


def test_plan_never_exceeds_remaining():
    items = [_item(ref=f"q{i}", age_h=i) for i in range(5)]
    assert len(_plan(items, spent=8, now=BASE.replace(hour=9, minute=30))) <= 2


def test_brief_slot_takes_at_most_brief_slots():
    items = [_item(ref=f"q{i}", age_h=i) for i in range(9)]
    assert len(_plan(items, slot=questions.SLOT_BRIEF)) == 5


def test_evening_slot():
    items = [_item(ref=f"q{i}", age_h=i) for i in range(9)]
    assert len(_plan(items, spent=7, slot=questions.SLOT_EVENING)) == 3
    assert len(_plan(items, spent=9, slot=questions.SLOT_EVENING)) == 1


def test_daytime_keeps_the_evening_reserve_for_non_urgent():
    items = [_item(ref=f"q{i}", age_h=i) for i in range(6)]
    assert len(_plan(items, spent=5)) <= 2
    urgent = _item("claim", "c9", stake="20000000", urgent=True)
    assert [p.ref for p in _plan([urgent, *items], spent=8)] == ["c9"]


def test_gap_blocks_non_urgent_not_urgent():
    now = BASE.replace(hour=14)
    recent = now - timedelta(minutes=30)
    items = [_item(ref="q1"), _item("claim", "c9", stake="20000000", urgent=True)]
    assert [p.ref for p in _plan(items, now=now, last_push=recent)] == ["c9"]
    later = _plan(items, now=now, last_push=now - timedelta(hours=3))
    assert {p.ref for p in later} == {"q1", "c9"}


def test_before_the_brief_only_urgent():
    items = [_item(ref="q1"), _item("claim", "c9", stake="20000000", urgent=True)]
    assert [p.ref for p in _plan(items, brief=False)] == ["c9"]


def test_receipt_ignores_the_gap():
    now = BASE.replace(hour=14)
    items = [_item(ref="q1")]
    picked = questions.plan(
        items,
        spent=0,
        slot=questions.SLOT_DAY,
        now=now,
        last_push=now - timedelta(minutes=5),
        brief_sent=True,
        interrupting=False,
    )
    assert [p.ref for p in picked] == ["q1"]


def test_zero_budget_plans_nothing(monkeypatch):
    monkeypatch.setattr(settings, "question_budget_per_day", 0)
    urgent = _item("claim", "c9", stake="20000000", urgent=True)
    assert _plan([urgent]) == []


def test_group_digest_rows_count_against_the_budget():
    groups = _item("groups", "digest:x", subject=["g1", "g2", "g3", "g4", "g5"])
    [picked] = _plan([groups], spent=8, slot=questions.SLOT_EVENING)
    assert len(picked.subject) == 2
    assert _plan([groups], spent=10, slot=questions.SLOT_EVENING) == []


# --- the database side -----------------------------------------------------------------


async def test_spent_today_counts_from_tashkent_midnight(session):
    session.add_all(
        [
            m.QuestionLog(
                kind="claim",
                ref="c1",
                via="push",
                sent_at=datetime(2026, 9, 24, 23, 59, tzinfo=TZ),
            ),
            m.QuestionLog(
                kind="claim",
                ref="c2",
                via="push",
                sent_at=datetime(2026, 9, 25, 0, 1, tzinfo=TZ),
            ),
        ]
    )
    await session.flush()
    assert (
        await questions.spent_today(session, now=datetime(2026, 9, 25, 10, 0, tzinfo=TZ))
        == 1
    )


async def _claim(session, *, created_at, asked_at=None) -> m.Claim:
    row = m.Interaction(
        source=InteractionSource.assistant_bot, occurred_at=created_at, raw_text="x"
    )
    session.add(row)
    await session.flush()
    claim = m.Claim(
        interaction_id=row.id,
        person_name="Akmal",
        kind="debt",
        payload={"amount": "5000000", "currency": "UZS", "direction": "i_owe_them"},
        created_at=created_at,
        asked_at=asked_at,
    )
    session.add(claim)
    await session.flush()
    return claim


async def _offered(session, claim, *times):
    for at in times:
        session.add(
            m.QuestionLog(kind="claim", ref=f"c{claim.id}", via="push", sent_at=at)
        )
    await session.flush()


async def test_claim_reoffer_rule(session):
    now = datetime.now(TZ).replace(hour=15, minute=0)
    old = now - timedelta(days=20)
    today_once = await _claim(session, created_at=old)
    await _offered(session, today_once, now - timedelta(hours=2))
    yesterday = await _claim(session, created_at=old)
    await _offered(session, yesterday, now - timedelta(days=1))
    twice_recent = await _claim(session, created_at=old)
    await _offered(
        session, twice_recent, now - timedelta(days=5), now - timedelta(days=3)
    )
    week_ago = await _claim(session, created_at=old)
    await _offered(session, week_ago, now - timedelta(days=12), now - timedelta(days=8))

    refs = {p.ref for p in await questions.collect(session, now=now) if p.kind == "claim"}
    assert refs == {f"c{yesterday.id}", f"c{week_ago.id}"}


async def test_claim_shown_on_a_receipt_is_not_pushed_again_today(session):
    now = datetime.now(TZ).replace(hour=10, minute=10)
    claim = await _claim(
        session, created_at=now - timedelta(hours=1), asked_at=now - timedelta(minutes=10)
    )
    refs = {p.ref for p in await questions.collect(session, now=now)}
    assert f"c{claim.id}" not in refs
    tomorrow = {
        p.ref for p in await questions.collect(session, now=now + timedelta(days=1))
    }
    assert f"c{claim.id}" in tomorrow
    pulled = {p.ref for p in await questions.collect(session, now=now, for_push=False)}
    assert f"c{claim.id}" in pulled


async def _money_row(session, body, *, at):
    row = m.Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        occurred_at=at,
        raw_text=body,
        processed=True,
        needs_review=False,
        media={"type": "sms", "sender": "Payme"},
    )
    session.add(row)
    await session.flush()
    await money_events.apply_reading(
        session,
        row,
        sms_money.read(body, received_at=at),
        channel=money_events.channel_for_sms("Payme"),
        now=at,
    )
    await session.flush()
    return row


async def test_money_review_rows_enter_the_queue_once(session):
    now = datetime.now(TZ)
    row = await _money_row(session, "Oplata 250 000 sum", at=now - timedelta(hours=1))
    advert = await _money_row(
        session, "Кешбэк 3 000 сум зачислен на карту *1234", at=now - timedelta(hours=1)
    )
    declined = await _money_row(
        session, "Oplata 250 000 UZS otklonena. Karta *1234", at=now - timedelta(hours=1)
    )
    pushed = [p for p in await questions.collect(session, now=now) if p.kind == "money"]
    assert [p.ref for p in pushed] == [f"s{row.id}"]
    pulled = {p.ref for p in await questions.collect(session, now=now, for_push=False)}
    assert {f"s{advert.id}", f"s{declined.id}"} <= pulled

    await questions.record_shown(session, pushed, via=questions.VIA_PUSH, now=now)
    again = [p for p in await questions.collect(session, now=now) if p.kind == "money"]
    assert again == []
    assert row.media["money"]["asked_at"] == now.isoformat()


async def test_collect_sees_every_kind_and_record_shown_marks_each(session):
    now = datetime.now(TZ)
    claim = await _claim(session, created_at=now - timedelta(hours=1))
    await phone_events.ingest_call_events(
        session,
        "dev",
        [
            {
                "call_log_id": 1,
                "started_at": (now - timedelta(hours=2)).isoformat(),
                "duration_seconds": 0,
                "type": "missed",
                "number": "+998901234567",
                "contact_name": None,
                "sim_slot": 0,
            }
        ],
    )
    person = m.Person(display_name="Sardor", aliases=[])
    session.add(person)
    await session.flush()
    promise = m.Promise(
        made_by=PromiseMadeBy.them,
        person_id=person.id,
        description="hujjat yuboradi",
        created_at=now - timedelta(days=10),
    )
    chat = m.ChatMonitor(
        tg_chat_id=4242, chat_type=ChatType.private, title="Sardor", monitor_enabled=True
    )
    group = m.ChatMonitor(
        tg_chat_id=-4243, chat_type=ChatType.group, title="Yuk", monitor_enabled=False
    )
    question = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=4242,
        person_id=person.id,
        occurred_at=now - timedelta(hours=6),
        raw_text="Yuk qachon keladi?",
        meta={"tg_message_id": 1},
    )
    media = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        occurred_at=now - timedelta(hours=1),
        media={"approval": {"state": approvals.PENDING, "reason": "video"}},
    )
    session.add_all([promise, chat, group, question, media])
    await session.flush()
    money = await _money_row(session, "Oplata 250 000 sum", at=now - timedelta(hours=1))

    items = await questions.collect(session, now=now)
    kinds = {p.kind: p for p in items}
    assert kinds["claim"].ref == f"c{claim.id}"
    assert kinds["missed"].ref.startswith("m")
    assert kinds["still_open"].ref == f"promise:{promise.id}"
    assert kinds["nudge"].ref == f"q{question.id}"
    assert kinds["groups"].ref.startswith("digest:")
    assert kinds["media"].ref == f"i{media.id}"
    assert kinds["money"].ref == f"s{money.id}"

    shown = list(kinds.values())
    await questions.record_shown(session, shown, via=questions.VIA_BRIEF, now=now)
    assert claim.asked_at == now
    assert group.asked_at == now and group.offered_at == now and group.digest_shows == 1
    assert approvals.state_of(media) == approvals.ASKED
    assert media.media["approval"]["shown_at"] == now.isoformat()
    logs = list(await session.scalars(sa.select(m.QuestionLog)))
    assert len(logs) == 6 + len(kinds["groups"].subject)
    assert {row.sent_at for row in logs} == {now}
    ask_rows = await session.scalar(
        sa.select(sa.func.count(m.ReminderLog.id)).where(
            m.ReminderLog.kind == "ask:promise"
        )
    )
    assert ask_rows == 1
    assert await questions.spent_today(session, now=now) == len(logs)
