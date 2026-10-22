"""JV-14: the owner's style profile, few-shot examples and per-person tone.

The style is learned only from the owner's own words (never a reply MIYA
sent), in private chats with real people the owner did not mark personal.
Examples are scrubbed deny-by-default and must pass the red-line guard in the
owner role without a hit, so the AI never learns to promise. No network: the
model and the embedder are fakes.
"""

from __future__ import annotations

import importlib.util
import itertools
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiogram.filters import CommandObject
from sqlalchemy.exc import IntegrityError

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.jarvis import gate, outbox, policy, redlines, stalls, style, style_prompt
from miya.services import chats, people, purge

TZ = settings.tz
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = 900001
AKMAL_CHAT, SARDOR_CHAT, KARIM_CHAT = 7101, 7102, 7103
_ids = itertools.count(1000)

AI_TAG = {"sent_by": "miya", "authored_by": "ai", "outbox_id": 41}
OWNER_TAG = {"sent_by": "miya", "authored_by": "owner", "outbox_id": 42}

# (client line, owner reply): every reply here is admissible.
CLEAN_PAIRS = [
    ("Assalomu alaykum, yuk bormi?", "Assalomu alaykum aka, ko'rib beraman"),
    ("Salom, qalaysiz?", "Rahmat aka, yaxshi. O'zingiz yaxshimisiz?"),
    ("Yuk qayerdan keladi?", "Qaysi shahardan, aka?"),
    ("Ishlar qalay?", "Salom aka, qanday yordam bera olaman?"),
    ("Kecha yozgan edim", "Aka, yukni ko'rib beraman 🙂"),
    ("Hujjatlar kerakmi?", "Qanaqa yuk ekan, aka?"),
]


class FakeEmbedder:
    """Deterministic unit vectors: the same text, the same vector."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for text in texts:
            vector = [0.0] * settings.embed_dim
            vector[sum(map(ord, text)) % settings.embed_dim] = 1.0
            out.append(vector)
        return out


class FakeClient:
    """The model client: one fixed answer, every call recorded."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []
        self.messages = self

    def with_options(self, **kwargs):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=120, output_tokens=60),
        )


GOOD_PROSE = (
    '{"prompt_text": "The owner writes short, warm messages in Latin script. '
    "They greet with «Assalomu alaykum» and call clients «aka», always with the "
    'polite «siz». They ask short questions and rarely use emoji."}'
)


@pytest.fixture
def styled(monkeypatch):
    """Small sample sizes, the owner's own id, fake model and embedder."""
    monkeypatch.setattr(settings, "style_min_samples", 4)
    monkeypatch.setattr(settings, "style_sample_size", 300)
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "owner_aliases", "")
    monkeypatch.setattr(settings, "reason_model", "test-reason-model")
    monkeypatch.setattr(settings, "reason_model_price", "3.00,15.00")
    embedder = FakeEmbedder()
    monkeypatch.setattr(style, "get_embedder", lambda: embedder)
    client = FakeClient(GOOD_PROSE)
    monkeypatch.setattr(style, "get_client", lambda: client)
    return SimpleNamespace(embedder=embedder, client=client)


async def _person(session, name, *, telegram_id=None, username=None) -> m.Person:
    person = m.Person(
        display_name=name,
        aliases=[],
        telegram_id=telegram_id,
        telegram_username=username,
    )
    session.add(person)
    await session.flush()
    return person


async def _monitor(
    session, chat, *, chat_type=ChatType.private, enabled=True, title="Chat"
) -> m.ChatMonitor:
    monitor = m.ChatMonitor(
        tg_chat_id=chat, chat_type=chat_type, title=title, monitor_enabled=enabled
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def _msg(
    session, text, *, at, out=False, person=None, chat=AKMAL_CHAT, meta=None, media=None
) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        media=media,
        meta={"tg_message_id": next(_ids), **(meta or {})},
    )
    session.add(row)
    await session.flush()
    return row


async def _dialogue(session, person, chat, pairs, *, start=None):
    """Client line, then the owner's reply a minute later, pair after pair."""
    at = start or NOW - timedelta(days=1)
    made = []
    for client_line, owner_line in pairs:
        asked = await _msg(session, client_line, at=at, person=person, chat=chat)
        answered = await _msg(
            session,
            owner_line,
            at=at + timedelta(minutes=1),
            out=True,
            person=person,
            chat=chat,
        )
        made.append((asked, answered))
        at += timedelta(minutes=10)
    return made


