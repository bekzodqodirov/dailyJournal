"""Build step 6, the service half: call-log and SMS ingest, and the loops.

What the phone uploads is metadata, so the guarantees worth pinning are
bookkeeping ones: a retried batch never becomes a second row, a call-log
event never wears the recording's ``call_id`` (the phone deletes audio it
is told is a duplicate), a money SMS becomes exactly one transaction with
no model call, and a missed call rides the open loops until any real
contact — or the owner's ✅ — discharges it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    TransactionType,
)
from miya.services import call_recordings as cr
from miya.services import loops, nudges, phone_events, sms_money

TZ = settings.tz
DEVICE = "b7f1c2e0-0000-4000-8000-000000000001"


def _now() -> datetime:
    return datetime.now(TZ)


def _ago(**kwargs) -> datetime:
    return _now() - timedelta(**kwargs)


def _call(call_log_id=1, *, at=None, type="missed", number="+998901234567", **over):
    event = {
        "call_log_id": call_log_id,
        "started_at": (at or _ago(hours=2)).isoformat(),
        "duration_seconds": 0,
        "type": type,
        "number": number,
        "contact_name": None,
        "sim_slot": 0,
    }
    event.update(over)
    return event


def _sms(sms_id=1, *, at=None, sender="Payme", body="Oplata 25 000 sum", **over):
    message = {
        "sms_id": sms_id,
        "sender": sender,
        "received_at": (at or _ago(hours=1)).isoformat(),
        "body": body,
        "sim_slot": 0,
    }
    message.update(over)
    return message


async def _person(session, name="Akmal", phone=None) -> m.Person:
    person = m.Person(display_name=name, aliases=[], phone=phone)
    session.add(person)
    await session.flush()
    return person


async def _rows(session) -> list[m.Interaction]:
    return list(
        await session.scalars(sa.select(m.Interaction).order_by(m.Interaction.id))
    )


# --- batches, counts, idempotency ---------------------------------------------


async def test_a_call_batch_counts_accepted_and_rejected(session):
    outcome = await phone_events.ingest_call_events(
        session,
        DEVICE,
        [
            _call(1, type="incoming", duration_seconds=65),
            _call(2, type="missed"),
            _call(3, type="ringing"),  # not a CALL_TYPES member
            {"call_log_id": "four"},  # unusable
        ],
    )
    await session.commit()

    assert outcome.accepted == 2
    assert outcome.duplicates == 0
    assert [index for index, _ in outcome.rejected] == [2, 3]
    assert all(reason for _, reason in outcome.rejected)
    rows = await _rows(session)
    assert len(rows) == 2
    assert rows[0].direction is Direction.in_
    assert rows[1].direction is Direction.na


async def test_reposting_the_same_batch_is_all_duplicates(session):
    batch = [_call(1), _call(2, type="outgoing")]
    first = await phone_events.ingest_call_events(session, DEVICE, batch)
    await session.commit()
    second = await phone_events.ingest_call_events(session, DEVICE, batch)
    await session.commit()

    assert (first.accepted, first.duplicates) == (2, 0)
    assert (second.accepted, second.duplicates) == (0, 2)
    assert second.rejected == []
    assert len(await _rows(session)) == 2


async def test_the_same_event_twice_in_one_batch_is_one_row(session):
    outcome = await phone_events.ingest_call_events(session, DEVICE, [_call(7), _call(7)])
    await session.commit()

    assert (outcome.accepted, outcome.duplicates) == (1, 1)
    assert len(await _rows(session)) == 1


async def test_the_savepoint_catches_the_race_the_preselect_missed(session, monkeypatch):
    """The unique index, not the SELECT, is the guarantee: with the
    pre-select blinded, a duplicate insert lands on ux_interactions_event_key
    inside its SAVEPOINT and the rest of the batch still goes through."""
    await phone_events.ingest_call_events(session, DEVICE, [_call(1)])
    await session.commit()

    async def blind(session_, keys):
        return set()

    monkeypatch.setattr(phone_events, "_existing_event_keys", blind)
    outcome = await phone_events.ingest_call_events(session, DEVICE, [_call(1), _call(2)])
    await session.commit()

    assert (outcome.accepted, outcome.duplicates) == (1, 1)
    assert outcome.rejected == []
    assert len(await _rows(session)) == 2


async def test_sms_ids_survive_a_provider_wipe_via_the_content_hash(session):
    """Android restarts Sms._ID after a wipe: same id, different message —
    the content hash keeps the keys apart and both messages land."""
    first = await phone_events.ingest_sms(session, DEVICE, [_sms(1, body="birinchi")])
    await session.commit()
    second = await phone_events.ingest_sms(
        session, DEVICE, [_sms(1, body="boshqa xabar")]
    )
    await session.commit()

    assert first.accepted == 1 and second.accepted == 1
    assert len(await _rows(session)) == 2
    key = phone_events.sms_event_key(DEVICE, 1, "Payme", _now(), "matn")
    assert re.fullmatch(rf"{re.escape(DEVICE)}:sms:1:[0-9a-f]{{16}}", key)


# --- who the row belongs to ---------------------------------------------------


async def test_a_number_matches_a_person_by_its_nine_digit_tail(session):
    akmal = await _person(session, "Akmal", phone="+998 90 123-45-67")
    await phone_events.ingest_call_events(
        session, DEVICE, [_call(1, number="998901234567")]
    )
    await session.commit()

    [row] = await _rows(session)
    assert row.person_id == akmal.id
    assert await session.scalar(sa.select(sa.func.count(m.Person.id))) == 1


async def test_a_contact_name_resolves_through_resolve_person(session):
    """The phone's address book is trusted, like the recording sidecar."""
    await phone_events.ingest_call_events(
        session,
        DEVICE,
        [_call(1, type="incoming", contact_name="Akmal aka", number="+998901234567")],
    )
    await session.commit()

    person = await session.scalar(sa.select(m.Person))
    assert person is not None
    assert person.display_name == "Akmal aka"
    assert person.phone == "998901234567"
    [row] = await _rows(session)
    assert row.person_id == person.id


