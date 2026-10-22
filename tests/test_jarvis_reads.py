"""JV-06: «Bugun kimga nima va'da bergandim?» and «Kecha kimlar yozdi?» are
answered from SQL, typed or spoken, with no model call.

The day is a local Asia/Tashkent day; only the owner's own words make a
promise or answer someone (JV-10), never MIYA's automatic replies.
"""

from __future__ import annotations

import itertools
import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import sqlalchemy as sa

from miya.bot import formatting, handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
)
from miya.services import queries, rag, retry
from tests.test_close_and_correct import _Message, bound  # noqa: F401
from tests.test_rag import _StubClient, _text_response, _usage

TZ = settings.tz
DAY = date(2026, 10, 20)  # a Tuesday
DM_AKMAL, DM_VALI, GROUP, QUIET_GROUP = 7601, 7602, -7603, -7604
_ids = itertools.count(1)

AI_TAG = {"sent_by": "miya", "authored_by": "ai", "outbox_id": 41}
OWNER_TAG = {"sent_by": "miya", "authored_by": "owner", "outbox_id": 42}


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=TZ)


async def _person(session, name: str) -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _monitors(session) -> None:
    session.add_all(
        [
            m.ChatMonitor(tg_chat_id=DM_AKMAL, chat_type=ChatType.private, title="Akmal"),
            m.ChatMonitor(tg_chat_id=DM_VALI, chat_type=ChatType.private, title="Vali"),
            m.ChatMonitor(tg_chat_id=GROUP, chat_type=ChatType.group, title="Yuk guruhi"),
            m.ChatMonitor(
                tg_chat_id=QUIET_GROUP, chat_type=ChatType.group, title="Boshqa guruh"
            ),
        ]
    )
    await session.flush()


async def _message(
    session,
    *,
    chat: int,
    at: datetime,
    person: m.Person | None = None,
    out: bool = False,
    text: str = "salom",
    meta: dict | None = None,
    source: InteractionSource = InteractionSource.telegram_userbot,
) -> m.Interaction:
    row = m.Interaction(
        source=source,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        meta={"tg_message_id": next(_ids), **(meta or {})},
    )
    session.add(row)
    await session.flush()
    return row


async def _promise(
    session,
    person: m.Person,
    text: str,
    *,
    source: m.Interaction | None = None,
    made_by: PromiseMadeBy = PromiseMadeBy.me,
    status: PromiseStatus = PromiseStatus.open,
    due: date | None = None,
    due_time: time | None = None,
    created_at: datetime | None = None,
) -> m.Promise:
    promise = m.Promise(
        made_by=made_by,
        person_id=person.id,
        description=text,
        status=status,
        due_date=due,
        due_time=due_time,
        source_interaction_id=source.id if source else None,
    )
    if created_at is not None:
        promise.created_at = created_at
    session.add(promise)
    await session.flush()
    return promise


# --- promises made in a local day ---------------------------------------------------