async def _akmal(session):
    person = await _person(session, "Akmal", telegram_id=AKMAL_CHAT)
    await _monitor(session, AKMAL_CHAT, title="Akmal")
    pairs = await _dialogue(session, person, AKMAL_CHAT, CLEAN_PAIRS)
    return person, pairs


# --- the sampler ------------------------------------------------------------------------


async def test_sampler_excludes_miya_rows(session, styled):
    """Only the owner's own words: a reply MIYA wrote itself is never a
    sample; a text the owner dictated or approved word for word is; an
    archive import is; a client's message is not."""
    person = await _person(session, "Akmal", telegram_id=AKMAL_CHAT)
    await _monitor(session, AKMAL_CHAT)
    at = NOW - timedelta(hours=5)
    mine = await _msg(session, "Salom aka", at=at, out=True, person=person)
    ai = await _msg(
        session,
        "Aka, hozir buni aniqlashtirib, o'zim tez orada yozvoraman.",
        at=at + timedelta(minutes=1),
        out=True,
        person=person,
        meta=AI_TAG,
    )
    dictated = await _msg(
        session,
        "Rahmat aka",
        at=at + timedelta(minutes=2),
        out=True,
        person=person,
        meta=OWNER_TAG,
    )
    archived = await _msg(
        session,
        "Qaysi shahardan, aka?",
        at=at - timedelta(days=200),
        out=True,
        person=person,
        meta={"archive": True},
    )
    client = await _msg(
        session, "Yuk bormi?", at=at + timedelta(minutes=3), person=person
    )
    ids = {s.interaction_id for s in await style.sample(session)}
    assert ids == {mine.id, dictated.id, archived.id}
    assert ai.id not in ids and client.id not in ids


async def test_sampler_skips_personal_bots_groups_and_saved_messages(session, styled):
    """Never a chat the owner marked personal (by the chat or the person),
    a bot, a group, the owner's own Saved Messages, Telegram's service chat,
    a chat switched off, or a media message."""
    at = NOW - timedelta(hours=3)
    akmal, _ = await _akmal(session)
    family = await _person(session, "Onam", telegram_id=KARIM_CHAT)
    await _monitor(session, KARIM_CHAT, title="Onam")
    await _msg(session, "Salom onajon", at=at, out=True, person=family, chat=KARIM_CHAT)
    await policy.mark_person(session, family, by="command", now=NOW)
    friend = await _person(session, "Sardor", telegram_id=SARDOR_CHAT)
    friend_chat = await _monitor(session, SARDOR_CHAT, title="Sardor")
    await _msg(session, "Qalaysan", at=at, out=True, person=friend, chat=SARDOR_CHAT)
    await policy.mark_chat(session, friend_chat, by="command", now=NOW)
    bot = await _person(session, "Kuryer", telegram_id=7201, username="CourierHelpBot")
    await _monitor(session, 7201)
    await _msg(session, "start", at=at, out=True, person=bot, chat=7201)
    await _monitor(session, -7301, chat_type=ChatType.group)
    await _msg(session, "Salom hammaga", at=at, out=True, chat=-7301)
    await _monitor(session, OWNER_ID)
    await _msg(session, "eslatma", at=at, out=True, chat=OWNER_ID)
    await _monitor(session, 777000)
    await _msg(session, "kod", at=at, out=True, chat=777000)
    quiet = await _person(session, "Bobur", telegram_id=7401)
    await _monitor(session, 7401, enabled=False)
    await _msg(session, "Salom", at=at, out=True, person=quiet, chat=7401)
    await _msg(
        session,
        "rasm",
        at=at,
        out=True,
        person=akmal,
        media={"type": "photo", "caption": "rasm"},
    )
    sampled = await style.sample(session)
    assert {s.tg_chat_id for s in sampled} == {AKMAL_CHAT}
    assert len(sampled) == len(CLEAN_PAIRS)
    # Each reply carries the client message it answered.
    assert all(s.context_id is not None for s in sampled)


# --- the scrubber -----------------------------------------------------------------------


