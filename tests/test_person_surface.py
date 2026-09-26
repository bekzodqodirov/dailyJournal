"""Build step 4, the bot surface of per-person memory.

`/kim` answers "Akmal kim?" from everything MIYA holds about him — identity,
the written profile, the SQL figures, the facts, the last contacts — and asks
back when two people fit the name. `/tarix` is the full history, oldest at the
top. `/eslab` lets the owner tell MIYA something about a person in his own
words. Everything the owner reads goes through `escape`; nothing a counterparty
wrote can break the message.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from miya.bot import formatting as f
from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
)
from miya.services import queries
from miya.services.people import Match
from miya.services.queries import PersonSummary, TimelineEntry
from tests.test_close_and_correct import _command, _Message

TZ = settings.tz
THIS_YEAR = datetime.now(TZ).year

# The /kim fixture's dates, relative to the real clock: the same shape as the
# original fixed calendar (a fact, then the records, then three contacts on
# consecutive days, the profile written the morning of the last one).
KIM_BASE = (datetime.now(TZ) - timedelta(days=15)).replace(
    hour=12, minute=0, second=0, microsecond=0
)
KIM_RECORDED = (KIM_BASE - timedelta(days=5)).replace(hour=10)
KIM_FACT_AT = (KIM_BASE - timedelta(days=9)).replace(hour=0)
KIM_PROFILE_AT = (KIM_BASE + timedelta(days=2)).replace(hour=9)


def _now() -> datetime:
    return datetime.now(TZ)


@pytest.fixture
def bound(session, monkeypatch):
    """Handlers run inside the test session instead of opening their own."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def _person(session, name: str, **fields) -> m.Person:
    person = m.Person(display_name=name, **{"aliases": [], **fields})
    session.add(person)
    await session.flush()
    return person