async def test_a_bare_number_never_creates_a_person(session):
    await phone_events.ingest_call_events(
        session, DEVICE, [_call(1, number="+998907654321")]
    )
    await session.commit()

    [row] = await _rows(session)
    assert row.person_id is None
    assert await session.scalar(sa.select(sa.func.count(m.Person.id))) == 0


async def test_an_alphanumeric_sms_sender_never_reaches_find_by_phone(session):
    await _person(session, "Akmal", phone="+998901234567")
    outcome = await phone_events.ingest_sms(
        session,
        DEVICE,
        [
            _sms(1, sender="PAYME"),
            _sms(2, sender="+998901234567", body="salom"),
        ],
    )
    await session.commit()

    assert outcome.accepted == 2
    rows = await _rows(session)
    assert rows[0].person_id is None  # a bank name is not a phone number
    assert rows[1].person_id is not None  # a numeric sender is


# --- the row shapes -----------------------------------------------------------


async def test_the_missed_call_row_shape(session):
    at = _ago(hours=3)
    await phone_events.ingest_call_events(
        session,
        DEVICE,
        [_call(4711, at=at, type="missed", contact_name="Akmal", number="+998901234567")],
    )
    await session.commit()

    [row] = await _rows(session)
    assert row.source is InteractionSource.phone_call
    assert row.direction is Direction.na  # a missed ring has no conversation
    assert row.occurred_at == at  # the client's timestamp, verbatim
    assert row.processed is True and row.needs_review is False
    assert row.raw_text == "[qo'ng'iroq ✖ Akmal — javobsiz]"  # /tarix shows this
    assert row.media["type"] == phone_events.MEDIA_CALL_LOG
    assert row.media["event_key"] == f"{DEVICE}:call:4711"
    assert row.media["call_type"] == "missed"
    assert row.media["phone"] == "+998901234567"
    assert row.media["contact_name"] == "Akmal"
    assert row.media["duration_seconds"] == 0
    assert row.media["device_id"] == DEVICE
    assert "call_id" not in row.media  # the recording hazard, see below