async def test_promises_made_day_window_tashkent(session):
    await _monitors(session)
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")

    # 23:59 the evening before is yesterday's, though it is 18:59 UTC.
    late = await _message(
        session,
        chat=DM_AKMAL,
        person=akmal,
        at=_at(DAY, 0) - timedelta(minutes=1),
        out=True,
    )
    await _promise(session, akmal, "kechagi va'da", source=late)
    # 00:00 local is 19:00 UTC the day before, and it is today.
    first = await _message(session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 0), out=True)
    await _promise(
        session,
        akmal,
        "konteynerni jo'nataman",
        source=first,
        due=DAY,
        due_time=time(18, 0),
    )
    in_group = await _message(
        session, chat=GROUP, person=vali, at=_at(DAY, 9, 30), out=True
    )
    await _promise(session, vali, "hujjatni <b>yuboraman</b>", source=in_group)
    # Typed into the bot: no chat, a note.
    note = await _message(
        session,
        chat=None,
        at=_at(DAY, 11),
        source=InteractionSource.assistant_bot,
    )
    await _promise(
        session, vali, "pulni qaytaraman", source=note, due=DAY + timedelta(days=3)
    )
    # Already kept: still made today.
    await _promise(
        session,
        akmal,
        "qo'ng'iroq qilaman",
        source=await _message(
            session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 12), out=True
        ),
        status=PromiseStatus.done,
    )
    # No source row at all: counted by when it was written.
    await _promise(session, vali, "kelaman", created_at=_at(DAY, 23, 59, 59))
    # Left out: their promise, a cancelled one, MIYA's own stall, tomorrow's.
    theirs = await _message(session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 13))
    await _promise(session, akmal, "to'layman", source=theirs, made_by=PromiseMadeBy.them)
    await _promise(
        session,
        akmal,
        "bekor qilingan",
        source=await _message(
            session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 14), out=True
        ),
        status=PromiseStatus.cancelled,
    )
    stall = await _message(
        session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 15), out=True, meta=AI_TAG
    )
    await _promise(session, akmal, "aniqlashtirib xabar beraman", source=stall)
    # MIYA sent the owner's own approved words: still the owner's promise.
    approved = await _message(
        session, chat=DM_VALI, person=vali, at=_at(DAY, 16), out=True, meta=OWNER_TAG
    )
    await _promise(session, vali, "ertaga uchrashamiz", source=approved)
    tomorrow = await _message(
        session, chat=DM_AKMAL, person=akmal, at=_at(DAY + timedelta(days=1), 0), out=True
    )
    await _promise(session, akmal, "ertangi", source=tomorrow)

    made, total = await queries.promises_made(session, queries.day_bounds(DAY))

    assert total == 6
    assert [item.promise.description for item in made] == [
        "konteynerni jo'nataman",
        "hujjatni <b>yuboraman</b>",
        "pulni qaytaraman",
        "qo'ng'iroq qilaman",
        "ertaga uchrashamiz",
        "kelaman",
    ]
    assert made[0].made_at == _at(DAY, 0)
    theirs_only, _ = await queries.promises_made(
        session, queries.day_bounds(DAY), made_by=PromiseMadeBy.them
    )
    assert [item.promise.description for item in theirs_only] == ["to'layman"]

    text = replies.promises_made_report(made, day=DAY, days_ago=0, today=DAY)
    assert text == (
        "📌 Bugun bergan va'dalaringiz (6):\n"
        "1. Akmal — konteynerni jo'nataman · muddat: bugun 18:00 · Telegram\n"
        "2. Vali — hujjatni &lt;b&gt;yuboraman&lt;/b&gt; · muddat: muddatsiz · "
        "Yuk guruhi\n"
        "3. Vali — pulni qaytaraman · muddat: 3 kundan keyin · MIYA bot\n"
        "4. Akmal — qo'ng'iroq qilaman · muddat: muddatsiz · Telegram\n"
        "5. Vali — ertaga uchrashamiz · muddat: muddatsiz · Telegram\n"
        "6. Vali — kelaman · muddat: muddatsiz · —"
    )
    yesterday, _ = await queries.promises_made(
        session, queries.day_bounds(DAY - timedelta(days=1))
    )
    assert [item.promise.description for item in yesterday] == ["kechagi va'da"]


async def test_promises_made_empty_day_says_so(session):
    made, total = await queries.promises_made(session, queries.day_bounds(DAY))
    assert made == [] and total == 0
    assert replies.promises_made_report(made, day=DAY, days_ago=0) == (
        "Bugun hech kimga va'da bermagansiz (yozuvlarda topilmadi)."
    )
    assert replies.promises_made_report(
        made, day=DAY - timedelta(days=4), days_ago=4
    ) == ("Juma kuni (16-okt) hech kimga va'da bermagansiz (yozuvlarda topilmadi).")