def test_scrubber_drops_figures_names_links():
    names = style.build_name_index(
        [(1, "Akmal Karimov", ["Akmal aka"]), (2, "Sardor", []), (3, "Aka", [])],
        owner_aliases=("Bekzod",),
    )
    admitted = [
        "Salom Akmal aka, ko'rib beraman",
        "Rahmat aka",
        "Qaysi shahardan, aka?",
    ]
    refused = [
        "Ertaga 5 da tashlab beraman",  # a digit, a time, a promise
        "Narxi 100 ming bo'ladi",  # a price
        "Xo'p bo'ladi aka",  # a promise
        "Ertaga tayyor bo'ladi",  # a deadline
        "Mana https://cargo.uz/track",  # a link
        "@sardor_cargo ga yozing",  # a handle
        "Sardorga ayting, u biladi",  # another person's name
        "Bekzod aka yozadi",  # the owner's own alias is no counterparty
        "Salom\u202e aka",  # a hidden character
        "x" * 401,  # longer than any reply may be
        "",
    ]
    for text in admitted:
        assert style.admit_reply(text, names=names, person_id=1), text
    for text in refused:
        assert not style.admit_reply(text, names=names, person_id=1), text
    # A client line may ask the price, never carry a figure, a phone, a link,
    # another name, or come from a scammer.
    assert style.admit_context("Narxi qancha?", names=names, person_id=1)
    assert style.admit_context("Aka, salom", names=names, person_id=1)
    for text in ("Raqamim 998901234567", "t.me/cargo", "Sardor aytdi"):
        assert not style.admit_context(text, names=names, person_id=1), text
    assert not style.admit_context(
        "Salom", names=names, person_id=1, meta={"safety": {"verdict": "scam"}}
    )
    # A contact saved as «Aka» does not make «aka» a name.
    assert not names.other_named("Rahmat aka", 1)


async def test_examples_never_teach_promises_or_prices(session, styled):
    """The examples of a built profile are only the clean pairs: a reply
    that promises, names a time or a price, or names someone else, never
    becomes one, though it still counts towards the traits."""
    person, pairs = await _akmal(session)
    bad = await _dialogue(
        session,
        person,
        AKMAL_CHAT,
        [
            ("Qachon keladi?", "Ertaga tayyor bo'ladi"),
            ("Narxi qancha?", "Kilosi 5 dollar"),
            ("Kim olib keladi?", "Sardor olib keladi"),
            ("Yuborasizmi?", "Xo'p bo'ladi aka"),
        ],
        start=NOW - timedelta(hours=10),
    )
    await _person(session, "Sardor")
    result = await style.build_profile(session, now=NOW)
    assert result.status == "built"
    stored = set(
        await session.scalars(
            sa.select(m.OwnerStyleExample.interaction_id).where(
                m.OwnerStyleExample.profile_id == result.profile.id
            )
        )
    )
    assert stored == {answered.id for _, answered in pairs}
    assert not stored & {answered.id for _, answered in bad}
    assert result.profile.sample_count == len(pairs) + len(bad)
    for example in await style.examples_for(session, result.profile, limit=20):
        verdict = redlines.check_outbound(example.pair.reply, "owner")
        assert verdict.passed and not verdict.hits
        assert "Akmal" not in example.pair.reply + example.pair.context


async def test_examples_follow_personal_marks_and_edits(session, styled):
    """Examples are joined, never copied, and checked again when read: an
    edited reply that now carries a figure, a purged message, or a chat
    marked personal after the build is never shown."""
    person, pairs = await _akmal(session)
    result = await style.build_profile(session, now=NOW)
    shown = await style.examples_for(session, result.profile, limit=20)
    assert len(shown) == len(pairs)
    edited = pairs[0][1]
    edited.raw_text = "Aka, 3 kunda keladi"
    await session.flush()
    shown = await style.examples_for(session, result.profile, limit=20)
    assert len(shown) == len(pairs) - 1
    monitor = await chats.get_monitor(session, AKMAL_CHAT)
    await policy.mark_chat(session, monitor, by="command", now=NOW)
    assert await style.examples_for(session, result.profile, limit=20) == []
    # The counterparty's name never reaches an example: a placeholder stands.
    pair = style._pair("Akmal aka, salom", "Rahmat Akmal aka", ["Akmal Karimov"])
    assert "Akmal" not in pair.context + pair.reply
    assert style_prompt.PLACEHOLDER in pair.reply


# --- traits -----------------------------------------------------------------------------

TRAIT_TEXTS = [
    "Assalomu alaykum aka, ko'rib beraman",
    "Assalomu alaykum aka, ko'rib beraman 🙂",
    "Salom aka, ko'rib beraman",
    "Xo'p bo'ladi aka",
    "Xo'p bo'ladi",
    "Xo'p bo'ladi, rahmat",
    "Sizga yozaman aka",
    "Хорошо, понял",
    "Davay aka, normalno",
    "Akmal aka bilan gaplashdim",
    "Akmal aka bilan gaplashdim",
    "Akmal aka bilan gaplashdim",
]