async def test_the_recording_of_a_logged_call_is_not_a_duplicate(session):
    """THE HAZARD, pinned: the phone deletes audio the server calls a
    duplicate. The call-log event of call 4711 arrives first; when the
    recording uploader then probes with the call_id it mints for that same
    call ("<device_id>:<CallLog._ID>"), it must find nothing."""
    at = _ago(minutes=50)
    await phone_events.ingest_call_events(
        session, DEVICE, [_call(4711, at=at, type="outgoing")]
    )
    await session.commit()

    recording_call_id = f"{DEVICE}:4711"
    assert (
        await cr.find_ingested(
            session, "0" * 64, call_id=recording_call_id, occurred_at=at
        )
        is None
    )
    # The probe path passes no occurred_at; still nothing.
    assert await cr.find_ingested(session, "0" * 64, call_id=recording_call_id) is None
    assert not await cr.already_ingested(
        session, "0" * 64, call_id=recording_call_id, occurred_at=at
    )


# --- money SMS ----------------------------------------------------------------

PAYME_BODY = (
    "Оплата 25 000 сум\nUZCARD *1234\nKORZINKA.UZ\n⏰ 14:30 16.09.2026\n"
    "Остаток: 1 250 000 сум"
)


async def test_a_payment_sms_becomes_exactly_one_transaction(session):
    at = _ago(hours=1)
    outcome = await phone_events.ingest_sms(
        session, DEVICE, [_sms(9, at=at, sender="Payme", body=PAYME_BODY)]
    )
    await session.commit()

    assert outcome.accepted == 1
    [row] = await _rows(session)
    assert row.source is InteractionSource.phone_sms
    assert row.direction is Direction.in_
    assert row.occurred_at == at
    assert row.raw_text == PAYME_BODY
    assert row.needs_review is False
    assert row.media["payment"] == {
        "amount": "25000.00",
        "currency": "UZS",
        "card_last4": "1234",
        "merchant": "KORZINKA.UZ",
        "balance_after": "1250000.00",
    }

    [txn] = list(await session.scalars(sa.select(m.Transaction)))
    assert txn.type is TransactionType.expense
    assert txn.amount == Decimal("25000.00")
    assert txn.currency is Currency.UZS
    assert txn.category == "oziq-ovqat"
    assert txn.description == "Payme: KORZINKA.UZ"
    assert txn.counterparty_person_id is None  # a bank is not a counterparty
    assert txn.occurred_at == at
    assert txn.source_interaction_id == row.id

    # The bank's record spent no model tokens: nothing was billed at all.
    assert await session.scalar(sa.select(sa.func.count(m.UsageLog.id))) == 0


async def test_a_reposted_payment_sms_still_means_one_transaction(session):
    message = _sms(9, sender="Payme", body=PAYME_BODY)
    await phone_events.ingest_sms(session, DEVICE, [message])
    await session.commit()
    outcome = await phone_events.ingest_sms(session, DEVICE, [message])
    await session.commit()

    assert (outcome.accepted, outcome.duplicates) == (0, 1)
    assert await session.scalar(sa.select(sa.func.count(m.Transaction.id))) == 1


async def test_a_low_confidence_bank_sms_is_flagged_not_invented(session):
    outcome = await phone_events.ingest_sms(
        session,
        DEVICE,
        [_sms(3, sender="Payme", body="Оплата произведена\nОстаток: 500 000 сум")],
    )
    await session.commit()

    assert outcome.accepted == 1
    [row] = await _rows(session)
    assert row.needs_review is True  # the owner's /tekshir eye
    assert "payment" not in row.media
    assert await session.scalar(sa.select(sa.func.count(m.Transaction.id))) == 0
    assert await session.scalar(sa.select(sa.func.count(m.UsageLog.id))) == 0