async def test_a_name_is_shown_without_bidi_controls(session):
    sly = await _person(session, "Akmal‮gpj.exe")
    note = await _message(
        session, chat=None, at=_at(DAY, 9), source=InteractionSource.assistant_bot
    )
    await _promise(session, sly, "yozaman​", source=note)
    made, _ = await queries.promises_made(session, queries.day_bounds(DAY))
    text = replies.promises_made_report(made, day=DAY, days_ago=0, today=DAY)
    assert "‮" not in text and "​" not in text
    assert "1. Akmalgpj.exe — yozaman ·" in text


async def test_promises_made_past_the_limit_say_how_many_more(session):
    vali = await _person(session, "Vali")
    for hour, text in ((9, "birinchi"), (10, "ikkinchi"), (11, "uchinchi")):
        note = await _message(
            session, chat=None, at=_at(DAY, hour), source=InteractionSource.assistant_bot
        )
        await _promise(session, vali, text, source=note)

    made, total = await queries.promises_made(session, queries.day_bounds(DAY), limit=2)

    assert [item.promise.description for item in made] == ["birinchi", "ikkinchi"]
    assert total == 3
    text = replies.promises_made_report(made, day=DAY, days_ago=0, total=total)
    assert text == (
        "📌 Bugun bergan va'dalaringiz (3):\n"
        "1. Vali — birinchi · muddat: muddatsiz · MIYA bot\n"
        "2. Vali — ikkinchi · muddat: muddatsiz · MIYA bot\n"
        "<i>… va yana 1 ta</i>"
    )


def test_a_long_promises_report_counts_what_did_not_fit():
    person = m.Person(display_name="Vali")
    items = [
        SimpleNamespace(
            promise=m.Promise(description=f"{index} " + "x" * 300, due_date=None),
            person=person,
            source=InteractionSource.assistant_bot,
            chat_type=None,
            chat_title=None,
        )
        for index in range(30)
    ]

    text = replies.promises_made_report(items, day=DAY, days_ago=0)

    shown = text.count(" — ")
    assert text.startswith("📌 Bugun bergan va'dalaringiz (30):")
    assert 0 < shown < 30
    assert text.endswith(f"<i>… va yana {30 - shown} ta</i>")
    assert len(text) <= formatting.TELEGRAM_LIMIT


# --- who wrote in a local day --------------------------------------------------------


async def test_who_wrote_private_and_addressed_only(session):
    await _monitors(session)
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    sardor = await _person(session, "Sardor")
    olim = await _person(session, "Olim")
    day = DAY - timedelta(days=1)

    # Akmal wrote twice in private; MIYA's stall after it answers nothing.
    await _message(session, chat=DM_AKMAL, person=akmal, at=_at(day, 9))
    await _message(session, chat=DM_AKMAL, person=akmal, at=_at(day, 10, 15))
    await _message(
        session, chat=DM_AKMAL, person=akmal, at=_at(day, 10, 20), out=True, meta=AI_TAG
    )
    # Vali wrote once and the owner answered the next morning.
    await _message(session, chat=DM_VALI, person=vali, at=_at(day, 11))
    await _message(session, chat=DM_VALI, person=vali, at=_at(DAY, 8), out=True)
    # Sardor aimed one group message at the owner, who wrote in the group after.
    await _message(
        session, chat=GROUP, person=sardor, at=_at(day, 12), meta={"to_me": True}
    )
    await _message(session, chat=GROUP, person=akmal, at=_at(day, 12, 30), out=True)
    # Not counted: group chatter not aimed at the owner, a "maybe" namesake
    # line, an archive import, a window row, the owner's own lines, the day
    # before and the day after.
    await _message(session, chat=GROUP, person=olim, at=_at(day, 13))
    await _message(
        session,
        chat=QUIET_GROUP,
        person=olim,
        at=_at(day, 13),
        meta={"to_me_maybe": True},
    )
    await _message(
        session,
        chat=DM_AKMAL,
        person=akmal,
        at=_at(day, 14),
        meta={"archive": True},
    )
    await _message(
        session, chat=DM_AKMAL, person=akmal, at=_at(day, 15), meta={"kind": "window"}
    )
    await _message(
        session, chat=DM_AKMAL, person=akmal, at=_at(day, 0) - timedelta(seconds=1)
    )
    await _message(session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 0))
    # A sender with no person row is named by the chat.
    await _message(session, chat=GROUP, at=_at(day, 16, 5), meta={"to_me": True})

    wrote, total = await queries.who_wrote(session, queries.day_bounds(day))

    assert total == 4

    assert [
        (item.person.display_name if item.person else None, item.messages, item.answered)
        for item in wrote
    ] == [
        ("Akmal", 2, False),
        ("Vali", 1, True),
        ("Sardor", 1, True),
        (None, 1, False),
    ]
    text = replies.who_wrote_report(wrote, day=day, days_ago=1)
    assert text == (
        "📨 Kecha sizga yozganlar (4):\n"
        "1. Akmal — 2 ta xabar, oxirgisi 10:15 · javobsiz\n"
        "2. Vali — 1 ta xabar, oxirgisi 11:00 · javob berilgan\n"
        "3. Sardor — 1 ta xabar, oxirgisi 12:00 · javob berilgan\n"
        "4. Yuk guruhi — 1 ta xabar, oxirgisi 16:05 · javobsiz"
    )