async def _interaction(
    session,
    person: m.Person,
    *,
    source: InteractionSource = InteractionSource.assistant_bot,
    direction: Direction = Direction.in_,
    at: datetime | None = None,
    text: str | None = None,
    summary: str | None = None,
    meta: dict | None = None,
) -> m.Interaction:
    interaction = m.Interaction(
        source=source,
        direction=direction,
        person_id=person.id,
        occurred_at=at or _now(),
        raw_text=text,
        summary=summary,
        meta=meta,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _fact(session, person: m.Person, content: str, *, at=None) -> m.Memory:
    memory = m.Memory(
        content=content,
        embedding=None,
        person_id=person.id,
        occurred_at=at or _now(),
        tags=["person"],
    )
    session.add(memory)
    await session.flush()
    return memory


async def _akmal_with_everything(session) -> m.Person:
    """One person with every kind of thing /kim can show."""
    akmal = await _person(
        session,
        "Akmal",
        aliases=["Akmal aka", "Akmal GZ"],
        telegram_username="akmal_gz",
        phone="+998901234567",
        relationship_="yuk beruvchi mijoz",
        notes="Xitoydan yuk olib keladi.\nOdatda o'z vaqtida to'laydi.",
        profile_updated_at=KIM_PROFILE_AT,
    )
    # Dated before the receipt below: a debt or promise recorded today would
    # itself be the last contact, which last_contact_at rightly counts.
    recorded = KIM_RECORDED
    session.add(
        m.Debt(
            direction=DebtDirection.they_owe_me,
            person_id=akmal.id,
            amount=Decimal("5000000"),
            currency=Currency.UZS,
            created_at=recorded,
        )
    )
    session.add(
        m.Promise(
            made_by=PromiseMadeBy.them,
            person_id=akmal.id,
            description="invoice yuboradi",
            created_at=recorded,
        )
    )
    await session.flush()
    await _fact(session, akmal, "Mashinasi oq Malibu", at=KIM_FACT_AT)
    await _fact(session, akmal, "Ukasi Sardor bilan ishlaydi")
    base = KIM_BASE
    await _interaction(
        session,
        akmal,
        source=InteractionSource.phone_call,
        at=base,
        summary="Yuk ertaga jo'natilishi kelishildi",
    )
    await _interaction(
        session,
        akmal,
        source=InteractionSource.telegram_userbot,
        direction=Direction.na,
        at=base + timedelta(days=1),
        text="[THEM] salom",
        summary="Konteyner raqamini so'radi",
        meta={"kind": "window"},
    )
    await _interaction(
        session,
        akmal,
        source=InteractionSource.receipt_photo,
        at=base + timedelta(days=2),
        text="chek 300$",
    )
    return akmal


def _reply(message: _Message) -> str:
    [(text, _)] = message.sent
    return text


# --- /kim ------------------------------------------------------------------


async def test_kim_shows_everything_held_about_a_person(bound):
    await _akmal_with_everything(bound)
    message = _Message()

    await handlers.cmd_person(message, _command("kim", "Akmal aka"))

    text = _reply(message)
    # identity: name, aliases, username, phone, relationship
    assert "<b>Akmal</b>" in text
    assert "Akmal aka, Akmal GZ" in text
    assert "@akmal_gz" in text and "+998901234567" in text
    assert "yuk beruvchi mijoz" in text
    # profile with its date
    assert f"📝 <b>Profil</b> <i>({f.day_label(KIM_PROFILE_AT)})</i>" in text
    assert "Xitoydan yuk olib keladi." in text
    assert "Profil hali yozilmagan" not in text
    # figures from SQL
    assert "5 mln so'm" in text and "→ senga" in text
    assert "U: invoice yuboradi" in text
    # facts, newest first
    assert "🧠" in text
    assert text.index("Ukasi Sardor bilan ishlaydi") < text.index("Mashinasi oq Malibu")
    assert f"{f.day_label(KIM_FACT_AT)} · Mashinasi oq Malibu" in text
    # timeline with source emojis
    day = f.day_label
    assert f"{day(KIM_BASE)} · 📞 qo'ng'iroq · Yuk ertaga jo'natilishi kelishildi" in text
    assert (
        f"{day(KIM_BASE + timedelta(days=1))} · 💬 telegram · Konteyner raqamini so'radi"
        in text
    )
    assert f"{day(KIM_BASE + timedelta(days=2))} · 🧾 chek · chek 300$" in text
    # last contact and totals
    assert f"Oxirgi aloqa: {f.day_label(KIM_BASE + timedelta(days=2))}" in text
    assert "jami 3 ta aloqa" in text
    assert "/tarix Akmal — to'liq tarix" in text
    assert len(text) <= f.TELEGRAM_LIMIT + 40


async def test_kim_says_when_no_profile_has_been_written(bound):
    akmal = await _person(bound, "Akmal")
    await _interaction(bound, akmal, text="salom")
    message = _Message()

    await handlers.cmd_person(message, _command("kim", "Akmal"))

    text = _reply(message)
    assert replies.PROFILE_MISSING in text
    assert "💰 Ochiq qarz yo'q" in text
    assert "jami 1 ta aloqa" in text


async def test_kim_asks_back_when_two_people_fit_the_name(bound):
    await _person(bound, "Akmal GZ")
    await _person(bound, "Akmal Toshkent")
    message = _Message()

    await handlers.cmd_person(message, _command("kim", "Akmal"))

    text = _reply(message)
    assert text.startswith("❓ Kimni nazarda tutding:")
    assert "<b>Akmal GZ</b>" in text and "<b>Akmal Toshkent</b>" in text
    assert "<code>/kim Akmal GZ</code>" in text


async def test_kim_reports_an_unknown_name(bound):
    await _person(bound, "Akmal")
    message = _Message()

    await handlers.cmd_person(message, _command("kim", "Dilnoza"))

    assert _reply(message) == replies.person_not_found("Dilnoza")


async def test_kim_without_a_name_shows_usage(bound):
    message = _Message()
    await handlers.cmd_person(message, _command("kim", ""))
    assert _reply(message) == replies.KIM_USAGE


async def test_kim_escapes_everything_a_counterparty_could_have_written(bound):
    hostile = await _person(
        bound,
        "<b>Ali</b>",
        aliases=["<i>Alik</i>"],
        telegram_username="<u>",
        relationship_="<script>",
        notes="<b>profil</b> & co",
        profile_updated_at=_now(),
    )
    await _fact(bound, hostile, "<img src=x>")
    await _interaction(
        bound,
        hostile,
        source=InteractionSource.phone_call,
        summary="<a href='x'>link</a>",
    )
    message = _Message()

    await handlers.cmd_person(message, _command("kim", "Ali"))

    text = _reply(message)
    for raw in ("<b>Ali</b>", "<i>Alik</i>", "<u>", "<script>", "<img", "<a href"):
        assert raw not in text
    assert "&lt;b&gt;Ali&lt;/b&gt;" in text
    assert "&lt;b&gt;profil&lt;/b&gt; &amp; co" in text
    assert "&lt;img src=x&gt;" in text
    assert "&lt;a href='x'&gt;link&lt;/a&gt;" in text
    assert "/tarix &lt;b&gt;Ali&lt;/b&gt;" in text


def test_person_ambiguous_escapes_both_names():
    match = Match(
        person=m.Person(display_name="<A>", aliases=[]),
        score=90,
        runner_up=m.Person(display_name="<B>", aliases=[]),
        runner_up_score=88,
    )
    text = replies.person_ambiguous(match, command="eslab")
    assert "<A>" not in text and "&lt;A&gt;" in text and "&lt;B&gt;" in text
    assert "/eslab &lt;A&gt;: …" in text


def test_person_report_still_works_with_a_bare_summary():
    """The API and older callers build a PersonSummary with the old fields only."""
    person = m.Person(display_name="Akmal", aliases=[])
    text = replies.person_report(PersonSummary(person=person, total_interactions=0))
    assert "<b>Akmal</b>" in text
    assert replies.PROFILE_MISSING in text
    assert "jami 0 ta aloqa" in text


# --- /tarix ----------------------------------------------------------------


async def test_tarix_lists_oldest_to_newest_and_caps_at_100(bound):
    akmal = await _person(bound, "Akmal")
    base = datetime(2026, 9, 1, 8, 0, tzinfo=TZ)
    for i in range(105):
        await _interaction(bound, akmal, at=base + timedelta(minutes=i), text=f"n{i:03d}")
    message = _Message()

    await handlers.cmd_history(message, _command("tarix", "Akmal 500"))

    text = _reply(message)
    shown = [line for line in text.splitlines() if line.startswith("• ")]
    assert len(shown) == 100
    assert shown[0].endswith("n005")  # the five oldest fell off the cap
    assert shown[-1].endswith("n104")
    assert "oxirgi 100 ta" in text
    assert "Ko'proq" not in text  # the cap is the cap
    assert len(text) <= f.TELEGRAM_LIMIT + 40


async def test_tarix_defaults_to_30_and_reads_a_count(bound):
    akmal = await _person(bound, "Akmal")
    base = datetime(2026, 9, 1, 8, 0, tzinfo=TZ)
    for i in range(40):
        await _interaction(bound, akmal, at=base + timedelta(minutes=i), text=f"n{i:03d}")

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Akmal"))
    shown = [line for line in _reply(message).splitlines() if line.startswith("• ")]
    assert len(shown) == 30 and shown[0].endswith("n010")
    assert "<i>Ko'proq: /tarix Akmal 60</i>" in _reply(message)

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Akmal 50"))
    assert "Ko'proq" not in _reply(message)  # 40 rows: the history is complete

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Akmal 5"))
    shown = [line for line in _reply(message).splitlines() if line.startswith("• ")]
    assert [line[-4:] for line in shown] == ["n035", "n036", "n037", "n038", "n039"]


async def test_tarix_keeps_the_newest_lines_when_the_message_overflows(bound):
    akmal = await _person(bound, "Akmal")
    base = datetime(2026, 9, 1, 8, 0, tzinfo=TZ)
    for i in range(100):
        await _interaction(
            bound, akmal, at=base + timedelta(minutes=i), text=f"n{i:03d} " + "x" * 150
        )
    message = _Message()

    await handlers.cmd_history(message, _command("tarix", "Akmal 100"))

    text = _reply(message)
    assert len(text) <= f.TELEGRAM_LIMIT
    shown = [line for line in text.splitlines() if line.startswith("• ")]
    assert 0 < len(shown) < 100
    assert "n099" in shown[-1]  # the newest survived; the oldest were cut
    assert "eskilari sig'madi" in text
    assert "(qisqartirildi)" not in text


async def test_tarix_hides_raw_member_lines_and_shows_the_window(bound):
    akmal = await _person(bound, "Akmal")
    window = m.ConversationWindow(
        tg_chat_id=1001,
        person_id=akmal.id,
        started_at=_now() - timedelta(minutes=10),
        ended_at=_now(),
        message_count=1,
        char_count=5,
        text="[THEM] salom",
        custom_id="w-surface",
    )
    bound.add(window)
    await bound.flush()
    member = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=akmal.id,
        occurred_at=_now() - timedelta(minutes=5),
        raw_text="salom, yuk qachon?",
        window_id=window.id,
    )
    bound.add(member)
    await _interaction(
        bound,
        akmal,
        source=InteractionSource.telegram_userbot,
        direction=Direction.na,
        summary="Yuk haqida so'radi",
        meta={"kind": "window"},
    )
    message = _Message()

    await handlers.cmd_history(message, _command("tarix", "Akmal"))

    text = _reply(message)
    assert "💬 telegram · Yuk haqida so'radi" in text
    assert "salom, yuk qachon?" not in text