async def test_a_non_payment_sms_is_stored_and_nothing_more(session):
    outcome = await phone_events.ingest_sms(
        session, DEVICE, [_sms(4, sender="Beeline", body="Sizga 5GB sovg'a!")]
    )
    await session.commit()

    assert outcome.accepted == 1
    [row] = await _rows(session)
    assert row.needs_review is False
    assert row.processed is True
    assert "payment" not in row.media
    assert await session.scalar(sa.select(sa.func.count(m.Transaction.id))) == 0


async def test_an_oversized_or_broken_sms_is_rejected_with_its_reason(session):
    outcome = await phone_events.ingest_sms(
        session,
        DEVICE,
        [
            _sms(1, body="x" * (phone_events.SMS_BODY_MAX_CHARS + 1)),
            _sms(2, sender=""),
            _sms(3, received_at="2026-09-16T14:30:00"),  # naive: no offset
            _sms(4),
        ],
    )
    await session.commit()

    assert outcome.accepted == 1
    assert [index for index, _ in outcome.rejected] == [0, 1, 2]
    assert len(await _rows(session)) == 1


# --- the missed-call loop -----------------------------------------------------


async def _missed(session, *, at, phone="+998901234567", person=None, call_type="missed"):
    row = m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.na,
        person_id=person.id if person else None,
        occurred_at=at,
        raw_text="[qo'ng'iroq ✖ — javobsiz]",
        processed=True,
        media={
            "type": phone_events.MEDIA_CALL_LOG,
            "event_key": f"{DEVICE}:call:{at.timestamp()}:{id(at)}",
            "call_type": call_type,
            "phone": phone,
            "contact_name": None,
            "duration_seconds": 0,
            "device_id": DEVICE,
        },
    )
    session.add(row)
    await session.flush()
    return row


async def _contact(session, person, *, at, source=InteractionSource.telegram_userbot):
    row = m.Interaction(
        source=source,
        direction=Direction.in_,
        person_id=person.id,
        occurred_at=at,
        raw_text="salom",
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_missed_call_is_an_open_loop_with_the_house_shape(session):
    akmal = await _person(session, "Akmal", phone="+998901234567")
    ring = await _missed(session, at=_ago(hours=2), person=akmal)

    [loop] = await loops.missed_calls(session)

    assert loop.kind == loops.KIND_MISSED
    assert loop.ref == f"m{ring.id}"
    assert loop.interaction_id == ring.id
    assert loop.person is akmal and loop.person_name == "Akmal"
    assert loop.phone == "+998901234567"
    assert loop.attempts == 1
    assert loop.called_at == ring.occurred_at
    assert timedelta(hours=1, minutes=59) < loop.age < timedelta(hours=2, minutes=1)

    result = await loops.open_loops(session)
    assert [type(x).__name__ for x in result.ordered] == ["MissedCall"]
    assert result.missed == result.ordered
    assert not result.is_empty()


async def test_a_fresh_missed_call_waits_for_the_threshold(session):
    akmal = await _person(session)
    await _missed(session, at=_ago(minutes=10), person=akmal)

    assert await loops.missed_calls(session) == []
    assert len(await loops.missed_calls(session, minutes=5)) == 1


async def test_an_old_missed_call_ages_out(session):
    akmal = await _person(session)
    await _missed(session, at=_ago(days=4), person=akmal)

    assert await loops.missed_calls(session) == []
    assert len(await loops.missed_calls(session, max_days=7)) == 1


async def test_any_later_contact_with_the_person_closes_the_loop(session):
    """The difference from unanswered_questions: ANY source, ANY direction."""
    akmal = await _person(session, "Akmal")
    await _missed(session, at=_ago(hours=5), person=akmal)
    assert len(await loops.missed_calls(session)) == 1

    # a) a Telegram message from him
    telegram = await _contact(session, akmal, at=_ago(hours=4))
    assert await loops.missed_calls(session) == []
    await session.delete(telegram)
    await session.flush()

    # b) an answered incoming call, as the call log reports it
    answered = m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.in_,
        person_id=akmal.id,
        occurred_at=_ago(hours=3),
        media={
            "type": phone_events.MEDIA_CALL_LOG,
            "call_type": "incoming",
            "event_key": f"{DEVICE}:call:answered",
            "phone": "+998901234567",
        },
    )
    session.add(answered)
    await session.flush()
    assert await loops.missed_calls(session) == []
    await session.delete(answered)
    await session.flush()

    # c) the owner's note into the bot about him
    note = await _contact(
        session, akmal, at=_ago(hours=1), source=InteractionSource.assistant_bot
    )
    assert await loops.missed_calls(session) == []
    await session.delete(note)
    await session.flush()

    assert len(await loops.missed_calls(session)) == 1