async def test_who_wrote_counts_the_answered_mark_and_owner_approved_words(session):
    await _monitors(session)
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    await _message(
        session,
        chat=DM_AKMAL,
        person=akmal,
        at=_at(DAY, 9),
        meta={"answered": {"at": _at(DAY, 9, 5).isoformat(), "by": "button"}},
    )
    await _message(session, chat=DM_VALI, person=vali, at=_at(DAY, 10))
    await _message(
        session, chat=DM_VALI, person=vali, at=_at(DAY, 10, 1), out=True, meta=OWNER_TAG
    )

    wrote, _ = await queries.who_wrote(session, queries.day_bounds(DAY))

    assert [item.answered for item in wrote] == [True, True]
    assert replies.who_wrote_report([], day=DAY, days_ago=0) == (
        "Bugun sizga hech kim yozmagan (yozuvlarda topilmadi)."
    )


async def test_who_wrote_past_the_limit_keeps_the_latest_and_says_how_many_more(
    session,
):
    await _monitors(session)
    for hour, name in ((9, "Akmal"), (10, "Vali"), (11, "Sardor")):
        person = await _person(session, name)
        await _message(
            session, chat=GROUP, person=person, at=_at(DAY, hour), meta={"to_me": True}
        )

    wrote, total = await queries.who_wrote(session, queries.day_bounds(DAY), limit=2)

    assert [item.person.display_name for item in wrote] == ["Vali", "Sardor"]
    assert total == 3
    text = replies.who_wrote_report(wrote, day=DAY, days_ago=0, total=total)
    assert text == (
        "📨 Bugun sizga yozganlar (3):\n"
        "<i>… va yana 1 ta</i>\n"
        "2. Vali — 1 ta xabar, oxirgisi 10:00 · javobsiz\n"
        "3. Sardor — 1 ta xabar, oxirgisi 11:00 · javobsiz"
    )


# --- the canonical questions ---------------------------------------------------------

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    ("text", "kind", "day"),
    [
        ("Bugun kimga nima va'da bergandim?", rag.OWNER_PROMISES, DAY),
        ("bugun kimga va’da berdim", rag.OWNER_PROMISES, DAY),
        ("Бугун кимга нима ваъда бергандим", rag.OWNER_PROMISES, DAY),
        ("O'tgan kuni kimga va'da bergan edim?", rag.OWNER_PROMISES, date(2026, 10, 18)),
        ("Kecha kimlar yozdi?", rag.OWNER_WHO_WROTE, date(2026, 10, 19)),
        ("kecha menga kim yozdi", rag.OWNER_WHO_WROTE, date(2026, 10, 19)),
        ("Кеча кимлар ёзди", rag.OWNER_WHO_WROTE, date(2026, 10, 19)),
        ("Juma kuni kimlar yozgan edi?", rag.OWNER_WHO_WROTE, date(2026, 10, 16)),
        ("Seshanba kimlar yozdi", rag.OWNER_WHO_WROTE, DAY),
    ],
)
def test_the_canonical_questions_are_recognised(text, kind, day):
    asked = rag.owner_question(text, NOW)
    assert asked is not None
    assert (asked.kind, asked.day) == (kind, day)
    assert (asked.start, asked.end) == queries.day_bounds(day)
    assert asked.days_ago == (DAY - day).days