async def test_tarix_with_no_history_and_with_the_kim_replies(bound):
    await _person(bound, "Akmal")
    await _person(bound, "Akmal GZ")
    await _person(bound, "Sardor")

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Sardor"))
    assert _reply(message) == "🕐 <b>Sardor</b> bilan hali aloqa yozilmagan."

    # "Akmal" is exactly one person's name, so it resolves even though
    # "Akmal GZ" contains it; only near-names ask back.
    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Akmal"))
    assert _reply(message) == "🕐 <b>Akmal</b> bilan hali aloqa yozilmagan."

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Akmal G"))
    assert "Kimni nazarda tutding" in _reply(message)

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", "Dilnoza 10"))
    assert _reply(message) == replies.person_not_found("Dilnoza")

    message = _Message()
    await handlers.cmd_history(message, _command("tarix", ""))
    assert _reply(message) == replies.TARIX_USAGE


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("Akmal", ("Akmal", 30)),
        ("Akmal 50", ("Akmal", 50)),
        ("Akmal GZ 7", ("Akmal GZ", 7)),
        ("Akmal 0", ("Akmal", 1)),
        ("Akmal 9999", ("Akmal", 100)),
        ("  Akmal  ", ("Akmal", 30)),
    ],
)
def test_parse_history_args(args, expected):
    assert handlers._parse_history_args(args) == expected