def test_traits_deterministic():
    names = style.build_name_index([(1, "Akmal", [])])
    first = style.compute_traits(TRAIT_TEXTS, names=names)
    shuffled = TRAIT_TEXTS[:]
    random.Random(7).shuffle(shuffled)
    assert style.compute_traits(shuffled, names=names) == first
    assert first["messages"] == len(TRAIT_TEXTS)
    assert first["greetings"][0] == ["Assalomu alaykum", 2]
    assert first["honorifics"][0][0] == "aka"
    assert first["address"]["siz"] >= 1
    phrases = {row[0]: row for row in first["phrases"]}
    # The owner's habits, each saying whether it passes the red lines.
    assert phrases["ko'rib beraman"][1:] == [3, True]
    assert phrases["xo'p bo'ladi"][1:] == [3, False]
    # A phrase that names a person is never listed.
    assert not [p for p in phrases if "akmal" in p.casefold()]
    assert first["emoji"]["messages"] == 1 and first["emoji"]["level"] == "low"
    assert first["script"]["cyrillic"] + first["script"]["russian"] == 1
    assert first["russian_words_pct"] > 0
    # Only safe phrases ever reach the model.
    assert "xo'p bo'ladi" not in style_prompt.safe_phrases(first)
    assert "ko'rib beraman" in style_prompt.safe_phrases(first)


def test_fallback_prose_has_no_digit():
    traits = style.compute_traits(TRAIT_TEXTS)
    prose = style_prompt.fallback_prose(traits)
    assert prose and not any(ch.isdigit() for ch in prose)
    assert "«xo'p bo'ladi»" not in prose
    assert style_prompt.fallback_prose({}) and not any(
        ch.isdigit() for ch in style_prompt.fallback_prose({})
    )


async def test_prompt_text_digit_free(session, styled):
    """The model's prose is refused when it holds a digit (or a money word,
    or a name): the deterministic guide stands in. Usage is recorded as
    jarvis_style and priced from config."""
    await _akmal(session)
    styled.client.text = (
        '{"prompt_text": "The owner writes 5 words per message and charges 100 dollars."}'
    )
    refused = await style.build_profile(session, now=NOW)
    assert refused.profile.model == style_prompt.FALLBACK_MODEL
    assert not any(ch.isdigit() for ch in refused.profile.prompt_text)
    styled.client.text = GOOD_PROSE
    kept = await style.build_profile(session, now=NOW, force=True)
    assert kept.profile.model == "test-reason-model"
    assert kept.profile.prompt_text.startswith("The owner writes short")
    usage = list(
        await session.scalars(
            sa.select(m.UsageLog).where(m.UsageLog.operation == style.STYLE_OPERATION)
        )
    )
    assert len(usage) == 2 and all(u.cost_usd is not None for u in usage)
    # The prompt saw only scrubbed examples, never the counterparty's name.
    sent = styled.client.calls[-1]["messages"][0]["content"]
    assert "Akmal" not in sent
    # A model answer naming a person is refused too.
    styled.client.text = (
        '{"prompt_text": "The owner writes warmly to Akmal and always greets them '
        'first with a long greeting in Latin script."}'
    )
    named = await style.build_profile(session, now=NOW, force=True)
    assert named.profile.model == style_prompt.FALLBACK_MODEL


async def test_cost_cap_stops_the_prose_call(session, styled, monkeypatch):
    await _akmal(session)

    async def over(session, now):
        return True

    monkeypatch.setattr(style.costs, "over_cap", over)
    result = await style.build_profile(session, now=NOW)
    assert result.status == "built"
    assert result.profile.model == style_prompt.FALLBACK_MODEL
    assert styled.client.calls == []


# --- profiles ---------------------------------------------------------------------------


def test_style_sample_size_alone_never_fails_validation(monkeypatch):
    """Rule 20: STYLE_SAMPLE_SIZE set below the unset STYLE_MIN_SAMPLES
    default still loads, and the build's minimum is the smaller one; only a
    pair the owner set themselves is refused."""
    from miya.config import Settings

    monkeypatch.delenv("STYLE_MIN_SAMPLES", raising=False)
    monkeypatch.delenv("STYLE_SAMPLE_SIZE", raising=False)
    fresh = Settings(_env_file=None, style_sample_size=20)
    assert (fresh.style_sample_size, fresh.style_min_samples) == (20, 50)
    with pytest.raises(ValueError, match="STYLE_MIN_SAMPLES"):
        Settings(_env_file=None, style_sample_size=20, style_min_samples=30)
    monkeypatch.setattr(settings, "style_sample_size", 20)
    monkeypatch.setattr(settings, "style_min_samples", 50)
    assert style.min_samples() == 20


