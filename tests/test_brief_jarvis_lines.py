"""JV-08: the evening summary at 20:00, and the reports' new lines; JV-22:
Jarvis's sections in both reports and the Sheets escalation column.

The 09:00 brief names today's deadlines that carry a time of day, soonest
first («⏰ Bugungi muddatlar»), and the evening recap counts the day's
receipts by their bank verdict. With JARVIS_ENABLED both reports tell what
Jarvis did (the brief yesterday, the recap today), and the brief names the
drafts waiting for a tap, the chats nobody answered and every escalation
still open; Client_FollowUps shows a client's open escalation. Every count is
SQL; nothing here calls a model. Real PostgreSQL.
"""

from __future__ import annotations

import itertools
import logging
import sys
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import recap_text, replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
    TaskStatus,
)
from miya.services import brief, queries, recaps, receipts, sheets_sync
from miya.services.people import resolve_person
from tests.recap_helpers import activity, recap_of, render

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]
TODAY = datetime.now(TZ).date()
_ids = itertools.count(1)


def _at(day, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


# --- REPORT_TIME moves to 20:00 -------------------------------------------------


def test_report_default_2000(monkeypatch):
    monkeypatch.delenv("REPORT_TIME", raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.report_time == "20:00"
    assert fresh.report_time_parsed == time(20, 0)
    # Building Settings ran the WP-49 validators: outside the quiet hours
    # (23:30-07:30) and after the brief.
    assert fresh.quiet_hours == "23:30-07:30"
    assert fresh.report_time_parsed > fresh.morning_brief_time_parsed
    # The documented default is the code's.
    assert "\nREPORT_TIME=20:00\n" in (ROOT / ".env.example").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "| `TIMEZONE`, `REPORT_TIME`, `QUIET_HOURS` | Asia/Tashkent, 20:00" in readme
    assert "daily report (20:00 Tashkent)" in readme
    assert "19:00" not in readme
    assert "REPORT_TIME=20:00" in (ROOT / "docs" / "ornatish.md").read_text()


# --- the brief: «⏰ Bugungi muddatlar» --------------------------------------------


async def _promise(session, person, text, *, day=TODAY, at=None, **kw) -> m.Promise:
    promise = m.Promise(
        made_by=kw.pop("made_by", PromiseMadeBy.me),
        person_id=person.id,
        description=text,
        due_date=day,
        due_time=at,
        **kw,
    )
    session.add(promise)
    await session.flush()
    return promise


async def _task(session, text, *, day=TODAY, at=None, **kw) -> m.Task:
    task = m.Task(description=text, due_date=day, due_time=at, **kw)
    session.add(task)
    await session.flush()
    return task


async def test_brief_orders_timed_items(session):
    akmal = await resolve_person(session, "Akmal")
    vali = await resolve_person(session, "Vali")
    late = await _promise(session, akmal, "hisob-kitob", at=time(16, 0))
    early = await _task(session, "bojxonaga qo'ng'iroq", at=time(10, 30))
    middle = await _promise(
        session, vali, "invoys yuboradi", at=time(14, 0), made_by=PromiseMadeBy.them
    )
    dawn = await _promise(session, vali, "nakladnoy", at=time(8, 0))
    undated_today = await _promise(session, akmal, "shartnoma")
    tomorrow = await _promise(
        session, akmal, "ertangi", day=TODAY + timedelta(days=1), at=time(9, 0)
    )
    await _promise(
        session, akmal, "bajarilgan", at=time(11, 0), status=PromiseStatus.done
    )
    await _task(session, "yopilgan", at=time(12, 0), status=TaskStatus.done)

    data = await brief.gather(session, now=_at(TODAY, 9))
    assert [(kind, r.id) for kind, r, _ in data.timed] == [
        ("promise", dawn.id),
        ("task", early.id),
        ("promise", middle.id),
        ("promise", late.id),
    ]

    body = replies.morning_brief(data)
    section = body[body.index(replies.BRIEF_TIMED) :].split("\n\n")[0]
    assert section.splitlines() == [
        "⏰ Bugungi muddatlar:",
        f"08:00 — Vali: nakladnoy <code>p{dawn.id}</code>",
        f"10:30 — bojxonaga qo'ng'iroq <code>t{early.id}</code>",
        f"14:00 — Vali: invoys yuboradi <code>p{middle.id}</code>",
        f"16:00 — Akmal: hisob-kitob <code>p{late.id}</code>",
    ]
    # Said once: the due section keeps the date-only deadline and leaves the
    # timed ones to the section above it; tomorrow's is not today's.
    due = body[body.index(replies.BRIEF_DUE) :].split("\n\n")[0]
    assert f"p{undated_today.id}" in due
    assert all(f"p{p.id}<" not in due for p in (dawn, middle, late))
    assert f"t{early.id}<" not in due
    assert body.index(replies.BRIEF_TIMED) < body.index(replies.BRIEF_DUE)
    assert f"p{tomorrow.id}<" not in section

    # The buttons act on the rows in the order the owner reads them.
    due_refs, _ = replies.morning_brief_refs(data, replies.morning_brief_parts(data))
    assert due_refs[:4] == [
        ("promise", dawn.id),
        ("task", early.id),
        ("promise", middle.id),
        ("promise", late.id),
    ]
    assert due_refs.count(("promise", late.id)) == 1
    assert ("promise", undated_today.id) in due_refs


async def test_timed_line_strips_bidi_from_names_and_text(session):
    person = await resolve_person(session, "Akmal")
    person.display_name = "Akmal\u202e"
    await _promise(session, person, "fayl\u200b.pdf yuboradi", at=time(15, 0))
    data = await brief.gather(session, now=_at(TODAY, 9))
    body = replies.morning_brief(data)
    line = next(ln for ln in body.splitlines() if ln.startswith("15:00"))
    assert "\u202e" not in line and "\u200b" not in line
    assert "fayl.pdf yuboradi" in line


async def test_namesakes_read_apart_across_timed_and_due(session):
    first = m.Person(display_name="Akmal", aliases=[])
    second = m.Person(display_name="Akmal", aliases=[])
    session.add_all([first, second])
    await session.flush()
    timed = await _promise(session, first, "hisob-kitob", at=time(14, 0))
    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=second.id,
        amount=Decimal("500000.00"),
        currency=Currency.UZS,
        due_date=TODAY,
    )
    session.add(debt)
    await session.flush()

    body = replies.morning_brief(await brief.gather(session, now=_at(TODAY, 9)))
    timed_line = next(ln for ln in body.splitlines() if ln.startswith("14:00"))
    assert f"Akmal (#{first.id}): hisob-kitob" in timed_line
    due = body[body.index(replies.BRIEF_DUE) :].split("\n\n")[0]
    assert f"Akmal (#{second.id}):" in due
    assert f"p{timed.id}<" not in due


async def test_timed_query_failure_keeps_the_brief(session, monkeypatch):
    akmal = await resolve_person(session, "Akmal")
    promise = await _promise(session, akmal, "hisob-kitob", at=time(16, 0))

    async def broken(session, day):
        await session.execute(sa.text("SELECT 1 / 0"))

    monkeypatch.setattr(queries, "timed_due_on", broken)
    data = await brief.gather(session, now=_at(TODAY, 9))
    assert data.timed == []
    body = replies.morning_brief(data)
    # The deadline is not lost: it reads in the due section as before.
    assert replies.BRIEF_TIMED not in body
    assert f"p{promise.id}" in body[body.index(replies.BRIEF_DUE) :]
    # The savepoint kept the session usable for the rest of the job.
    assert await session.scalar(sa.select(sa.func.count(m.Promise.id))) == 1


# --- the evening recap: the receipts line ------------------------------------------


async def _receipt(session, verdict: str, *, at: datetime, **kw) -> m.Receipt:
    row = m.Interaction(
        source=kw.pop("source", InteractionSource.telegram_userbot),
        direction=Direction.in_,
        occurred_at=at,
        raw_text="",
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    receipt = m.Receipt(
        interaction_id=row.id,
        direction=kw.pop("direction", receipts.IN),
        amount=Decimal("1250000.00"),
        currency=Currency.UZS,
        state=kw.pop("state", receipts.STATE_READ),
        verdict=verdict,
        created_at=at,
        **kw,
    )
    session.add(receipt)
    await session.flush()
    return receipt


async def test_recap_receipt_counts_from_sql(session):
    ten = _at(TODAY, 10)
    for verdict in (
        receipts.VERDICT_CONFIRMED,
        receipts.VERDICT_CONFIRMED,
        receipts.VERDICT_OWNER_OK,
        receipts.VERDICT_WAITING,
        receipts.VERDICT_UNCONFIRMED,
        receipts.VERDICT_SUSPICIOUS,
        receipts.VERDICT_OWNER_FAKE,
    ):
        await _receipt(session, verdict, at=ten)
    # Not today's, not read, and the owner's own payment: none of them count.
    await _receipt(session, receipts.VERDICT_CONFIRMED, at=ten - timedelta(days=1))
    await _receipt(
        session, receipts.VERDICT_WAITING, at=ten, state=receipts.STATE_NEEDS_REVIEW
    )
    await _receipt(session, receipts.VERDICT_UNCONFIRMED, at=ten, direction=receipts.OUT)

    start, end = queries.day_bounds(TODAY)
    day = await recaps.receipt_day(session, start, end)
    assert (day.total, day.confirmed, day.waiting, day.suspicious) == (7, 3, 2, 2)

    text = await recap_of(session, TODAY, now=_at(TODAY, 20))
    line = (
        "🧾 Bugun 7 ta chek: 3 tasi bankda tasdiqlandi, 2 tasi kutilmoqda, "
        "2 tasi shubhali."
    )
    assert line in text
    # Under the money, before the new records.
    assert text.index(recap_text.RECAP_MONEY) < text.index(line)


async def test_forwarded_receipt_counts_once(session):
    ten = _at(TODAY, 10)
    same = {"image_sha256": "a" * 64, "reference": "TX123456", "issuer": "payme"}
    await _receipt(session, receipts.VERDICT_CONFIRMED, at=ten, **same)
    # The owner forwards the client's receipt to the bot: the same receipt.
    await _receipt(
        session,
        receipts.VERDICT_CONFIRMED,
        at=ten + timedelta(minutes=5),
        source=InteractionSource.receipt_photo,
        **same,
    )
    # Same printed reference and issuer, a different image: still that receipt.
    await _receipt(
        session,
        receipts.VERDICT_CONFIRMED,
        at=ten + timedelta(minutes=6),
        source=InteractionSource.assistant_bot,
        image_sha256="b" * 64,
        reference="TX123456",
        issuer="payme",
    )
    # A receipt sent only to the bot is the owner's own read: it counts.
    await _receipt(
        session,
        receipts.VERDICT_WAITING,
        at=ten,
        source=InteractionSource.receipt_photo,
        image_sha256="c" * 64,
    )
    start, end = queries.day_bounds(TODAY)
    day = await recaps.receipt_day(session, start, end)
    assert (day.total, day.confirmed, day.waiting, day.suspicious) == (2, 1, 1, 0)


async def test_recap_without_receipts_has_no_line(session):
    text = await recap_of(session, TODAY, now=_at(TODAY, 20))
    assert "ta chek:" not in text


def test_receipts_line_alone_is_not_an_empty_day():
    day = recaps.ReceiptDay(total=1, waiting=1)
    text = render(activity(TODAY), receipts=day)
    assert recap_text.EVENING_EMPTY not in text
    assert recap_text.receipts_line(day) in text
    assert recap_text.receipts_line(None) is None
    assert recap_text.receipts_line(recaps.ReceiptDay()) is None


async def test_receipt_count_failure_keeps_the_recap(session, monkeypatch):
    await _receipt(session, receipts.VERDICT_CONFIRMED, at=_at(TODAY, 10))
    # Fails in PostgreSQL itself, so the transaction would be aborted without
    # the savepoint.
    monkeypatch.setattr(receipts, "OUT", sa.literal_column("(1/0)::text"))
    start, end = queries.day_bounds(TODAY)
    assert await recaps.receipt_day(session, start, end) is None
    # The savepoint kept the session usable for the report job's next writes.
    assert await session.scalar(sa.select(sa.func.count(m.Receipt.id))) == 1
    monkeypatch.undo()
    monkeypatch.setattr(recaps, "receipt_day", _no_receipts)
    text = await recap_of(session, TODAY, now=_at(TODAY, 20))
    assert recap_text.EVENING_HEADER.split(" ·")[0] in text
    assert "ta chek:" not in text


async def _no_receipts(session, start, end):
    return None


# --- JV-22: Jarvis in the brief and the recap -----------------------------------------

YESTERDAY = TODAY - timedelta(days=1)
_keys = itertools.count(1)


@pytest.fixture
def jarvis_on(monkeypatch):
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_daily_cost_cap_usd", Decimal("3.00"))
    monkeypatch.setattr(settings, "jarvis_snooze_max", 3)


def _history(at: datetime, *states: str, code: str | None = None) -> list[dict]:
    entries = []
    old = None
    for i, state in enumerate(states):
        entry = {"at": at.isoformat(), "field": "state", "old": old, "new": state}
        entry["by"] = "gate"
        if i == 0 and code:
            entry["code"] = code
        entries.append(entry)
        old = state
    return entries


def _out(
    *,
    at: datetime,
    states: tuple[str, ...],
    origin: str = "auto_reply",
    authored_by: str = "ai",
    intent: str | None = "greeting",
    chat: int | None = None,
    person: m.Person | None = None,
    flags: tuple[str, ...] = (),
    code: str | None = None,
    **fields,
) -> m.OutboxMessage:
    """An outbox row as the chokepoint leaves it: its state, and a history
    that went through ``states``."""
    chat = chat if chat is not None else 700000 + next(_keys)
    body = "Assalomu alaykum"
    if authored_by == "ai":
        # outbox.create keeps an AI row's own words from the start.
        fields.setdefault("ai_body", body)
    return m.OutboxMessage(
        channel="telegram",
        chat_key=f"tg:{chat}",
        tg_chat_id=chat,
        person_id=person.id if person else None,
        origin=origin,
        intent=intent,
        authored_by=authored_by,
        body=body,
        body_sha256="0" * 64,
        # As the chokepoint binds it: an autonomous row always carries a
        # passing shape check (0028, ck_outbox_messages_auto_shapes).
        guard={
            "verdict": "pass",
            "flags": list(flags),
            "shapes_ok": fields.get("approved_by") == "auto:policy",
        },
        state=states[-1],
        idempotency_key=f"jv22-{next(_keys)}",
        history=_history(at, *states, code=code),
        created_at=at,
        **fields,
    )


async def _client_message(session, person=None) -> m.Interaction:
    # Long before both reports' windows, so neither tells it as the day's talk.
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id if person else None,
        occurred_at=_at(TODAY - timedelta(days=5), 10),
        raw_text="",
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    return row


async def _escalation(
    session,
    *,
    at: datetime,
    mode: str = "draft",
    state: str = "open",
    person: m.Person | None = None,
    chat: int | None = None,
    reasons: tuple[str, ...] = ("price",),
    **fields,
) -> m.Escalation:
    chat = chat if chat is not None else 800000 + next(_keys)
    interaction = await _client_message(session, person)
    escalation = m.Escalation(
        interaction_id=interaction.id,
        person_id=person.id if person else None,
        channel="telegram",
        chat_key=f"tg:{chat}",
        reasons=list(reasons),
        state=state,
        history=[
            {
                "at": at.isoformat(),
                "field": "state",
                "old": None,
                "new": "open",
                "by": "responder",
                "mode": mode,
            }
        ],
        created_at=at,
        **fields,
    )
    session.add(escalation)
    await session.flush()
    return escalation


def _section(body: str, first: str) -> list[str]:
    return body[body.index(first) :].split("\n\n")[0].splitlines()


async def _seed_activity(session) -> None:
    """Yesterday's Jarvis day, and a little of today's that the brief must
    leave to the evening."""
    y = _at(YESTERDAY, 15)
    lease = timedelta(minutes=5)
    session.add_all(
        [
            # Sent on its own (a graduated intent): never a draft.
            _out(
                at=y,
                states=("approved", "sending", "sent"),
                approved_by="auto:policy",
                sent_at=y,
            ),
            # A draft the owner sent unchanged.
            _out(
                at=y,
                states=("draft", "approved", "sending", "sent"),
                approved_by="button",
                sent_at=y,
            ),
            # The owner's own reply to an escalation: a reply they tapped.
            _out(
                at=y,
                states=("approved", "sending", "sent"),
                origin="escalation",
                authored_by="owner",
                intent=None,
                approved_by="button",
                sent_at=y,
            ),
            # A send the owner commanded, and one in doubt.
            _out(
                at=y,
                states=("draft", "approved", "sending", "sent"),
                origin="owner_command",
                authored_by="owner",
                intent=None,
                approved_by="button",
                sent_at=y,
            ),
            _out(
                at=y,
                states=("draft", "approved", "sending", "unknown"),
                origin="owner_command",
                authored_by="owner",
                intent=None,
                approved_by="button",
                lease_until=y + lease,
            ),
            # Drafts: one the owner rewrote, one that strayed from the shapes,
            # and an observation draft that strayed too.
            _out(
                at=y,
                states=("draft",),
                authored_by="owner",
                intent="info_gathering",
                ai_body="Ma'lumot beraman",
            ),
            _out(
                at=y,
                states=("draft", "expired"),
                intent="general_question",
                flags=("unapproved_statement",),
            ),
            _out(at=y, states=("shadow",), flags=("unapproved_statement",)),
            # Sent before midnight the day before: not yesterday's.
            _out(
                at=y - timedelta(days=1),
                states=("approved", "sending", "sent"),
                approved_by="auto:policy",
                sent_at=y - timedelta(days=1),
            ),
            # Today's: the evening's, not the brief's.
            _out(
                at=_at(TODAY, 10),
                states=("approved", "sending", "sent"),
                approved_by="auto:policy",
                sent_at=_at(TODAY, 10),
            ),
            m.UsageLog(
                provider="anthropic",
                operation="jarvis_draft",
                cost_usd=Decimal("0.25"),
                created_at=y,
            ),
            m.UsageLog(
                provider="anthropic",
                operation="jarvis_intent",
                cost_usd=Decimal("0.05"),
                created_at=y,
            ),
            # Core MIYA's own spend never counts as Jarvis's.
            m.UsageLog(
                provider="anthropic",
                operation="extract",
                cost_usd=Decimal("9"),
                created_at=y,
            ),
            m.UsageLog(
                provider="anthropic",
                operation="jarvis_draft",
                cost_usd=Decimal("1.00"),
                created_at=_at(TODAY, 10),
            ),
        ]
    )
    await session.flush()
    # Shown escalations: one still open, one answered; an observation one.
    await _escalation(session, at=y, state="open")
    await _escalation(session, at=y, mode="auto", state="resolved", resolution="sent")
    await _escalation(session, at=y, mode="shadow")
    await _escalation(session, at=_at(TODAY, 10), state="open")


async def test_counts_from_sql(session, jarvis_on):
    await _seed_activity(session)
    start, end = queries.day_bounds(YESTERDAY)
    from miya.jarvis import status

    day = await status.day_report(session, start, end)
    assert (day.sent, day.auto, day.approved) == (3, 1, 2)
    assert (day.escalations, day.escalations_open, day.shadow_escalations) == (2, 1, 1)
    assert (day.commands, day.unknown) == (1, 1)
    assert day.cost_usd == Decimal("0.30") and day.cost_unpriced == 0
    assert [
        (q.intent, q.drafts, q.unchanged, q.edited, q.unapproved) for q in day.quality
    ] == [
        ("greeting", 2, 1, 0, 1),
        ("general_question", 1, 0, 0, 1),
        ("info_gathering", 1, 0, 1, 0),
    ]

    data = await brief.gather(session, now=_at(TODAY, 9))
    body = replies.morning_brief(data)
    assert _section(body, "🤖 Jarvis (kecha)") == [
        "🤖 Jarvis (kecha): 3 ta javob yuborildi (1 tasi avtomatik, 2 tasi siz "
        "tasdiqlagan), 2 ta eskalatsiya (1 tasi ochiq)",
        "📤 Buyrug'ingiz bilan yuborildi: 1 ta",
        "❓ 1 ta xabar yuborilganmi — aniq emas (/holat)",
        "🧪 Kuzatuv rejimida 1 ta xabar sizga yo'naltirilardi",
        "💸 Jarvis xarajati: $0.30 / $3.00",
        "🧪 Qoralamalar sifati:",
        "• salomlashish: 2 ta qoralama — 1 tasi o'zgartirilmasdan tasdiqlandi, "
        "1 tasi erkin gapli (o'zi yuborilmasdi)",
        "• umumiy savol: 1 ta qoralama — 1 tasi erkin gapli (o'zi yuborilmasdi)",
        "• ma'lumot so'rash: 1 ta qoralama — 1 tasi tahrirlandi",
        # What the cards leave to the brief follows in the same section.
        "✍️ Kutayotgan qoralamalar: 1 — /qoralamalar",
        # The rewritten draft still waits; the strayed one ran out without
        # the expiry's code (the owner's own cancel or a gate hold, say).
        "📨 Javobsiz chatlar: 1 — Mijoz",
        f"⚠️ Ochiq eskalatsiyalar: Mijoz (narx so'rayapti, {_shown_when(YESTERDAY)}); "
        f"Mijoz (narx so'rayapti, {_shown_when(TODAY)})",
    ]

    # The evening tells today's day, under its own headline.
    text = await recap_of(session, TODAY, now=_at(TODAY, 20))
    assert _section(text, "🤖 Bugun Jarvis") == [
        "🤖 Bugun Jarvis: 1 ta javob yuborildi (1 tasi avtomatik, 0 tasi siz "
        "tasdiqlagan), 1 ta eskalatsiya (1 tasi ochiq)",
        "💸 Jarvis xarajati: $1.00 / $3.00",
    ]
    assert text.index(recap_text.RECAP_MONEY) < text.index("🤖 Bugun Jarvis")


def _shown_when(day) -> str:
    at = _at(day, 15 if day == YESTERDAY else 10)
    return replies._card_when(at)


async def test_snoozed_over_max_in_brief(session, jarvis_on):
    akmal = await resolve_person(session, "Akmal")
    akmal.display_name = "Akmal\u202e"
    at = _at(YESTERDAY, 14, 5)
    await _escalation(
        session,
        at=at,
        state="snoozed",
        person=akmal,
        reasons=("price", "deadline"),
        snooze_count=3,
        snoozed_until=at + timedelta(hours=3),
    )
    # An observation escalation is never shown, a closed one is not open, and
    # one without a person reads as its chat's title.
    await _escalation(session, at=at, mode="shadow", person=akmal)
    await _escalation(session, at=at, state="expired", resolution="expired")
    session.add(
        m.ChatMonitor(
            tg_chat_id=-100555, chat_type=ChatType.group, title="Yuk\u200b guruhi"
        )
    )
    await session.flush()
    await _escalation(session, at=at + timedelta(minutes=1), chat=-100555)

    data = await brief.gather(session, now=_at(TODAY, 9))
    assert not data.is_empty()
    body = replies.morning_brief(data)
    line = next(ln for ln in body.splitlines() if ln.startswith("⚠️ Ochiq eskalatsiyalar"))
    when = replies._card_when(at)
    later = replies._card_when(at + timedelta(minutes=1))
    assert line == (
        f"⚠️ Ochiq eskalatsiyalar: Akmal (narx so'rayapti, muddat so'rayapti, {when}); "
        f"Yuk guruhi (narx so'rayapti, {later})"
    )


async def test_brief_names_waiting_drafts_and_unanswered_chats(session, jarvis_on):
    vali = await resolve_person(session, "Vali")
    sardor = await resolve_person(session, "Sardor")
    karim = await resolve_person(session, "Karim")
    night = _at(YESTERDAY, 22)
    session.add_all(
        [
            # Shown on a card at night, still waiting.
            _out(at=night, states=("draft",), person=vali, card_tg_message_id=91),
            # Expired unanswered overnight.
            _out(
                at=night,
                states=("draft", "expired"),
                person=sardor,
                last_error="expired",
            ),
            # Waiting, but the owner wrote in that chat since.
            _out(at=night, states=("draft",), person=karim, chat=4242),
            m.JarvisChatState(
                chat_key="tg:4242",
                channel="telegram",
                owner_replied_at=night + timedelta(hours=1),
            ),
            # Run out: no longer waiting, and the owner cancelled another.
            _out(
                at=night,
                states=("draft",),
                expires_at=_at(TODAY, 8),
                chat=4343,
            ),
            _out(at=night, states=("draft", "cancelled"), chat=4444),
            # Two days old: not the last day's.
            _out(at=night - timedelta(days=1), states=("draft", "expired"), chat=4545),
        ]
    )
    await session.flush()
    # The chat with an open escalation is named once, among the escalations.
    await _out_in_escalated_chat(session, night)

    items_body = replies.morning_brief(await brief.gather(session, now=_at(TODAY, 9)))
    # Vali's and Karim's drafts still wait for a tap; the one that ran out
    # does not, though nobody answered its chat either.
    assert "✍️ Kutayotgan qoralamalar: 2 — /qoralamalar" in items_body
    unanswered = next(
        ln for ln in items_body.splitlines() if ln.startswith("📨 Javobsiz chatlar")
    )
    assert unanswered == "📨 Javobsiz chatlar: 3 — Vali, Sardor, Mijoz"
    assert "Bobur (narx so'rayapti" in items_body


async def test_a_later_sent_draft_answers_the_older_one(session, jarvis_on):
    from miya.jarvis import cards

    aziz = await resolve_person(session, "Aziz")
    olim = await resolve_person(session, "Olim")
    night = _at(YESTERDAY, 22)
    session.add_all(
        [
            # The client wrote twice; the owner sent the second draft, and
            # the first ran out on its own.
            _out(
                at=night,
                states=("draft", "expired"),
                person=aziz,
                chat=5151,
                last_error="expired",
            ),
            _out(
                at=night + timedelta(minutes=10),
                states=("draft", "approved", "sending", "sent"),
                person=aziz,
                chat=5151,
                approved_by="button",
                sent_at=night + timedelta(minutes=15),
            ),
            # A reply sent before the draft was made answers nothing of it.
            _out(
                at=night - timedelta(hours=2),
                states=("approved", "sending", "sent"),
                person=olim,
                chat=5252,
                approved_by="auto:policy",
                sent_at=night - timedelta(hours=2),
            ),
            _out(
                at=night,
                states=("draft", "expired"),
                person=olim,
                chat=5252,
                last_error="expired",
            ),
        ]
    )
    await session.flush()

    items = await cards.brief_items(session, now=_at(TODAY, 9))
    assert items.unanswered == ("Olim",)


async def _out_in_escalated_chat(session, at: datetime) -> None:
    bobur = await resolve_person(session, "Bobur")
    session.add(_out(at=at, states=("draft", "expired"), person=bobur, chat=4646))
    await session.flush()
    await _escalation(session, at=at, person=bobur, chat=4646)


def test_brief_jarvis_lines_render_plainly():
    from miya.jarvis import cards, status

    quiet = status.DayReport(sent=2, auto=2, cost_usd=Decimal("0.004"))
    names = tuple(f"Mijoz{i}" for i in range(12))
    items = cards.BriefItems(unanswered=(*names[:-1], "<b>Ali\u202e"))
    lines = replies.jarvis_brief_lines(items, quiet)
    assert lines[0] == (
        "🤖 Jarvis (kecha): 2 ta javob yuborildi (2 tasi avtomatik, 0 tasi siz "
        "tasdiqlagan), 0 ta eskalatsiya (0 tasi ochiq)"
    )
    assert lines[1] == "💸 Jarvis xarajati: $0.0040 / $0.00"
    # At most ten names; the rest are counted.
    assert lines[2] == (
        "📨 Javobsiz chatlar: 12 — " + ", ".join(names[:10]) + ", … va yana 2 ta"
    )
    # A name the client chose is escaped and stripped of bidi marks.
    tail = replies.jarvis_brief_lines(cards.BriefItems(unanswered=("<b>Ali\u202e",)))
    assert tail == ["📨 Javobsiz chatlar: 1 — &lt;b&gt;Ali"]
    # A quiet day says nothing; Jarvis off (None) says nothing.
    assert replies.jarvis_brief_lines(None, status.DayReport()) == []
    assert replies.jarvis_brief_lines(None, None) == []
    unpriced = status.DayReport(cost_unpriced=2, cost_cap_usd=Decimal("3.00"))
    assert replies.jarvis_brief_lines(None, unpriced)[1] == (
        "💸 Jarvis xarajati: $0.00 / $3.00 · narxi noma'lum: 2 ta chaqiruv"
    )


def test_jarvis_activity_alone_keeps_the_all_clear_brief():
    from miya.jarvis import status

    data = brief.MorningBrief(
        now=_at(TODAY, 9), jarvis_day=status.DayReport(sent=1, auto=1)
    )
    assert data.is_empty()
    body = replies.morning_brief(data)
    assert replies.BRIEF_ALL_CLEAR in body
    assert "🤖 Jarvis (kecha): 1 ta javob yuborildi" in body


def test_recap_jarvis_section_alone_is_not_an_empty_day():
    from miya.jarvis import status

    report = status.DayReport(escalations=1, escalations_open=1)
    text = render(activity(TODAY), jarvis=report)
    assert recap_text.EVENING_EMPTY not in text
    assert recap_text.jarvis_section(report) in text
    assert recap_text.jarvis_section(None) is None
    assert recap_text.jarvis_section(status.DayReport()) is None

    # Its own section after tomorrow's plan, before the footer.
    event = SimpleNamespace(
        start_at=_at(TODAY + timedelta(days=1), 10), title="Uchrashuv", location=None
    )
    full = render(
        activity(TODAY),
        tomorrow=recaps.Tomorrow(events=[event]),
        system=["🩺 test"],
        jarvis=report,
    )
    assert (
        full.index(recap_text.RECAP_TOMORROW)
        < full.index("🤖 Bugun Jarvis")
        < full.index("🩺 test")
    )


async def test_jarvis_off_reports_unchanged(session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    now = _at(TODAY, 9)
    evening = _at(TODAY, 20)

    async def reports() -> tuple[str, str]:
        morning = replies.morning_brief(await brief.gather(session, now=now))
        return morning, await recap_of(session, TODAY, now=evening)

    before = await reports()
    await _seed_activity(session)
    # Rule 20: with Jarvis off nothing may import it.
    monkeypatch.setitem(sys.modules, "miya.jarvis", None)
    with caplog.at_level(logging.WARNING):
        data = await brief.gather(session, now=now)
        after = (replies.morning_brief(data), await recap_of(session, TODAY, now=evening))
        assert data.jarvis is None and data.jarvis_day is None
        assert await recaps.jarvis_day(session, *queries.day_bounds(TODAY)) is None
    assert after == before
    assert not [r for r in caplog.records if "Jarvis" in r.getMessage()]


async def test_a_failed_jarvis_read_costs_only_its_section(session, jarvis_on, caplog):
    from miya.jarvis import cards, status

    await _seed_activity(session)

    async def broken_report(session, start, end):
        await session.execute(sa.text("SELECT 1 / 0"))

    async def broken_items(session, *, now):
        await session.execute(sa.text("SELECT 1 / 0"))

    original = (status.day_report, cards.brief_items)
    status.day_report, cards.brief_items = broken_report, broken_items
    try:
        with caplog.at_level(logging.WARNING):
            data = await brief.gather(session, now=_at(TODAY, 9))
            text = await recap_of(session, TODAY, now=_at(TODAY, 20))
    finally:
        status.day_report, cards.brief_items = original
    assert data.jarvis is None and data.jarvis_day is None
    body = replies.morning_brief(data)
    assert body.startswith(replies.BRIEF_HEADER)
    assert "🤖 Jarvis" not in body and "🤖 Bugun Jarvis" not in text
    assert recap_text.EVENING_HEADER.split(" ·")[0] in text
    failures = [r for r in caplog.records if "Jarvis" in r.getMessage()]
    assert {getattr(r, "error", None) for r in failures} == {"DataError"}
    assert len(failures) == 3
    # The savepoints kept the session usable for the jobs' next writes.
    assert await session.scalar(sa.select(sa.func.count(m.Escalation.id))) == 4


async def test_qoralamalar_lists_waiting_drafts(session, jarvis_on):
    from miya.jarvis import status

    vali = await resolve_person(session, "Vali\u202e")
    now = datetime.now(TZ)
    waiting = _out(at=now - timedelta(hours=1), states=("draft",), person=vali)
    session.add_all(
        [
            waiting,
            _out(at=now - timedelta(hours=1), states=("draft", "expired")),
            _out(
                at=now - timedelta(hours=1),
                states=("draft",),
                expires_at=now - timedelta(minutes=1),
            ),
            _out(at=now - timedelta(hours=1), states=("shadow",)),
        ]
    )
    await session.flush()
    found = await status.waiting_drafts(session, now=now)
    assert [d.id for d in found] == [waiting.id]
    shadow = await status.shadow_drafts(session)
    text = replies.shadow_drafts(shadow, found)
    lines = text.splitlines()
    assert lines[0] == "✍️ Kutayotgan qoralamalar:"
    assert lines[1] == (
        f"1. j{waiting.id} · Vali: «» → «Assalomu alaykum» [salomlashish]"
    )
    assert lines[3] == "🧪 Kuzatuv qoralamalari:"
    # Without waiting drafts the observation list reads as before.
    assert replies.shadow_drafts(shadow).splitlines()[0] == "🧪 Kuzatuv qoralamalari:"
    assert replies.shadow_drafts([], []) == replies.SHADOW_DRAFTS_EMPTY


# --- JV-22: the Sheets carry-over ---------------------------------------------------


async def test_sheets_followups_escalation_column(session, jarvis_on, monkeypatch):
    from tests.test_sheets_sync import FakeSheetsAPI

    monkeypatch.setattr(settings, "sheets_enabled", True)
    akmal = await resolve_person(session, "Akmal")
    vali = await resolve_person(session, "Vali")
    since = datetime(2026, 10, 20, 14, 5, tzinfo=TZ)
    await _escalation(session, at=since, person=akmal, reasons=("price", "deadline"))
    # A later one in another chat: the oldest is the one shown.
    await _escalation(session, at=since + timedelta(hours=2), person=akmal)
    # Observation mode only counted it: not the owner's to follow up.
    await _escalation(session, at=since, person=vali, mode="shadow")
    await session.commit()

    api = FakeSheetsAPI()
    await sheets_sync.sync(session, api, now=datetime.now(TZ))
    rows = api.by_id("Client_FollowUps")
    assert rows[f"k{akmal.id}"][6] == (
        "narx so'rayapti, muddat so'rayapti · 2026-10-20 14:05 dan beri"
    )
    assert f"k{vali.id}" not in rows

    # Once nothing keeps Akmal open, the row is cleared.
    for row in await session.scalars(sa.select(m.Escalation)):
        row.state, row.resolution = "resolved", "sent"
    await session.commit()
    await sheets_sync.sync(session, api, now=datetime.now(TZ))
    assert f"k{akmal.id}" not in api.by_id("Client_FollowUps")

    # With Jarvis off the column stays blank and miya.jarvis is not imported.
    await _escalation(session, at=since, person=akmal)
    session.add(m.ClientCode(code="GS901", person_id=akmal.id, source="command"))
    await session.commit()
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    monkeypatch.setitem(sys.modules, "miya.jarvis", None)
    await sheets_sync.sync(session, api, now=datetime.now(TZ))
    assert api.by_id("Client_FollowUps")[f"k{akmal.id}"][6] == ""


async def test_sheets_followups_survive_a_failed_escalation_read(
    session, jarvis_on, monkeypatch, caplog
):
    from miya.jarvis import cards
    from tests.test_sheets_sync import FakeSheetsAPI

    monkeypatch.setattr(settings, "sheets_enabled", True)
    akmal = await resolve_person(session, "Akmal")
    await _escalation(session, at=datetime(2026, 10, 20, 14, 5, tzinfo=TZ), person=akmal)
    session.add(m.ClientCode(code="GS902", person_id=akmal.id, source="command"))
    await session.commit()

    def broken():
        raise RuntimeError("escalations unreadable")

    monkeypatch.setattr(cards, "shown_escalation", broken)
    api = FakeSheetsAPI()
    with caplog.at_level(logging.WARNING, logger="miya.services.sheets_sync"):
        await sheets_sync.sync(session, api, now=datetime.now(TZ))
    # The tab is still built from MIYA's own rows; only the column is blank.
    row = api.by_id("Client_FollowUps")[f"k{akmal.id}"]
    assert row[2] == "GS902" and row[6] == ""
    failed = [r for r in caplog.records if "Jarvis escalations failed" in r.getMessage()]
    assert failed and failed[0].error == "RuntimeError"
