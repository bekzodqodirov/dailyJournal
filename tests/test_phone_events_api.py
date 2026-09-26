"""Build step 6, the API and bot-surface half: /v1/phone/* and the missed loop.

The service guarantees live in tests/test_phone_events_core.py; what is worth
pinning here is the envelope and the owner's side of it: a device token opens
exactly the two phone routes, a bad token writes nothing, a batch always comes
back with the contract's accepted/duplicates/rejected shape, the phone's
heartbeat is best-effort, a call-log event can never cost a recording its
audio, and a missed call reads correctly — escaped — in the brief, the report
and under the ✅ Bog'landim / ⏰ buttons.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from miya.api import main
from miya.api.main import app
from miya.bot import formatting, handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource, TransactionType
from miya.services import loops, nudges, phone_events, queries, reports
from tests.test_claims_surface import _claim
from tests.test_close_and_correct import _Callback, _Message
from tests.test_step2_render import NOW, _question, _stale

TZ = settings.tz
TOKEN = "test-token-" + "x" * 53
DEVICE = "b7f1c2e0-0000-4000-8000-000000000002"


def _now() -> datetime:
    return datetime.now(TZ)


def _ago(**kwargs) -> str:
    return (_now() - timedelta(**kwargs)).isoformat()


def _call(call_log_id=1, *, at=None, type="missed", number="+998901234567", **over):
    event = {
        "call_log_id": call_log_id,
        "started_at": at or _ago(hours=2),
        "duration_seconds": 0,
        "type": type,
        "number": number,
        "contact_name": None,
        "sim_slot": 0,
    }
    event.update(over)
    return event


def _sms(sms_id=1, *, at=None, sender="Payme", body="Oplata: 25 000 sum", **over):
    message = {
        "sms_id": sms_id,
        "sender": sender,
        "received_at": at or _ago(hours=1),
        "body": body,
        "sim_slot": 0,
    }
    message.update(over)
    return message


def _calls_payload(*events) -> dict:
    return {"device_id": DEVICE, "events": list(events)}


def _sms_payload(*messages) -> dict:
    return {"device_id": DEVICE, "messages": list(messages)}


@pytest.fixture
def client(monkeypatch):
    """The owner's client — API_BEARER_TOKEN opens everything."""
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


@pytest.fixture
def device(monkeypatch):
    """A companion device — an UPLOAD_TOKENS entry, not the owner's token."""
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    monkeypatch.setattr(settings, "upload_tokens", "phone:device-secret")
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer device-secret"})
        yield c


async def _rows(session) -> list[m.Interaction]:
    return list(
        await session.scalars(sa.select(m.Interaction).order_by(m.Interaction.id))
    )


# --- auth ---------------------------------------------------------------------


async def test_a_device_token_opens_the_phone_routes_and_nothing_else(session, device):
    """An extracted APK can push metadata but read nothing back out. An empty
    batch is the app's connection test, like an empty probe."""
    calls = device.post("/v1/phone/calls", json=_calls_payload())
    sms = device.post("/v1/phone/sms", json=_sms_payload())

    assert calls.status_code == 200
    assert calls.json() == {"accepted": 0, "duplicates": 0, "rejected": []}
    assert sms.status_code == 200
    assert sms.json() == {"accepted": 0, "duplicates": 0, "rejected": []}
    assert device.get("/v1/config").status_code == 401
    assert device.post("/v1/ask", json={"question": "qarz?"}).status_code == 401