async def test_below_min_samples_and_unchanged_input(session, styled, monkeypatch):
    """Below STYLE_MIN_SAMPLES nothing is built; the same input twice is one
    profile; a new message makes the next build a new draft."""
    person, _ = await _akmal(session)
    monkeypatch.setattr(settings, "style_min_samples", 50)
    few = await style.build_profile(session, now=NOW)
    assert few.status == "too_few" and few.sample_count == len(CLEAN_PAIRS)
    assert await session.scalar(sa.select(sa.func.count(m.OwnerStyleProfile.id))) == 0
    monkeypatch.setattr(settings, "style_min_samples", 4)
    built = await style.build_profile(session, now=NOW)
    assert built.status == "built" and built.profile.state == "draft"
    again = await style.build_profile(session, now=NOW)
    assert again.status == "unchanged" and again.profile.id == built.profile.id
    await _msg(
        session, "Rahmat aka", at=NOW - timedelta(minutes=1), out=True, person=person
    )
    newer = await style.build_profile(session, now=NOW)
    assert newer.status == "built" and newer.profile.version == built.profile.version + 1


async def test_single_active_profile(session, styled):
    await _akmal(session)
    first = (await style.build_profile(session, now=NOW)).profile
    assert (await style.activate(session, first.version, by="button", now=NOW)).state == (
        "active"
    )
    second = (await style.build_profile(session, now=NOW, force=True)).profile
    assert (await style.current_profile(session)).id == second.id
    await style.activate(session, second.version, by="button", now=NOW)
    await session.refresh(first)
    assert first.state == "retired" and second.state == "active"
    assert second.approved_by == "button" and second.approved_at == NOW
    assert [e["new"] for e in first.history] == ["draft", "active", "retired"]
    # A retired version is a stale card; the database holds the line too.
    assert await style.activate(session, first.version, by="button", now=NOW) is None
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                sa.update(m.OwnerStyleProfile)
                .where(m.OwnerStyleProfile.id == first.id)
                .values(state="active")
            )


async def test_an_older_draft_never_replaces_a_newer_profile(session, styled):
    """Activating a version retires every older draft, so an older card's
    «✅ Faollashtirish» reads as stale; a draft older than the active one is
    refused even when it was never retired."""
    await _akmal(session)
    older = (await style.build_profile(session, now=NOW)).profile
    newer = (await style.build_profile(session, now=NOW, force=True)).profile
    assert older.state == newer.state == "draft"
    assert (await style.activate(session, newer.version, by="button", now=NOW)).id == (
        newer.id
    )
    await session.refresh(older)
    assert older.state == "retired"
    assert [e["new"] for e in older.history] == ["draft", "retired"]
    assert await style.activate(session, older.version, by="button", now=NOW) is None
    await session.refresh(newer)
    assert newer.state == "active"

    await session.execute(
        sa.update(m.OwnerStyleProfile)
        .where(m.OwnerStyleProfile.id == older.id)
        .values(state="draft")
    )
    assert await style.activate(session, older.version, by="button", now=NOW) is None
    await session.refresh(older)
    await session.refresh(newer)
    assert older.state == "draft" and newer.state == "active"


async def test_person_tone_from_history(session, styled):
    """How the owner writes to each person, from their own messages: «aka»
    and «siz» in Latin to one, «брат» and «ты» in Russian to another; a
    person with too few messages gets none."""
    akmal, _ = await _akmal(session)
    sardor = await _person(session, "Sardor", telegram_id=SARDOR_CHAT)
    await _monitor(session, SARDOR_CHAT)
    await _dialogue(
        session,
        sardor,
        SARDOR_CHAT,
        [
            ("Привет", "Привет, брат, как ты?"),
            ("Норм", "Брат, ты где сейчас?"),
            ("Дома", "Понял, брат, давай"),
        ],
    )
    rare = await _person(session, "Bobur", telegram_id=7401)
    await _monitor(session, 7401)
    await _msg(
        session, "Salom", at=NOW - timedelta(hours=2), out=True, person=rare, chat=7401
    )
    await style.build_profile(session, now=NOW)
    tone = await style.tone_for(session, akmal.id)
    assert (tone.honorific, tone.address_form, tone.script, tone.lang) == (
        "aka",
        "siz",
        "latin",
        "uz",
    )
    assert tone.greeting == "Assalomu alaykum"
    assert stalls.honorific_of(tone.honorific) == "aka"
    russian = await style.tone_for(session, sardor.id)
    assert (russian.honorific, russian.address_form, russian.script, russian.lang) == (
        "brat",
        "sen",
        "cyrillic",
        "ru",
    )
    assert await session.get(m.OwnerStylePerson, rare.id) is None
    # Unknown: the neutral form (Q2), never «aka» by default.
    assert stalls.honorific_of((await style.tone_for(session, rare.id)).honorific) == (
        "neutral"
    )