@pytest.mark.parametrize(
    "text",
    [
        "Bugun Akmalga va'da berdim",  # a note with a name: it is written down
        "Kecha Akmal yozdi",
        "Kimlar yozdi?",  # no day
        "Bugun kecha kimga va'da berdim",  # two days
        "O'tgan dushanba kimlar yozdi",
        "Bugun kimga qarz berdim?",
        "Kecha nima bo'ldi?",
        # Promises made TO the owner: the model answers them (made_by=them).
        "Bugun menga nima va'da bergan edi?",
        "Kecha menga qanday va'da bergan?",
        "Bugun nima va'da bergan?",
        "",
    ],
)
def test_other_text_is_not_a_canonical_question(text):
    assert rag.owner_question(text, NOW) is None


def test_date_hints_name_the_local_day_window_and_each_weekday():
    hints = rag.date_hints(NOW).splitlines()
    assert hints[-2] == "day window: a date is [00:00, next day 00:00) Asia/Tashkent"
    assert hints[-1] == (
        "dushanba=2026-10-19; seshanba=2026-10-20; chorshanba=2026-10-14; "
        "payshanba=2026-10-15; juma=2026-10-16; shanba=2026-10-17; "
        "yakshanba=2026-10-18"
    )


# --- the bot: typed and spoken, no model call -----------------------------------------


class _TextMessage(_Message):
    def __init__(self, text: str, at: datetime) -> None:
        super().__init__()
        self.text = text
        self.date = at
        self.reply_to_message = None

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=500 + len(self.sent))


@pytest.fixture
def no_model(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("the model must not be called")

    def no_client():
        raise AssertionError("no model client for a SQL answer")

    async def no_typing(message):
        return None

    monkeypatch.setattr(handlers.rag, "answer_full", boom)
    monkeypatch.setattr(rag, "get_client", no_client)
    monkeypatch.setattr(handlers, "process_interaction", boom)
    monkeypatch.setattr(handlers, "_typing", no_typing)


async def test_fast_path_no_model_call(bound, no_model):  # noqa: F811
    now = datetime.now(TZ).replace(microsecond=0)
    today = now.date()
    akmal = await _person(bound, "Akmal")
    await _monitors(bound)
    said = await _message(
        bound, chat=DM_AKMAL, person=akmal, at=_at(today, 0, 0, 1), out=True
    )
    await _promise(bound, akmal, "konteynerni jo'nataman", source=said)

    message = _TextMessage("Bugun kimga nima va'da bergandim?", now)
    await handlers.on_text(message)

    [(text, markup)] = message.sent
    assert text == (
        "📌 Bugun bergan va'dalaringiz (1):\n"
        "1. Akmal — konteynerni jo'nataman · muddat: muddatsiz · Telegram"
    )
    row = await bound.scalar(
        sa.select(m.Interaction).where(
            m.Interaction.raw_text == "Bugun kimga nima va'da bergandim?"
        )
    )
    assert row.processed is True
    assert row.meta["kind"] == "question" and row.meta["sql"] == rag.OWNER_PROMISES
    assert row.meta["answer"] == text and row.meta["answer_message_id"] == 501
    assert markup.inline_keyboard[0][0].callback_data == f"vq:n:{row.id}"

    # No '?' and no question word: still the question, still no model.
    plain = _TextMessage("kecha kimlar yozdi", now)
    await handlers.on_text(plain)
    [(text, _)] = plain.sent
    assert text == "Kecha sizga hech kim yozmagan (yozuvlarda topilmadi)."


async def test_a_failed_query_falls_back_to_the_model(bound, monkeypatch, caplog):  # noqa: F811
    async def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    asked = []

    async def answer_full(session, text, *, mode=None, **kwargs):
        asked.append((text, mode))
        return rag.RagAnswer(text="model javobi")

    async def no_typing(message):
        return None

    monkeypatch.setattr(queries, "who_wrote", broken)
    monkeypatch.setattr(handlers.rag, "answer_full", answer_full)
    monkeypatch.setattr(handlers, "_typing", no_typing)

    message = _TextMessage("Kecha kimlar yozdi?", datetime.now(TZ))
    await handlers.on_text(message)

    assert message.sent[0][0] == "model javobi"
    assert asked == [("Kecha kimlar yozdi?", "question")]
    [record] = [r for r in caplog.records if r.getMessage().startswith("sql answer")]
    assert record.error == "RuntimeError" and record.kind == rag.OWNER_WHO_WROTE
    assert isinstance(record.interaction_id, int)


def _voice_message(at: datetime):
    message = SimpleNamespace(
        caption=None,
        date=at,
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        voice=SimpleNamespace(mime_type="audio/ogg", file_size=10, duration=3),
        audio=None,
        video_note=None,
        reply_to_message=None,
        sent=[],
    )

    async def _answer(text, **kwargs):
        message.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=700 + len(message.sent))

    message.answer = _answer
    return message


