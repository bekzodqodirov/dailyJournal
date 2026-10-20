"""JV-08: the evening summary at 20:00, and the reports' new lines.

The 09:00 brief names today's deadlines that carry a time of day, soonest
first («⏰ Bugungi muddatlar»), and the evening recap counts the day's
receipts by their bank verdict. Every count is SQL; nothing here calls a
model. Real PostgreSQL.
"""

from __future__ import annotations

import itertools
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from miya.bot import recap_text, replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
    TaskStatus,
)
from miya.services import brief, queries, recaps, receipts
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