async def test_merge_recomputes_tone(session, styled):
    akmal, _ = await _akmal(session)
    twin = await _person(session, "Akmal Karimov")
    await _monitor(session, 7501)
    await _dialogue(
        session,
        twin,
        7501,
        [("Salom", "Salom aka"), ("Yaxshi", "Rahmat aka"), ("Ha", "Qalaysiz aka?")],
    )
    await style.build_profile(session, now=NOW)
    assert (await session.get(m.OwnerStylePerson, twin.id)).sample_count == 3
    twin_id, akmal_id = twin.id, akmal.id
    await people.merge_into(session, twin, akmal, by="command")
    session.expire_all()
    assert await session.get(m.OwnerStylePerson, twin_id) is None
    merged = await session.get(m.OwnerStylePerson, akmal_id)
    assert merged.sample_count == len(CLEAN_PAIRS) + 3


async def test_one_bad_row_never_stops_a_build(session, styled, monkeypatch):
    """Rule 25: an example that cannot be stored, or a person whose tone
    cannot be, is logged with its ids and skipped; the rest are stored."""
    akmal, pairs = await _akmal(session)
    sardor = await _person(session, "Sardor", telegram_id=SARDOR_CHAT)
    await _monitor(session, SARDOR_CHAT)
    await _dialogue(
        session,
        sardor,
        SARDOR_CHAT,
        [("Salom", "Salom aka"), ("Yaxshi", "Rahmat aka"), ("Ha", "Qalaysiz aka?")],
    )
    real_embed = style._embed

    async def _one_bad(texts):
        vectors = await real_embed(texts)
        vectors[0] = [1.0, 0.0]  # the wrong dimension: that row's insert fails
        return vectors

    monkeypatch.setattr(style, "_embed", _one_bad)
    real_store = style._store_tone

    async def _store(session, person_id, texts):
        if person_id == sardor.id:
            raise RuntimeError("tone write failed")
        await real_store(session, person_id, texts)

    monkeypatch.setattr(style, "_store_tone", _store)
    result = await style.build_profile(session, now=NOW)
    assert result.status == "built"
    assert result.example_count == len(pairs) + 3 - 1
    assert await session.get(m.OwnerStylePerson, akmal.id) is not None
    assert await session.get(m.OwnerStylePerson, sardor.id) is None


async def test_purge_takes_style_examples(session, styled):
    akmal, pairs = await _akmal(session)
    profile = (await style.build_profile(session, now=NOW)).profile
    profile.prompt_text = "Greets Akmal warmly."
    # A phrase that named them before they were known to MIYA.
    kept_phrases = list(profile.traits.get("phrases") or [])
    profile.traits = {
        **profile.traits,
        "phrases": [["akmalga aytaman", 9, True], *kept_phrases],
    }
    await session.flush()
    plan = await purge.plan_person(session, akmal)
    assert plan.counts["style_examples"] == len(pairs)
    assert "uslub misoli" in replies.purge_preview(plan)
    await purge.execute(session, plan)
    left = await session.scalar(
        sa.select(sa.func.count(m.OwnerStyleExample.id)).where(
            m.OwnerStyleExample.profile_id == profile.id
        )
    )
    assert left == 0
    assert await session.get(m.OwnerStylePerson, akmal.id) is None
    await session.refresh(profile)
    assert "Akmal" not in profile.prompt_text
    assert profile.traits["phrases"] == kept_phrases


# --- the owner's stall overrides --------------------------------------------------------

OVERRIDE = "Aka, savolingizni ko'rib chiqib, o'zim yozaman."