# --- /eslab ----------------------------------------------------------------


async def _memories(session, person_id: int) -> list[m.Memory]:
    return list(
        await session.scalars(
            sa.select(m.Memory)
            .where(m.Memory.person_id == person_id)
            .order_by(m.Memory.id)
        )
    )


@pytest.mark.parametrize(
    "args",
    [
        "Akmal: mashinasi oq Malibu",
        "Akmal — mashinasi oq Malibu",
        "Akmal aka:mashinasi oq Malibu",
    ],
)
async def test_eslab_writes_a_manual_memory_against_the_person(bound, args):
    akmal = await _person(bound, "Akmal")
    before = _now()
    message = _Message()

    await handlers.cmd_remember(message, _command("eslab", args))

    [memory] = await _memories(bound, akmal.id)
    assert memory.content == "mashinasi oq Malibu"
    assert memory.tags == ["manual"]
    assert memory.embedding is None  # the worker embeds it
    assert memory.source_interaction_id is None
    assert memory.occurred_at >= before
    assert _reply(message) == "🧠 Eslab qoldim: <b>Akmal</b> — «mashinasi oq Malibu»"


async def test_eslab_shows_up_in_kim_afterwards(bound):
    akmal = await _person(bound, "Akmal")
    await handlers.cmd_remember(_Message(), _command("eslab", "Akmal: ukasi Sardor"))
    facts = await queries.person_summary(bound, akmal)
    assert [fact.content for fact in facts.facts] == ["ukasi Sardor"]

    message = _Message()
    await handlers.cmd_person(message, _command("kim", "Akmal"))
    assert "ukasi Sardor" in _reply(message)