async def test_calling_a_stranger_back_closes_it_by_the_phone_tail(session):
    await _missed(session, at=_ago(hours=5), phone="+998907654321")
    assert len(await loops.missed_calls(session)) == 1

    call_back = m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.out,
        occurred_at=_ago(hours=4),
        media={
            "type": phone_events.MEDIA_CALL_LOG,
            "call_type": "outgoing",
            "event_key": f"{DEVICE}:call:back",
            # A different spelling of the same number: the 9-digit tail agrees.
            "phone": "998 90 765-43-21",
        },
    )
    session.add(call_back)
    await session.flush()

    assert await loops.missed_calls(session) == []


async def test_another_missed_ring_joins_the_loop_instead_of_closing_it(session):
    akmal = await _person(session, "Akmal")
    first = await _missed(session, at=_ago(hours=6), person=akmal)
    await _missed(session, at=_ago(hours=3), person=akmal, call_type="rejected")
    stranger = await _missed(session, at=_ago(hours=2), phone="+998907654321")

    result = await loops.missed_calls(session)

    assert [loop.ref for loop in result] == [f"m{first.id}", f"m{stranger.id}"]
    assert result[0].attempts == 2  # one loop per number, oldest carries it
    assert result[1].attempts == 1


async def test_the_answered_mark_closes_the_number(session):
    akmal = await _person(session, "Akmal")
    ring = await _missed(session, at=_ago(hours=6), person=akmal)
    await _missed(session, at=_ago(hours=3), person=akmal)

    nudges.mark_answered(ring, now=_ago(hours=2))
    await session.flush()

    # ✅ "Bog'landim" resets the number like an outgoing reply resets a chat:
    # the later ring from the same person is discharged too.
    assert await loops.missed_calls(session) == []

    # ... but a ring after the mark opens a fresh loop.
    fresh = await _missed(session, at=_ago(hours=1), person=akmal)
    [loop] = await loops.missed_calls(session)
    assert loop.ref == f"m{fresh.id}" and loop.attempts == 1


async def test_the_stake_is_the_callers_open_debt(session):
    akmal = await _person(session, "Akmal")
    session.add(
        m.Debt(
            direction=DebtDirection.they_owe_me,
            person_id=akmal.id,
            amount=Decimal("5000000"),
            currency=Currency.UZS,
        )
    )
    await session.flush()
    await _missed(session, at=_ago(hours=2), person=akmal)
    await _missed(session, at=_ago(hours=3), phone="+998907654321")

    result = await loops.missed_calls(session)

    by_ref = {loop.person_name: loop.stake_rank for loop in result}
    assert by_ref["Akmal"] == Decimal("5000000")
    assert by_ref["?"] == Decimal(0)
    # The money orders the union, exactly like every other loop.
    ordered = (await loops.open_loops(session)).ordered
    assert ordered[0].person_name == "Akmal"


async def test_ingested_missed_calls_flow_into_the_loop_end_to_end(session):
    akmal = await _person(session, "Akmal", phone="+998901234567")
    await phone_events.ingest_call_events(
        session, DEVICE, [_call(21, at=_ago(hours=2), type="missed")]
    )
    await session.commit()

    [loop] = await loops.missed_calls(session)
    assert loop.person.id == akmal.id

    # He calls back: the next call-log batch closes the loop by person.
    await phone_events.ingest_call_events(
        session, DEVICE, [_call(22, at=_ago(minutes=50), type="outgoing")]
    )
    await session.commit()
    assert await loops.missed_calls(session) == []