async def test_voice_routes_to_fast_path(bound, no_model, monkeypatch, tmp_path):  # noqa: F811
    heard = "kecha menga kimlar yozdi"
    now = datetime.now(TZ).replace(microsecond=0)
    yesterday = now.date() - timedelta(days=1)
    await _monitors(bound)
    vali = await _person(bound, "Vali")
    await _message(bound, chat=DM_VALI, person=vali, at=_at(yesterday, 11, 40))

    async def _download(bot, message, media, path):
        return True

    async def _transcribe(session, interaction, path):
        interaction.transcript = heard
        return heard

    monkeypatch.setattr(handlers, "_download", _download)
    monkeypatch.setattr(handlers, "_media_path", lambda suffix: tmp_path / f"a{suffix}")
    monkeypatch.setattr(handlers, "transcribe_into", _transcribe)
    assert handlers._spoken_mode(heard) == "question"

    message = _voice_message(now)
    await handlers.on_voice(message, bot=None)

    [(text, _)] = message.sent
    assert text == (
        "🎙 <i>Savolingiz:</i> «kecha menga kimlar yozdi»\n\n"
        "📨 Kecha sizga yozganlar (1):\n"
        "1. Vali — 1 ta xabar, oxirgisi 11:40 · javobsiz"
    )
    row = await bound.scalar(
        sa.select(m.Interaction).where(m.Interaction.transcript == heard)
    )
    assert row.meta["via"] == "voice" and row.meta["sql"] == rag.OWNER_WHO_WROTE


# --- the model's toolbox -------------------------------------------------------------