async def test_owner_stall_override_is_an_approved_stall(session, styled, monkeypatch):
    """An override the owner set that passes the stall role is an approved
    stall for the statement allowlist: outbox.create loads it from the
    active profile itself (a caller still cannot hand texts in), and the
    claim reads it again. One that fails the stall role is refused."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    redlines.install()
    akmal, pairs = await _akmal(session)
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="auto", by="command", now=NOW
    )
    trigger = await _msg(
        session, "Aka, yuk bormi?", at=NOW - timedelta(minutes=2), person=akmal
    )

    async def stall_row(body):
        return await outbox.create(
            session,
            channel="telegram",
            chat_key=f"tg:{AKMAL_CHAT}",
            origin="stall",
            authored_by="ai",
            body=body,
            now=NOW,
            intent="stall",
            person_id=akmal.id,
            trigger_interaction_id=trigger.id,
        )

    before = await stall_row(OVERRIDE)
    # A holding reply is never sent on its own (JV-23): always a draft. Before
    # the override is in force it is no approved shape either.
    assert before.state == "draft" and before.guard["shapes_ok"] is False
    assert gate.UNAPPROVED_STATEMENT in before.guard["flags"]
    assert not await style.set_stall_override(
        session,
        reason="general",
        script="latin",
        honorific="aka",
        text=OVERRIDE,
        by="command",
        now=NOW,
    ), "no active profile yet"
    profile = (await style.build_profile(session, now=NOW)).profile
    await style.activate(session, profile.version, by="button", now=NOW)
    assert not await style.set_stall_override(
        session,
        reason="price",
        script="latin",
        honorific="aka",
        text="Aka, ertaga aniq aytaman.",
        by="command",
        now=NOW,
    )
    assert await style.set_stall_override(
        session,
        reason="general",
        script="latin",
        honorific="aka",
        text=OVERRIDE,
        by="command",
        now=NOW,
    )
    assert await style.stall_texts(session) == (OVERRIDE,)
    assert (await style.stall_overrides(session)) == {"general:latin:aka": OVERRIDE}
    chosen = stalls.choose(
        ["identity"],
        script="latin",
        honorific="aka",
        overrides=await style.stall_overrides(session),
    )
    assert chosen.source == "owner" and chosen.text == OVERRIDE
    after = await stall_row(OVERRIDE + " ")
    # In force, the override is an approved stall shape; the row still waits
    # for the owner's tap, as every holding reply does (JV-23).
    assert after.guard["shapes_ok"] is True and set(after.guard["shapes"]) == {"stall"}
    assert after.state == "draft" and after.approved_by is None
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    with pytest.raises(outbox.OutboxError, match=outbox.APPROVED_TEXTS_REFUSED):
        await outbox.create(
            session,
            channel="telegram",
            chat_key=f"tg:{AKMAL_CHAT}",
            origin="stall",
            authored_by="ai",
            body="Boshqa matn.",
            now=NOW,
            intent="stall",
            person_id=akmal.id,
            trigger_interaction_id=trigger.id,
            stall_texts=("Boshqa matn.",),
        )
    # A new profile carries the override over.
    newer = (await style.build_profile(session, now=NOW, force=True)).profile
    assert newer.stalls == {"general:latin:aka": OVERRIDE}
    # An edit on the active profile after the draft was built is what the
    # activated draft keeps, not the draft's copy from build time.
    changed = "Aka, savolingizni ko'rib chiqib, keyin o'zim yozaman."
    assert await style.set_stall_override(
        session,
        reason="general",
        script="latin",
        honorific="aka",
        text=changed,
        by="command",
        now=NOW,
    )
    await style.activate(session, newer.version, by="button", now=NOW)
    assert newer.state == "active"
    assert newer.stalls == {"general:latin:aka": changed}
    assert await style.stall_texts(session) == (changed,)


# --- the reply drafter's view -----------------------------------------------------------


async def test_for_reply_reads_the_active_profile(session, styled):
    akmal, _ = await _akmal(session)
    assert await style.for_reply(session, person_id=akmal.id, text="Salom") is None
    profile = (await style.build_profile(session, now=NOW)).profile
    await style.activate(session, profile.version, by="button", now=NOW)
    reply_style = await style.for_reply(
        session, person_id=akmal.id, text="Assalomu alaykum, yuk bormi?"
    )
    assert reply_style.prompt_text == profile.prompt_text
    assert 0 < len(reply_style.examples) <= style.EXAMPLES_PER_PROMPT
    block = style_prompt.render(reply_style)
    assert "Call this person «aka»." in block and "«siz»" in block
    assert "Akmal" not in block
    assert style_prompt.UNTRUSTED_OPEN in block


# --- the owner's surface ----------------------------------------------------------------


class _Message:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message()

    async def answer(self, *args, **kwargs):
        return None


@pytest.fixture
def bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def _uslub(args=None):
    message = _Message()
    await handlers.cmd_style(
        message, CommandObject(prefix="/", command="uslub", args=args)
    )
    [(text, keyboard)] = message.sent
    return text, keyboard


async def test_style_command_card_and_buttons(bound, styled):
    session = bound
    await _akmal(session)
    text, keyboard = await _uslub()
    assert text.startswith(f"🗣 Uslubingiz ({len(CLEAN_PAIRS)} ta xabardan):\n")
    assert "• Salomlashish: «Assalomu alaykum»" in text
    assert "• Murojaat: aka, siz" in text
    assert "• Emoji: o'rtacha" in text
    assert "• Yozuv: lotin 100% · kirill 0% · rus so'zlari 0%" in text
    assert "Misollar:\n• «" in text and "Akmal" not in text
    assert text.endswith("Versiya: v1 · qoralama — faollashtirilmagan")
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [(b.text, b.callback_data) for b in buttons] == [
        ("✅ Faollashtirish", "js:y:1"),
        ("🔄 Qayta tuzish", "js:r:1"),
    ]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert keyboards.STYLE_PREFIX not in handlers._button_handlers()
    tap = _Callback("js:y:1")
    await handlers.on_style_button(tap)
    assert tap.message.sent[0][0] == "✅ Uslub v1 faollashtirildi."
    text, keyboard = await _uslub()
    assert text.endswith("Versiya: v1 · faol")
    assert [b.text for row in keyboard.inline_keyboard for b in row] == [
        "🔄 Qayta tuzish"
    ]
    rebuilt = _Callback("js:r:1")
    await handlers.on_style_button(rebuilt)
    assert rebuilt.message.sent[0][0].endswith(
        "Versiya: v2 · qoralama — faollashtirilmagan"
    )
    await handlers.on_style_button(_Callback("js:y:2"))
    stale = _Callback("js:y:1")
    await handlers.on_style_button(stale)
    assert stale.message.sent[0][0] == replies.STYLE_STALE
    assert (await _uslub("nimadir"))[0] == replies.STYLE_USAGE


async def test_style_command_too_few_and_import_request(bound, styled, monkeypatch):
    session = bound
    monkeypatch.setattr(settings, "style_min_samples", 50)
    await _akmal(session)
    await _monitor(session, -7301, chat_type=ChatType.group)
    text, keyboard = await _uslub()
    assert text == replies.style_too_few(len(CLEAN_PAIRS), 50) and keyboard is None
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    text, _ = await _uslub("yukla")
    assert text == (
        "📥 Uslub uchun eski yozishmalaringizni yuklayapman. Tayyor bo'lganda yozaman."
    )
    # Without Jarvis no «✅ Uslub tayyor» is ever sent, so none is promised.
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    text, _ = await _uslub("yukla")
    assert text == (
        "📥 Uslub uchun eski yozishmalaringizni yuklayapman. Bir necha daqiqadan "
        "so'ng /uslub ni oching."
    )
    requested = {
        mon.tg_chat_id
        for mon in await session.scalars(
            sa.select(m.ChatMonitor).where(
                m.ChatMonitor.style_import_requested_at.isnot(None)
            )
        )
    }
    assert requested == {AKMAL_CHAT}


async def test_style_refresh_job_tells_the_owner_once(session, styled, monkeypatch):
    """Once every chat of the import is read the profile is built and the
    owner is told once; while a chat is still waiting nothing happens."""
    from miya.worker import main as worker

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(worker, "session_scope", _scope)
    told: list[str] = []

    async def _notify(bot, text, **kwargs):
        told.append(text)
        return True

    monkeypatch.setattr(worker, "notify", _notify)
    await _akmal(session)
    assert await chats.request_style_import(session, now=NOW) == 1
    await worker.style_refresh_job(None, now=NOW)
    assert told == []  # the userbot has not read the chat yet
    [monitor] = await chats.pending_style_imports(session)
    chats.mark_style_imported(monitor, now=NOW)
    await session.flush()
    await worker.style_refresh_job(None, now=NOW)
    n = len(CLEAN_PAIRS)
    assert told == [f"✅ Uslub tayyor: {n} ta xabar, {n} ta misol. /uslub"]
    await worker.style_refresh_job(None, now=NOW)
    assert len(told) == 1


# --- the schema ------------------------------------------------------------------------


def test_check_lists_match_the_migration():
    path = ROOT / "alembic" / "versions" / "0026_owner_style.py"
    spec = importlib.util.spec_from_file_location("migration_0026", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PROFILE_STATES == m.STYLE_PROFILE_STATES
    assert module.EXAMPLE_SOURCES == m.STYLE_EXAMPLE_SOURCES
    assert module.ADDRESS_FORMS == m.STYLE_ADDRESS_FORMS
    assert module.PERSON_SCRIPTS == m.STYLE_PERSON_SCRIPTS
    assert module.PERSON_LANGS == m.STYLE_PERSON_LANGS
    assert settings.embed_dim == module.EMBED_DIM
    assert module.down_revision == "0025_jarvis_core"
    assert len(module.revision) <= 32