# --- the nudge cadence --------------------------------------------------------


async def test_a_missed_call_is_nudged_once_plus_once_after_a_snooze(session):
    akmal = await _person(session, "Akmal")
    ring = await _missed(session, at=_ago(hours=6), person=akmal)
    now = _now()

    [due] = await nudges.collect_missed(session, now=now)
    assert due.interaction_id == ring.id

    nudges.mark_missed_nudged(session, [due])
    await session.flush()
    log_row = await session.scalar(sa.select(m.ReminderLog))
    assert log_row.kind == nudges.MISSED_KIND and log_row.ref == str(ring.id)

    # Once, ever: the loop stays in the brief but the sweep is done with it.
    assert await nudges.collect_missed(session, now=now) == []

    # "⏰ Ertaga" earns exactly one more after the snooze expires.
    until = now + timedelta(hours=12)
    nudges.snooze(ring, until=until)
    await session.flush()
    assert await nudges.collect_missed(session, now=now + timedelta(hours=1)) == []
    [again] = await nudges.collect_missed(session, now=until + timedelta(hours=1))
    assert again.interaction_id == ring.id
    # The post-snooze nudge, stamped when that sweep would really run
    # (ReminderLog's default sent_at is the wall clock, which this test is
    # ahead of).
    session.add(
        m.ReminderLog(
            kind=nudges.MISSED_KIND,
            ref=str(again.interaction_id),
            sent_at=until + timedelta(hours=1),
        )
    )
    await session.flush()
    assert await nudges.collect_missed(session, now=until + timedelta(hours=2)) == []


async def test_question_and_missed_nudge_ledgers_stay_apart(session):
    """A question nudge for interaction N must not spend the missed-call
    nudge of interaction N — the reminder_log kinds differ."""
    akmal = await _person(session, "Akmal")
    ring = await _missed(session, at=_ago(hours=6), person=akmal)
    session.add(m.ReminderLog(kind=nudges.NUDGE_KIND, ref=str(ring.id)))
    await session.flush()

    [due] = await nudges.collect_missed(session)
    assert due.interaction_id == ring.id


# --- the parser wiring is deterministic ---------------------------------------


async def test_no_extraction_path_is_touched_by_phone_events(session, monkeypatch):
    """A bank SMS is the bank's record: nothing here may reach the model.
    Both extraction entry points explode if anything tries."""
    from miya.services import extraction, ingest

    async def boom(*args, **kwargs):  # pragma: no cover - the trap itself
        raise AssertionError("phone events must never reach the model")

    monkeypatch.setattr(extraction, "extract", boom)
    monkeypatch.setattr(ingest, "process_interaction", boom)

    await phone_events.ingest_call_events(session, DEVICE, [_call(1)])
    outcome = await phone_events.ingest_sms(
        session,
        DEVICE,
        [
            _sms(1, sender="Payme", body=PAYME_BODY),
            _sms(2, sender="Payme", body="hech narsa"),
            _sms(3, sender="Beeline", body="reklama"),
        ],
    )
    await session.commit()

    assert outcome.accepted == 3
    assert await session.scalar(sa.select(sa.func.count(m.Transaction.id))) == 1
    assert await session.scalar(sa.select(sa.func.count(m.UsageLog.id))) == 0


def test_the_call_line_covers_every_call_type():
    for call_type in phone_events.CALL_TYPES:
        line = phone_events._call_line(call_type, "Akmal")
        assert line.startswith("[qo'ng'iroq") and "Akmal" in line
    assert "javobsiz" in phone_events._call_line("missed", "Akmal")
    assert phone_events._call_line("incoming", "X") != phone_events._call_line(
        "outgoing", "X"
    )


def test_low_and_high_share_the_constants_the_ingester_reads():
    assert sms_money.HIGH == "high" and sms_money.LOW == "low"
    assert phone_events.MEDIA_CALL_LOG == "call_log"
    assert phone_events.MEDIA_SMS == "sms"
    assert phone_events.MAX_BATCH == 200
    assert phone_events.call_event_key("dev", 5) == "dev:call:5"