async def test_a_bad_token_is_refused_from_the_headers_with_nothing_written(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as anonymous:
        bare = anonymous.post("/v1/phone/calls", json=_calls_payload(_call()))
        wrong = anonymous.post(
            "/v1/phone/sms",
            json=_sms_payload(_sms()),
            headers={"Authorization": "Bearer wrong"},
        )

    assert bare.status_code == 401
    assert wrong.status_code == 401
    assert await _rows(session) == []


async def test_an_unconfigured_server_accepts_no_phone_events(session, monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", "")
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer anything"})
        assert c.post("/v1/phone/calls", json=_calls_payload()).status_code == 503
    assert await _rows(session) == []


# --- the batch envelope -------------------------------------------------------


async def test_a_call_batch_answers_with_the_contract_shape(session, device):
    """accepted / duplicates / rejected-with-reasons — and a re-post is all
    duplicates, so a lost 200 never doubles the call log."""
    batch = _calls_payload(
        _call(1, type="incoming", duration_seconds=65),
        _call(2, type="missed"),
        _call(3, type="ringing"),  # not a CALL_TYPES member
    )
    first = device.post("/v1/phone/calls", json=batch)
    second = device.post("/v1/phone/calls", json=batch)

    assert first.status_code == 200
    body = first.json()
    assert (body["accepted"], body["duplicates"]) == (2, 0)
    assert [r["index"] for r in body["rejected"]] == [2]
    assert body["rejected"][0]["reason"]

    again = second.json()
    assert (again["accepted"], again["duplicates"]) == (0, 2)
    assert [r["index"] for r in again["rejected"]] == [2]
    assert len(await _rows(session)) == 2


async def test_a_bad_timestamp_rejects_only_its_own_event(session, device):
    """A naive clock or garbage text costs one event its slot, with a reason —
    never the 199 good events around it."""
    response = device.post(
        "/v1/phone/calls",
        json=_calls_payload(
            _call(1),
            _call(2, at="2026-09-16T10:00:00"),  # naive: no offset
            _call(3, at="not-a-date"),
        ),
    )

    body = response.json()
    assert body["accepted"] == 1
    assert [r["index"] for r in body["rejected"]] == [1, 2]
    assert all("started_at" in r["reason"] for r in body["rejected"])
    [row] = await _rows(session)
    assert row.media["event_key"] == phone_events.call_event_key(DEVICE, 1)


async def test_batch_and_body_caps_hold(session, device):
    over = _calls_payload(*[_call(i) for i in range(phone_events.MAX_BATCH + 1)])
    assert device.post("/v1/phone/calls", json=over).status_code == 422

    long_body = _sms_payload(_sms(body="x" * 4097))
    assert device.post("/v1/phone/sms", json=long_body).status_code == 422

    # The ASGI guard's 1 MiB cap covers the phone routes like any /v1 JSON.
    huge = device.post(
        "/v1/phone/sms",
        content=b"x" * (1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    assert await _rows(session) == []


# --- SMS through the route ------------------------------------------------------


async def test_a_money_sms_becomes_one_transaction_and_costs_no_tokens(session, device):
    body = "Oplata: 25 000 sum. Karta *1234. KORZINKA TASHKENT. Ostatok: 125 000 sum"
    # The same received_at on both posts: the dedupe key hashes the content,
    # so a retry has to present the very same message, as the phone would.
    at = _ago(hours=1)
    first = device.post("/v1/phone/sms", json=_sms_payload(_sms(at=at, body=body)))
    again = device.post("/v1/phone/sms", json=_sms_payload(_sms(at=at, body=body)))

    assert first.json()["accepted"] == 1
    assert again.json() == {"accepted": 0, "duplicates": 1, "rejected": []}

    [row] = await _rows(session)
    assert row.source is InteractionSource.phone_sms
    assert row.direction is Direction.in_
    assert row.raw_text == body
    assert row.needs_review is False
    assert row.media["payment"]["amount"] == "25000.00"

    [txn] = list(await session.scalars(sa.select(m.Transaction)))
    assert txn.type is TransactionType.expense
    assert txn.amount == Decimal("25000")
    assert txn.category == "oziq-ovqat"
    assert txn.source_interaction_id == row.id
    # The bank's record, not a claim: no extraction, no model call, no cost.
    assert await session.scalar(sa.select(sa.func.count(m.UsageLog.id))) == 0
    assert await session.scalar(sa.select(sa.func.count(m.Claim.id))) == 0


async def test_a_low_confidence_sms_waits_for_the_owners_eye(session, device):
    response = device.post(
        "/v1/phone/sms", json=_sms_payload(_sms(body="Vash parol: 1234"))
    )

    assert response.json()["accepted"] == 1
    [row] = await _rows(session)
    assert row.needs_review is True
    assert list(await session.scalars(sa.select(m.Transaction))) == []
    # /tekshir names the source instead of leaking the enum value.
    assert replies.SOURCE_LABEL["phone_sms"] == "sms"


# --- the phone heartbeat --------------------------------------------------------


async def test_the_heartbeat_rides_an_accepted_batch_only(session, device):
    from miya.services import health

    device.post("/v1/phone/calls", json=_calls_payload(_call(1), _call(2)))
    beats = await health.beats(session)
    assert beats["phone"].detail == {"device_id": DEVICE, "calls": 2}

    # All-duplicate: nothing new arrived, so the ledger is left alone.
    device.post("/v1/phone/calls", json=_calls_payload(_call(1), _call(2)))
    beats = await health.beats(session)
    assert beats["phone"].detail == {"device_id": DEVICE, "calls": 2}

    # An SMS batch replaces the detail whole, as health.beat documents.
    device.post("/v1/phone/sms", json=_sms_payload(_sms()))
    beats = await health.beats(session)
    assert beats["phone"].detail == {"device_id": DEVICE, "sms": 1}


async def test_a_failing_heartbeat_never_costs_the_phone_its_200(
    session, device, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(main.health, "beat", boom)
    response = device.post("/v1/phone/calls", json=_calls_payload(_call(1)))

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert len(await _rows(session)) == 1


# --- the recording hazard, end to end -------------------------------------------


async def test_a_call_log_event_never_makes_the_recording_a_duplicate(
    session, client, tmp_path, monkeypatch
):
    """THE hazard: the phone deletes audio it is told is a duplicate.

    The call-log event for call 4711 lands first (it always does — metadata
    outruns a 40 MB upload). The recording of that same call, wearing the
    call_id the uploader mints for it, must still be 202 accepted: event rows
    carry media["event_key"], never media["call_id"], so neither the upload's
    own dedupe nor the probe can see them.
    """
    directory = tmp_path / "call_recordings"
    directory.mkdir()
    monkeypatch.setattr(settings, "call_recordings_dir", str(directory))

    started = _ago(hours=2)
    assert (
        client.post(
            "/v1/phone/calls",
            json=_calls_payload(_call(4711, at=started, type="outgoing")),
        ).json()["accepted"]
        == 1
    )

    audio = b"RIFFfake-audio-of-call-4711"
    call_id = f"{DEVICE}:4711"
    meta = {
        "schema": 1,
        "device_id": DEVICE,
        "call_id": call_id,
        "sha256": hashlib.sha256(audio).hexdigest(),
        "size_bytes": len(audio),
        "started_at": started,
        "direction": "outgoing",
    }
    upload = client.post(
        "/v1/recordings",
        data={"meta": json.dumps(meta)},
        files={"audio": ("call.m4a", audio, "audio/mp4")},
    )
    assert upload.status_code == 202
    assert upload.json()["status"] == "accepted"

    probe = client.post("/v1/recordings/probe", json={"call_id": [call_id]})
    assert probe.json()["known_call_id"] == []


# --- rendering: missed_line, the timeline ----------------------------------------


def _missed(interaction_id=12, *, person=None, phone="+998901112233", attempts=1):
    return loops.MissedCall(
        kind=loops.KIND_MISSED,
        ref=f"m{interaction_id}",
        age=timedelta(hours=2),
        stake_rank=Decimal(0),
        person=person,
        interaction_id=interaction_id,
        called_at=NOW - timedelta(hours=2),
        phone=phone,
        attempts=attempts,
    )


def test_missed_line_escapes_hostile_names_and_shows_strangers():
    hostile = SimpleNamespace(display_name="<b>Yovuz</b>")
    line = formatting.missed_line(_missed(person=hostile, attempts=2))
    assert "&lt;b&gt;Yovuz&lt;/b&gt;" in line and "<b>Yovuz" not in line
    assert "javobsiz · 2 soat oldin (2 marta)" in line

    stranger = formatting.missed_line(_missed(phone="+99890<x>"))
    assert "+99890&lt;x&gt;" in stranger and "(1 marta)" not in stranger
    assert "noma'lum raqam" in formatting.missed_line(_missed(phone=None))

    # Plain: no tags added and no escaping — the name stays verbatim, because
    # the report escapes the finished line itself (pinned further down).
    plain = formatting.missed_line(_missed(person=hostile), markup=False)
    assert "<b>Yovuz</b> qo'ng'iroq qildi" in plain and "&lt;" not in plain
    assert "javobsiz, 2 soat oldin" in plain


def test_the_timeline_tells_an_sms_apart_from_a_call():
    assert formatting.SOURCE_EMOJI[InteractionSource.phone_sms] == "✉️"
    assert formatting.SOURCE_WORD[InteractionSource.phone_sms] == "sms"
    entry = queries.TimelineEntry(
        interaction=None,
        when=NOW,
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        text="Oplata <b>25 000</b> sum",
    )
    line = formatting.timeline_line(entry)
    assert "✉️ sms" in line and "&lt;b&gt;25 000&lt;/b&gt;" in line
    # queries already lists the source; without the entries above /tarix
    # would degrade to the bare enum value.
    assert InteractionSource.phone_sms in queries.TIMELINE_SOURCES


# --- rendering: the brief ---------------------------------------------------------


def _brief_ns(*, missed=(), questions=(), stale=(), claims_=()):
    ns = SimpleNamespace(
        now=NOW,
        day=date(2026, 9, 15),
        events=[],
        due={},
        loops=loops.OpenLoops(
            now=NOW,
            questions=list(questions),
            missed=list(missed),
            stale=list(stale),
        ),
        claims=list(claims_),
        is_empty=lambda: False,
    )
    return ns


def test_the_brief_slots_missed_between_questions_and_claims():
    body = replies.morning_brief(
        _brief_ns(
            missed=[_missed(attempts=3)],
            questions=[_question()],
            stale=[_stale()],
            claims_=[_claim(12)],
        )
    )
    positions = [
        body.index(replies.BRIEF_QUESTIONS),
        body.index(replies.BRIEF_MISSED),
        body.index(replies.BRIEF_CLAIMS),
        body.index(replies.BRIEF_STALE),
    ]
    assert positions == sorted(positions)
    assert "(3 marta)" in body


def test_a_brief_without_missed_reads_as_before():
    data = _brief_ns(questions=[_question()])
    assert replies.BRIEF_MISSED not in replies.morning_brief(data)
    assert replies.morning_brief_missed_ids(data) == []

    # A brief whose loops predate build step 6 simply has no section.
    legacy = _brief_ns(questions=[_question()])
    legacy.loops = SimpleNamespace(questions=[_question()], stale=[], quiet=[])
    assert replies.BRIEF_MISSED not in replies.morning_brief(legacy)
    assert replies.morning_brief_missed_ids(legacy) == []


def _payloads(markup) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


def test_the_brief_keyboard_rows_their_order_and_their_ids():
    data = _brief_ns(missed=[_missed(12), _missed(15)])
    assert replies.morning_brief_missed_ids(data) == [12, 15]

    markup = keyboards.brief_actions([], [], claim_ids=[3], missed_ids=[12, 15])
    assert _payloads(markup) == [
        ["cl:y:3", "cl:n:3", "cl:e:3"],
        ["rec:ma:m12", "rec:ms:m12"],
        ["rec:ma:m15", "rec:ms:m15"],
    ]
    labels = [b.text for b in markup.inline_keyboard[1]]
    assert labels == ["✅ Bog'landim m12", "⏰ Ertalab eslat m12"]


def test_missed_payloads_fit_and_never_read_as_records():
    markup = keyboards.missed_actions(2_000_000_000)
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert payloads == ["rec:ma:m2000000000", "rec:ms:m2000000000"]
    assert all(len(p.encode()) <= 64 for p in payloads)
    assert keyboards.parse_missed_ref("m12") == 12
    assert keyboards.parse_missed_ref("d12") is None
    assert keyboards.parse_missed_ref("m") is None
    assert formatting.parse_ref("m12") is None  # never a d/p/t record


# --- through the real handler -----------------------------------------------------


@pytest.fixture
def bound(session, monkeypatch):
    """Handlers run inside the test session instead of opening their own."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def _missed_call_row(session, *, contact_name=None, rings=1) -> m.Interaction:
    events = [
        _call(i + 1, at=_ago(hours=3, minutes=-10 * i), contact_name=contact_name)
        for i in range(rings)
    ]
    outcome = await phone_events.ingest_call_events(session, DEVICE, events)
    assert outcome.accepted == rings
    await session.commit()
    rows = await _rows(session)
    return rows[0]


async def test_the_brief_and_the_ma_button_close_the_loop(bound):
    oldest = await _missed_call_row(bound, contact_name="<b>Yovuz</b>", rings=2)

    message = _Message()
    await handlers.cmd_brief(message)
    [(body, markup)] = message.sent
    assert replies.BRIEF_MISSED in body
    assert "&lt;b&gt;Yovuz&lt;/b&gt;" in body and "(2 marta)" in body
    assert [f"rec:ma:m{oldest.id}", f"rec:ms:m{oldest.id}"] in _payloads(markup)

    tapped = _Message(reply_markup=markup)
    await handlers.on_record_button(_Callback(f"rec:ma:m{oldest.id}", tapped))

    assert tapped.sent[-1] == (replies.MISSED_ANSWERED, None)
    assert nudges.is_answered(oldest)
    assert (oldest.meta or {})[nudges.ANSWERED_KEY]["by"] == "button"
    assert await loops.missed_calls(bound) == []
    # Only the tapped row left the keyboard; nothing else was touched.
    assert tapped.edited_markup is None or f"m{oldest.id}" not in str(
        _payloads(tapped.edited_markup)
    )


async def test_the_ms_button_snoozes_the_nudge_until_the_morning(bound):
    oldest = await _missed_call_row(bound)
    assert [item.interaction_id for item in await nudges.collect_missed(bound)] == [
        oldest.id
    ]

    tapped = _Message(reply_markup=keyboards.missed_actions(oldest.id))
    await handlers.on_record_button(_Callback(f"rec:ms:m{oldest.id}", tapped))

    until = nudges.snoozed_until(oldest)
    assert until is not None
    assert tapped.sent[-1] == (replies.nudge_snoozed(until), None)
    assert not nudges.is_answered(oldest)
    # Snoozed: not due a nudge now, but the loop itself stays open.
    assert await nudges.collect_missed(bound) == []
    assert [item.interaction_id for item in await loops.missed_calls(bound)] == [
        oldest.id
    ]


async def test_a_stale_missed_button_says_gone(bound):
    tapped = _Message(reply_markup=keyboards.missed_actions(999_999))
    await handlers.on_record_button(_Callback("rec:ma:m999999", tapped))
    assert tapped.sent[-1] == (replies.MISSED_GONE, None)


# --- the report -------------------------------------------------------------------


async def test_the_report_carries_the_missed_section_with_hostile_names(bound):
    await _missed_call_row(bound, contact_name="<b>Yovuz</b>")

    data = await reports.gather(bound, _now().date())
    block = reports.render_data_block(data)

    assert len(data.missed) == 1
    assert "📵 JAVOBSIZ QO'NG'IROQLAR:" in block
    section = block[block.index("JAVOBSIZ QO'NG'IROQLAR") :]
    assert "&lt;b&gt;Yovuz&lt;/b&gt;" in section.split("🤫")[0]
    assert block.index("JAVOBSIZ QOLGANLAR") < block.index("JAVOBSIZ QO'NG'IROQLAR")
    assert block.index("JAVOBSIZ QO'NG'IROQLAR") < block.index("JIM BO'LIB")
    assert reports._stats_json(data)["missed"] == 1
    prompt = reports.REPORT_SYSTEM_PROMPT
    assert prompt.index("Javobsiz qolganlar") < prompt.index("Javobsiz qo'ng'iroqlar")
    assert prompt.index("Javobsiz qo'ng'iroqlar") < prompt.index("Jim bo'lib")


async def test_a_report_without_missed_calls_has_no_empty_section(session):
    data = await reports.gather(session, _now().date())
    block = reports.render_data_block(data)
    assert "JAVOBSIZ QO'NG'IROQLAR" not in block
    assert reports._stats_json(data)["missed"] == 0