async def test_the_tools_answer_from_sql(session):
    await _monitors(session)
    akmal = await _person(session, "Akmal")
    said = await _message(session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 9), out=True)
    await _promise(
        session, akmal, "jo'nataman", source=said, due=DAY, due_time=time(18, 0)
    )
    await _message(session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 10))

    made = json.loads(
        await rag._run_tool(session, None, "promises_made", {"date": DAY.isoformat()})
    )
    assert made["made_by"] == "me" and made["note"] == rag.UNTRUSTED_NOTE
    assert made["total"] == 1
    assert made["promises"] == [
        {
            "person": "Akmal",
            "description": "jo'nataman",
            "due_date": "2026-10-20",
            "due_time": "18:00",
            "status": "open",
            "made_at": _at(DAY, 9).isoformat(),
            "source": "telegram_userbot",
            "chat": "Akmal",
        }
    ]
    wrote = json.loads(
        await rag._run_tool(session, None, "who_wrote", {"date": DAY.isoformat()})
    )
    assert wrote == {
        "date": "2026-10-20",
        "note": rag.UNTRUSTED_NOTE,
        "total": 1,
        "people": [
            {
                "person": "Akmal",
                "messages": 1,
                "last_at": _at(DAY, 10).isoformat(),
                "answered": False,
            }
        ],
    }
    assert {"promises_made", "who_wrote"} <= rag.SQL_TOOLS


# --- the model call is asked again when it comes back empty --------------------------


def _empty_response() -> SimpleNamespace:
    return SimpleNamespace(content=[], stop_reason="end_turn", usage=_usage())


@pytest.fixture
def no_sleep(monkeypatch):
    waits: list[float] = []
    real = retry.with_backoff

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    async def recorded(fn, **kwargs):
        kwargs["sleep"] = _sleep
        return await real(fn, **kwargs)

    monkeypatch.setattr(retry, "with_backoff", recorded)
    return waits


async def test_rag_backoff_retries(session, monkeypatch, no_sleep):
    stub = _StubClient([_empty_response(), _text_response("Javob tayyor.")])
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    answer = await rag.answer_full(session, "Akmal qachon keladi?", now=NOW)

    assert answer.text == "Javob tayyor." and answer.model_called is True
    assert len(stub.calls) == 2 and len(no_sleep) == 1
    await session.flush()
    spent = await session.scalar(
        sa.select(sa.func.count()).where(m.UsageLog.operation == "rag")
    )
    assert spent == 2  # the empty answer was paid for too


async def test_rag_backoff_gives_up_and_never_retries_a_transport_failure(
    session, monkeypatch, no_sleep
):
    empty = _StubClient([_empty_response() for _ in range(rag.ANSWER_ATTEMPTS)])
    monkeypatch.setattr(rag, "get_client", lambda: empty)
    answer = await rag.answer_full(session, "Akmal qachon keladi?", now=NOW)
    assert answer.text == rag.FALLBACK_ANSWER
    assert len(empty.calls) == rag.ANSWER_ATTEMPTS

    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")
    )
    failing = _StubClient([error])
    monkeypatch.setattr(rag, "get_client", lambda: failing)
    answer = await rag.answer_full(session, "Akmal qachon keladi?", now=NOW)
    assert answer.text == rag.FALLBACK_ANSWER and len(failing.calls) == 1


# --- /tarix: MIYA's reply is MIYA's line, not the owner's (JV-10) --------------------


async def test_timeline_labels_miya_rows_as_miya(session):
    akmal = await _person(session, "Akmal")
    ai = await _message(
        session,
        chat=DM_AKMAL,
        person=akmal,
        at=_at(DAY, 9),
        out=True,
        meta=AI_TAG,
        text="aniqlashtirib xabar beraman",
    )
    own = await _message(
        session,
        chat=DM_AKMAL,
        person=akmal,
        at=_at(DAY, 10),
        out=True,
        meta=OWNER_TAG,
        text="ertaga kelaman",
    )
    theirs = await _message(session, chat=DM_AKMAL, person=akmal, at=_at(DAY, 11))

    lines = [
        formatting.timeline_line(queries.timeline_entry(row)) for row in (ai, own, theirs)
    ]

    assert lines[0].endswith("· 💬 telegram · MIYA: aniqlashtirib xabar beraman")
    assert lines[1].endswith("· 💬 telegram · sen: ertaga kelaman")
    assert lines[2].endswith("· 💬 telegram · u: salom")
    assert rag._jsonable(queries.timeline_entry(ai))["author"] == "miya"
    assert "author" not in rag._jsonable(queries.timeline_entry(own))