@pytest.mark.parametrize("args", ["", "Akmal", "Akmal:", ": matn", "Akmal —   "])
async def test_eslab_malformed_shows_usage_and_writes_nothing(bound, args):
    akmal = await _person(bound, "Akmal")
    message = _Message()

    await handlers.cmd_remember(message, _command("eslab", args))

    assert _reply(message) == replies.ESLAB_USAGE
    assert await _memories(bound, akmal.id) == []


async def test_eslab_asks_back_or_refuses_like_kim(bound):
    await _person(bound, "Akmal GZ")
    await _person(bound, "Akmal Toshkent")

    message = _Message()
    await handlers.cmd_remember(message, _command("eslab", "Akmal: uyi Chilonzorda"))
    assert "Kimni nazarda tutding" in _reply(message)
    assert "<code>/eslab Akmal GZ: …</code>" in _reply(message)

    message = _Message()
    await handlers.cmd_remember(message, _command("eslab", "Dilnoza: opa"))
    assert _reply(message) == replies.person_not_found("Dilnoza")

    count = await bound.scalar(sa.select(sa.func.count()).select_from(m.Memory))
    assert count == 0


async def test_eslab_escapes_the_owners_words_on_render_only(bound):
    akmal = await _person(bound, "Akmal")
    message = _Message()

    await handlers.cmd_remember(message, _command("eslab", "Akmal: <b>bold</b> & co"))

    [memory] = await _memories(bound, akmal.id)
    assert memory.content == "<b>bold</b> & co"  # stored as typed
    text = _reply(message)
    assert "<b>bold</b>" not in text and "&lt;b&gt;bold&lt;/b&gt; &amp; co" in text


# --- formatting ------------------------------------------------------------


def _entry(source, text, *, direction=Direction.in_, when=None) -> TimelineEntry:
    when = when or datetime(THIS_YEAR, 9, 12, 10, 0, tzinfo=TZ)
    return TimelineEntry(
        interaction=m.Interaction(),
        when=when,
        source=source,
        direction=direction,
        text=text,
    )


@pytest.mark.parametrize(
    ("source", "emoji", "word"),
    [
        (InteractionSource.phone_call, "📞", "qo'ng'iroq"),
        (InteractionSource.telegram_userbot, "💬", "telegram"),
        (InteractionSource.assistant_bot, "✍️", "yozuv"),
        (InteractionSource.manual, "✍️", "yozuv"),
        (InteractionSource.receipt_photo, "🧾", "chek"),
        (InteractionSource.calendar, "📅", "uchrashuv"),
    ],
)
def test_timeline_line_names_every_source(source, emoji, word):
    line = f.timeline_line(_entry(source, "salom", direction=Direction.na))
    assert line == f"12-sen · {emoji} {word} · salom"
    assert f.SOURCE_EMOJI[source] == emoji


def test_timeline_line_folds_clips_escapes_and_names_the_speaker():
    entry = _entry(
        InteractionSource.telegram_userbot,
        "  <b>salom</b>\n\n" + "x" * 300,
        direction=Direction.out,
    )
    line = f.timeline_line(entry)
    assert line.startswith("12-sen · 💬 telegram · sen: &lt;b&gt;salom&lt;/b&gt; xxx")
    assert line.endswith("…")
    plain = f.timeline_line(entry, markup=False)
    assert "<b>salom</b>" in plain and "&lt;" not in plain
    assert f.timeline_line(_entry(InteractionSource.phone_call, "")).endswith(
        f.TIMELINE_NO_TEXT
    )
    # an older year is spelled out; an owner note carries no speaker
    old = _entry(
        InteractionSource.assistant_bot,
        "eski",
        when=datetime(THIS_YEAR - 1, 3, 2, tzinfo=TZ),
    )
    assert f.timeline_line(old) == f"2-mar {THIS_YEAR - 1} · ✍️ yozuv · eski"


def test_operation_label_and_help_cover_the_new_surface():
    assert replies.OPERATION_LABEL["profile"] == "odam haqida profil"
    assert "/tarix" in replies.HELP
    assert "/eslab" in replies.HELP
    assert "/kim" in replies.HELP
