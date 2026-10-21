"""JV-15: the business knowledge base — general answers from owner-approved facts.

An entry is active only by the owner's own act (``/bilim qo'sh``, or a tap
on a suggestion MIYA mined from their own answers); it never holds a price,
a deadline, a promise or a client's personal data; retrieval is hybrid with
KB_MATCH_THRESHOLD; and the answers of the active entries are the ``kb``
shape of the statement allowlist, which the outbox reads itself. No network:
the model and the embedder are fakes.
"""

from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiogram.filters import CommandObject
from dotenv import dotenv_values

from miya.bot import handlers, keyboards, replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.jarvis import chatkey, gate, kb, outbox, policy, redlines
from miya.services import purge
from miya.services.embeddings import EmbeddingError
from miya.tools import doctor

TZ = settings.tz
NOW = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = 900015
AKMAL_CHAT, SARDOR_CHAT, KARIM_CHAT = 7151, 7152, 7153
KEY = chatkey.telegram(AKMAL_CHAT)
_ids = itertools.count(5000)

OFFICE_Q = "Ofisingiz qayerda joylashgan?"
OFFICE_A = "Ofisimiz Toshkentda joylashgan."


class FakeEmbedder:
    """A vector per topic word: texts that share a topic point the same way,
    every other text gets its own axis. ``fail`` makes it unavailable."""

    def __init__(self, topics=None) -> None:
        self.topics = topics or {}
        self.fail = False
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        if self.fail:
            raise EmbeddingError("embedder down")
        self.calls.append(list(texts))
        out = []
        for text in texts:
            vector = [0.0] * settings.embed_dim
            axis = next(
                (a for word, a in self.topics.items() if word in text.casefold()),
                100 + sum(map(ord, text)) % (settings.embed_dim - 100),
            )
            vector[axis] = 1.0
            out.append(vector)
        return out


class FakeClient:
    """The model client: answers in order (an exception is raised), every
    call recorded."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.messages = self

    def with_options(self, **kwargs):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if self.answers else '{"skip": true}'
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=answer)],
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
        )


@pytest.fixture
def known(monkeypatch):
    """The owner's id, no aliases, a fake embedder and model, priced calls."""
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "owner_aliases", "")
    monkeypatch.setattr(settings, "extract_model", "test-extract-model")
    monkeypatch.setattr(settings, "extract_model_price", "1.00,5.00")
    monkeypatch.setattr(settings, "kb_match_threshold", 0.75)
    embedder = FakeEmbedder({"joylash": 1, "manzil": 1, "hujjat": 2})
    monkeypatch.setattr(kb, "get_embedder", lambda: embedder)
    client = FakeClient()
    monkeypatch.setattr(kb, "get_client", lambda: client)
    return SimpleNamespace(embedder=embedder, client=client)


@pytest.fixture
def bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


@pytest.fixture
def real_guard(monkeypatch):
    """Jarvis on at the auto ceiling with the JV-13 red-line guard."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    previous = gate.registered_guard()
    redlines.install()
    yield redlines.GUARD
    gate.register_guard(previous)


class _Message:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


class _CardMessage(_Message):
    def __init__(self) -> None:
        super().__init__()
        self.edited: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edited.append(text)


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _CardMessage()

    async def answer(self, *args, **kwargs):
        return None


async def _bilim(args=None) -> list[tuple[str, object]]:
    message = _Message()
    await handlers.cmd_knowledge(
        message, CommandObject(prefix="/", command="bilim", args=args)
    )
    return message.sent


async def _one(args=None) -> str:
    [(text, _keyboard)] = await _bilim(args)
    return text


async def _person(session, name, *, telegram_id=None) -> m.Person:
    person = m.Person(display_name=name, aliases=[], telegram_id=telegram_id)
    session.add(person)
    await session.flush()
    return person


async def _monitor(session, chat, *, title="Chat") -> m.ChatMonitor:
    monitor = m.ChatMonitor(
        tg_chat_id=chat, chat_type=ChatType.private, title=title, monitor_enabled=True
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def _msg(session, text, *, at, out=False, person=None, chat=AKMAL_CHAT):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    return row


async def _add(session, question, answer) -> m.KnowledgeEntry:
    result = await kb.add(
        session, question=question, answer=answer, by="command", now=NOW
    )
    assert result.stored, result.refusal
    return result.entry


async def _entries(session, state=None) -> list[m.KnowledgeEntry]:
    query = sa.select(m.KnowledgeEntry).order_by(m.KnowledgeEntry.id)
    if state is not None:
        query = query.where(m.KnowledgeEntry.state == state)
    return list(await session.scalars(query.execution_options(populate_existing=True)))


# --- the writer ------------------------------------------------------------------------


async def test_writer_refuses_red_lines(bound, known):
    """The answer is read in the ``kb`` role: a price, discount, payment
    term, payment confirmation, deadline, promise or guarantee is refused
    with the plan's string and nothing is stored; office hours may stay."""
    session = bound
    refused = {
        "Bir kilosi 5 dollar.": "price",
        "Doimiy mijozlarga chegirma bor.": "discount",
        "Oldindan to'lov 50%.": "payment_terms",
        "Pul tushdi.": "payment_claim",
        "Yetkazish muddati 15 kun.": "deadline",
        "Yuk 10 kunda keladi.": "deadline",
        "Kafolat beramiz.": "guarantee",
        "Albatta yetkazamiz.": "guarantee",
        "Va'da beramiz.": "promise",
    }
    for answer, flag in refused.items():
        result = await kb.add(
            session, question="Savol?", answer=answer, by="command", now=NOW
        )
        assert result.entry is None and result.refusal is not None, answer
        assert result.refusal.reason == "red_line", (answer, result.refusal)
        assert flag in result.refusal.flags, (answer, result.refusal)
    assert await _entries(session) == []
    text = await _one("qo'sh Narxi qancha? | Bir kilosi 5 dollar.")
    assert text == (
        "⛔ Qo'shilmadi: javobda narx bor. Bunday savollarga MIYA doim sizga "
        "yo'naltiradi."
    )
    text = await _one("qo'sh Qachon keladi? | Yuk 10 kunda keladi.")
    assert text.startswith("⛔ Qo'shilmadi: javobda va'da, muddat bor.")
    assert await _entries(session) == []
    # The owner check of the plan: an office-hours fact works.
    hours = "Ofisimiz dushanbadan shanbagacha soat 9 dan 18 gacha ishlaydi."
    text = await _one(f"qo'sh Ish vaqtingiz qanday? | {hours}")
    [entry] = await _entries(session)
    assert text == f"✅ Qo'shildi: b{entry.id} — Ish vaqtingiz qanday?"
    assert (entry.kind, entry.state, entry.source) == ("faq", "active", "owner")
    assert entry.answer == hours and entry.guard["verdict"] == "pass"
    assert entry.guard["role"] == "kb"
    assert entry.history[0]["by"] == "command"
    assert entry.search_norm and entry.embedding is not None
    # Without «|», a fact.
    text = await _one(f"qo'sh {OFFICE_A}")
    fact = (await _entries(session))[-1]
    assert (fact.kind, fact.question) == ("fact", None)
    assert text == f"✅ Qo'shildi: b{fact.id} — {OFFICE_A}"


async def test_writer_refuses_a_clients_personal_data(bound, known, monkeypatch):
    """No phone or card number, e-mail, handle, client code, waybill or
    known person's name in any field; the owner's own name may stay."""
    session = bound
    monkeypatch.setattr(settings, "owner_aliases", "Bekzod")
    await _person(session, "Akmal Karimov", telegram_id=AKMAL_CHAT)
    owner = await _person(session, "Bekzod", telegram_id=OWNER_ID)
    cases = {
        "Qo'ng'iroq qiling: +998 90 123 45 67": "phone",
        "Karta 8600 1234 5678 9012 ga.": "phone",
        "Yozing: info@example.uz": "email",
        "Telegramda @cargo_desk ga yozing.": "handle",
        "GS367 yuki omborda.": "code",
        "YW26-004715 yuk xati shu.": "code",
        "Akmal yukni oladi.": "name",
    }
    for answer, kind in cases.items():
        result = await kb.add(
            session, question=None, answer=answer, by="command", now=NOW
        )
        assert result.refusal is not None, answer
        assert result.refusal.reason == "private", (answer, result.refusal)
        assert result.refusal.flags == (kind,), (answer, result.refusal)
    # A name in the question is refused too.
    result = await kb.add(
        session, question="Akmal qayerda?", answer=OFFICE_A, by="command", now=NOW
    )
    assert result.refusal is not None and result.refusal.flags == ("name",)
    phone = await _one("qo'sh Raqamingiz? | Qo'ng'iroq qiling: +998 90 123 45 67")
    assert phone.startswith("⛔ Qo'shilmadi: matnda telefon yoki karta raqami bor.")
    text = await _one("qo'sh Akmal qayerda? | Ofisimiz Toshkentda joylashgan.")
    assert text == (
        "⛔ Qo'shilmadi: matnda odam ismi bor. Mijozlarning shaxsiy ma'lumotlari "
        "bilimlar bazasiga yozilmaydi."
    )
    assert await _entries(session) == []
    # The owner's own name is no client's.
    assert owner.telegram_id == OWNER_ID
    entry = await _add(session, None, "Bekzod aka ofis rahbari.")
    assert entry.state == "active"
    too_long = await _one("qo'sh " + "Juda uzun javob. " * 80)
    assert too_long == "⛔ Qo'shilmadi: matn juda uzun (ko'pi bilan 1000 belgi)."


async def test_bilim_list_edit_and_usage(bound, known):
    session = bound
    assert await _one() == (
        "📚 Bilimlar bazasi bo'sh. Qo'shish: <code>/bilim qo'sh Savol | Javob</code>"
    )
    entry = await _add(session, OFFICE_Q, OFFICE_A)
    fact = await _add(session, None, "Biz <b>Xitoydan</b> yuk olib kelamiz.")
    text = await _one()
    assert text.splitlines() == [
        "📚 Bilimlar bazasi (2 ta faol):",
        f"<code>b{entry.id}</code> {OFFICE_Q} — {OFFICE_A}",
        # The owner's text is escaped like any shown value.
        f"<code>b{fact.id}</code> Biz &lt;b&gt;Xitoydan&lt;/b&gt; yuk olib kelamiz.",
    ]
    text = await _one(f"tuzat b{entry.id} Ofis manzili qayerda? | {OFFICE_A}")
    assert text == f"✏️ b{entry.id} yangilandi: Ofis manzili qayerda?"
    edited = (await _entries(session))[0]
    assert edited.question == "Ofis manzili qayerda?" and edited.state == "active"
    assert edited.history[-1]["field"] == "answer"
    refused = await _one(f"tuzat b{entry.id} Narx? | Bir kilosi 5 dollar.")
    assert refused.startswith("⛔ Qo'shilmadi: javobda narx bor.")
    assert (await _entries(session))[0].answer == OFFICE_A
    assert await _one("tuzat b999999 Savol | Javob") == replies.kb_not_found(999999)
    # The rest may start on the next line.
    text = await _one(f"qo'sh\nIsh vaqti? | {OFFICE_A}")
    added = (await _entries(session))[-1]
    assert text == f"✅ Qo'shildi: b{added.id} — Ish vaqti?"
    text = await _one(f"tuzat\nb{added.id}\nIsh vaqtingiz? | {OFFICE_A}")
    assert text == f"✏️ b{added.id} yangilandi: Ish vaqtingiz?"
    for args in ("nimadir", "qo'sh", "tuzat b1", "o'chir 12", "o'chir"):
        assert await _one(args) == replies.KB_USAGE, args
    assert await _one("taklif") == "💡 Hozircha bilim takliflari yo'q."


async def test_bilim_contains_a_failure(bound, known, monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(kb, "add", broken)
    with caplog.at_level(logging.ERROR, logger="miya.bot.handlers"):
        assert await _one(f"qo'sh {OFFICE_Q} | {OFFICE_A}") == replies.JARVIS_FAILED
    [record] = [r for r in caplog.records if r.getMessage() == "/bilim failed"]
    assert record.error == "RuntimeError" and record.command == "qosh"


# --- retrieval -------------------------------------------------------------------------


async def test_retrieval_threshold(session, known, monkeypatch):
    """Hybrid: an entry matches by meaning or by words at KB_MATCH_THRESHOLD;
    a weaker one does not; at most three, best first."""
    office = await _add(session, OFFICE_Q, OFFICE_A)
    papers = await _add(
        session, "Qanday hujjatlar kerak?", "Yuk uchun invoys va qadoq varag'i kerak."
    )
    # Words: «ofis» and «joylashgan» are both in the entry's question.
    [hit] = await kb.retrieve(session, "Ofis qayerda joylashgan?")
    assert hit.id == office.id and hit.answer == OFFICE_A
    assert hit.lexical == 1.0 and hit.score >= settings.kb_match_threshold
    # Meaning: no shared word, the same topic.
    [hit] = await kb.retrieve(session, "Manzilingizni ayting")
    assert hit.id == office.id and hit.semantic == 1.0 and hit.lexical == 0.0
    # Half the words and an unrelated meaning: below the threshold.
    assert await kb.retrieve(session, "Ofis ishlaydimi?") == []
    monkeypatch.setattr(settings, "kb_match_threshold", 0.5)
    assert [h.id for h in await kb.retrieve(session, "Ofis ishlaydimi?")] == [office.id]
    monkeypatch.setattr(settings, "kb_match_threshold", 0.75)
    assert [h.id for h in await kb.retrieve(session, "Hujjatlar qanday?")] == [papers.id]
    assert await kb.retrieve(session, "Salom, ishlar qalay?") == []
    # At most three, best first.
    more = [
        await _add(session, f"Ofisingiz qayerda {word}?", OFFICE_A)
        for word in ("aka", "uka", "opa")
    ]
    hits = await kb.retrieve(session, "Ofisingiz qayerda?")
    assert len(hits) == kb.MAX_MATCHES == 3
    assert [h.id for h in hits] == sorted(h.id for h in hits)
    assert {h.id for h in hits} <= {office.id, *(e.id for e in more)}
    # Best first by score, not by id: «aka» holds both words, the rest half.
    partial = await _add(session, "Ofisingiz bormi?", OFFICE_A)
    every = await kb.retrieve(session, "Ofisingiz qayerda aka?", limit=10, threshold=0.5)
    assert [(h.id, h.score) for h in every] == [
        (more[0].id, 1.0),
        *((e.id, 0.5) for e in (office, more[1], more[2], partial)),
    ]
    [best] = await kb.retrieve(session, "Ofisingiz qayerda aka?", limit=1, threshold=0.5)
    assert best.id == more[0].id
    # The embedder down: the words still decide.
    known.embedder.fail = True
    [hit] = await kb.retrieve(session, "Hujjatlar qanday?", limit=1)
    assert hit.id == papers.id and hit.semantic is None


async def test_a_short_word_matches_only_itself(session, known):
    """«Да» or «Ha» alone is no question about office hours: a word under
    PREFIX_LEN letters never matches a longer one by its first letters."""
    hours = await _add(session, None, "Ofisimiz har kuni soat 9 dan 18 gacha ishlaymiz.")
    for text in ("Да", "Ha", "da", "har"):
        hits = await kb.retrieve(session, text, threshold=0.5)
        assert [h.id for h in hits] == ([hours.id] if text == "har" else []), text
    assert kb.lexical_score(["da"], "soat 9 dan") == 0.0
    assert kb.lexical_score(["ofis"], "ofisingiz bormi") == 1.0
    assert kb.lexical_score(["ofisingiz"], "ofis manzili") == 1.0


async def test_retrieval_never_raises(session, known, monkeypatch, caplog):
    await _add(session, OFFICE_Q, OFFICE_A)

    async def broken(*args, **kwargs):
        raise RuntimeError("index gone")

    monkeypatch.setattr(kb, "_retrieve", broken)
    with caplog.at_level(logging.ERROR, logger="miya.jarvis.kb"):
        assert await kb.retrieve(session, OFFICE_Q) == []
    assert any(r.getMessage() == "knowledge retrieval failed" for r in caplog.records)
    # The session is still usable.
    assert len(await _entries(session)) == 1


async def test_retired_not_retrieved(bound, known):
    session = bound
    entry = await _add(session, OFFICE_Q, OFFICE_A)
    assert [h.id for h in await kb.retrieve(session, OFFICE_Q)] == [entry.id]
    assert await kb.active_texts(session) == {entry.id: OFFICE_A}
    assert await _one(f"o'chir b{entry.id}") == f"🗑 b{entry.id} o'chirildi."
    [retired] = await _entries(session)
    assert retired.state == "retired" and retired.history[-1]["new"] == "retired"
    assert await kb.retrieve(session, OFFICE_Q) == []
    assert await kb.active_texts(session) == {}
    assert await _one() == replies.KB_EMPTY
    assert await _one(f"o'chir b{entry.id}") == replies.kb_not_found(entry.id)
    assert await _one(f"tuzat b{entry.id} {OFFICE_Q} | {OFFICE_A}") == (
        replies.kb_not_found(entry.id)
    )


async def test_an_entry_failing_the_checker_now_is_never_read(session, known):
    """Every reader checks the text again: an entry stored before a rule
    existed (here: written straight into the table) is never an answer."""
    entry = await _add(session, OFFICE_Q, OFFICE_A)
    await session.execute(
        sa.update(m.KnowledgeEntry)
        .where(m.KnowledgeEntry.id == entry.id)
        .values(answer="Ofisimiz Toshkentda, bir kilosi 5 dollar.")
    )
    assert await kb.retrieve(session, OFFICE_Q) == []
    assert await kb.active_texts(session) == {}


# --- suggestions -----------------------------------------------------------------------


async def _asked_twice(session, *, answer_one=None, answer_two=None):
    """Two clients ask where the office is; the owner answers both."""
    akmal = await _person(session, "Akmal", telegram_id=AKMAL_CHAT)
    sardor = await _person(session, "Sardor", telegram_id=SARDOR_CHAT)
    await _monitor(session, AKMAL_CHAT, title="Akmal")
    await _monitor(session, SARDOR_CHAT, title="Sardor")
    at = NOW - timedelta(days=2)
    pairs = []
    for person, chat, answer in (
        (akmal, AKMAL_CHAT, answer_one or "Ofisimiz Toshkentda, aka."),
        (sardor, SARDOR_CHAT, answer_two or "Toshkentda, ofisimiz bor."),
    ):
        asked = await _msg(session, "Ofisingiz qayerda?", at=at, person=person, chat=chat)
        answered = await _msg(
            session,
            answer,
            at=at + timedelta(minutes=1),
            out=True,
            person=person,
            chat=chat,
        )
        pairs.append((asked, answered))
        at += timedelta(hours=1)
    return akmal, sardor, pairs


SUGGESTION = (
    '{"question": "Ofisingiz qayerda?", "answer": "Ofisimiz Toshkentda joylashgan."}'
)


async def test_suggestion_needs_approval(bound, known):
    """A mined entry is a suggestion: never retrieved and never an approved
    text before the owner's «✅ Qo'shish»; «✖️ Kerak emas» drops it and it
    is never mined again; the kb: buttons are not typed answers."""
    session = bound
    _akmal, _sardor, pairs = await _asked_twice(session)
    known.client.answers = [SUGGESTION]
    result = await kb.mine(session, now=NOW)
    assert (result.suggested, result.clusters, result.failed) == (1, 1, 0)
    [call] = known.client.calls
    assert call["model"] == "test-extract-model"
    assert "Ofisingiz qayerda?" in call["messages"][0]["content"]
    [entry] = await _entries(session)
    assert (entry.state, entry.source, entry.kind) == ("suggested", "suggested", "faq")
    assert entry.tags == ["mined"] and entry.history[0]["by"] == "auto:mining"
    assert entry.source_interaction_id in {answered.id for _a, answered in pairs}
    usage = await session.scalar(
        sa.select(m.UsageLog).where(m.UsageLog.operation == "jarvis_kb")
    )
    assert usage is not None and usage.cost_usd is not None
    # Not an answer yet.
    assert await kb.retrieve(session, "Ofisingiz qayerda?") == []
    assert await kb.active_texts(session) == {}
    assert await _one() == replies.KB_EMPTY
    [(card, keyboard)] = await _bilim("taklif")
    assert card == (
        f"💡 Taklif b{entry.id}:\nOfisingiz qayerda?\n— Ofisimiz Toshkentda joylashgan."
    )
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [(b.text, b.callback_data) for b in buttons] == [
        ("✅ Qo'shish", f"kb:y:{entry.id}"),
        ("✖️ Kerak emas", f"kb:n:{entry.id}"),
    ]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert keyboards.KB_PREFIX not in handlers._button_handlers()
    tap = _Callback(f"kb:y:{entry.id}")
    await handlers.on_knowledge_button(tap)
    assert tap.message.edited == [f"✅ Qo'shildi: b{entry.id} — Ofisingiz qayerda?"]
    [approved] = await _entries(session)
    assert approved.state == "active" and approved.history[-1]["by"] == "button"
    assert [h.id for h in await kb.retrieve(session, "Ofisingiz qayerda?")] == [entry.id]
    assert await kb.active_texts(session) == {entry.id: "Ofisimiz Toshkentda joylashgan."}
    again = _Callback(f"kb:y:{entry.id}")
    await handlers.on_knowledge_button(again)
    assert again.message.edited == [replies.KB_STALE]
    # The same cluster is never mined twice.
    known.client.answers = [SUGGESTION]
    assert (await kb.mine(session, now=NOW)).suggested == 0
    assert len(known.client.calls) == 1


async def test_a_dismissed_suggestion_is_never_mined_again(bound, known):
    session = bound
    await _asked_twice(session)
    known.client.answers = [SUGGESTION]
    await kb.mine(session, now=NOW)
    [entry] = await _entries(session)
    tap = _Callback(f"kb:n:{entry.id}")
    await handlers.on_knowledge_button(tap)
    assert tap.message.edited == [f"✖️ b{entry.id} taklifi olib tashlandi."]
    assert (await _entries(session))[0].state == "retired"
    assert await kb.retrieve(session, "Ofisingiz qayerda?") == []
    assert await _one("taklif") == replies.KB_NO_SUGGESTIONS
    # The model would write it again: the cluster is skipped before any call.
    known.client.answers = [SUGGESTION]
    assert (await kb.mine(session, now=NOW)).suggested == 0
    assert len(known.client.calls) == 1
    # A bad payload is acknowledged and changes nothing.
    for data in ("kb:x:1", "kb:y:abc", "kb:y"):
        await handlers.on_knowledge_button(_Callback(data))
    assert (await _entries(session))[0].state == "retired"


def test_mining_pairs_skip_red_lines_and_private_data():
    names = SimpleNamespace(other_named=lambda text, person_id: "Sardor" in text)

    def pair(question, answer, n=1):
        return SimpleNamespace(
            interaction_id=n,
            person_id=1,
            tg_chat_id=7000 + n,
            text=answer,
            context_id=100 + n,
            context_text=question,
            context_meta=None,
        )

    samples = [
        pair("Ofisingiz qayerda?", "Ofisimiz Toshkentda, aka.", 1),
        # A price, a deadline or an identity question always goes to the owner.
        pair("Narxi qancha?", "Kelishamiz, aka.", 2),
        pair("Qachon keladi?", "Tez orada, aka.", 3),
        pair("Siz botmisiz?", "Yo'q, aka.", 4),
        # No question asked.
        pair("Rahmat", "Arzimaydi.", 5),
        # Private data or another person on either side.
        pair("Ofisingiz qayerda?", "Qo'ng'iroq qiling 901234567.", 6),
        pair("Ofisingiz qayerda?", "Sardordan so'rang.", 7),
        pair("Raqamingiz nima? 901234567", "Ofisda, aka.", 8),
        # The owner's answer itself carries a red line.
        pair("Yuk qanday keladi?", "Ertaga albatta yetkazib beramiz.", 9),
    ]
    pairs = kb.mining_pairs(samples, names)
    assert [p.interaction_id for p in pairs] == [1]


def test_parse_suggestion_refuses_what_an_entry_may_not_hold():
    names = re.compile(r"(?<![a-z0-9])akmal(?![a-z0-9])")
    good = kb.parse_suggestion(SUGGESTION, names=names)
    assert good == ("Ofisingiz qayerda?", "Ofisimiz Toshkentda joylashgan.")
    assert kb.parse_suggestion(f"```json\n{SUGGESTION}\n```", names=names) == good
    for reply in (
        '{"skip": true}',
        "not json",
        "[]",
        '{"question": "Ish vaqti?", "answer": "Soat 9 dan ishlaymiz."}',
        '{"question": "Narx?", "answer": "Bir kilosi besh dollar."}',
        '{"question": "Kim?", "answer": "Akmal aka javob beradi."}',
        '{"question": "Sayt?", "answer": "example.uz saytida bor."}',
        '{"question": "Savol?", "answer": "Javob​ bor."}',
        '{"question": "Savol?", "answer": ""}',
        '{"question": 5, "answer": "Bor."}',
    ):
        assert kb.parse_suggestion(reply, names=names) is None, reply


async def test_mining_failures_are_contained(bound, known, caplog):
    """One cluster whose model call fails is logged with its ids; the next
    cluster still gets its suggestion; a refused answer stores nothing."""
    session = bound
    await _asked_twice(session)
    karim = await _person(session, "Karim", telegram_id=KARIM_CHAT)
    await _monitor(session, KARIM_CHAT, title="Karim")
    for chat, person in ((KARIM_CHAT, karim), (AKMAL_CHAT, None)):
        at = NOW - timedelta(hours=3) if chat == KARIM_CHAT else NOW - timedelta(hours=2)
        await _msg(session, "Hujjatlar kerakmi?", at=at, person=person, chat=chat)
        await _msg(
            session,
            "Invoys kerak, aka.",
            at=at + timedelta(minutes=1),
            out=True,
            person=person,
            chat=chat,
        )
    known.client.answers = [
        TimeoutError(),
        '{"question": "Qanday hujjatlar kerak?", "answer": "Invoys kerak."}',
    ]
    with caplog.at_level(logging.WARNING, logger="miya.jarvis.kb"):
        result = await kb.mine(session, now=NOW)
    assert (result.suggested, result.clusters, result.failed) == (1, 2, 1)
    [failure] = [
        r for r in caplog.records if r.getMessage() == "knowledge suggestion unavailable"
    ]
    assert failure.error == "TimeoutError" and failure.interaction_ids
    [entry] = await _entries(session)
    assert entry.question in ("Ofisingiz qayerda?", "Qanday hujjatlar kerak?")
    # A refused model answer stores nothing.
    for row in await _entries(session):
        await session.delete(row)
    await session.flush()
    known.client.answers = [
        '{"question": "Narx?", "answer": "Bir kilosi besh dollar."}',
        '{"skip": true}',
    ]
    assert (await kb.mine(session, now=NOW)).suggested == 0
    assert await _entries(session) == []


async def test_mining_stops_at_the_cost_cap(bound, known, monkeypatch):
    session = bound
    await _asked_twice(session)

    async def over(session, now):
        return True

    async def never(*args, **kwargs):
        raise AssertionError("sampled over the cost cap")

    monkeypatch.setattr(kb.costs, "over_cap", over)
    monkeypatch.setattr(kb.style, "sample", never)
    result = await kb.mine(session, now=NOW)
    assert (result.suggested, result.clusters, result.stopped) == (0, 0, "cost_cap")
    assert known.client.calls == [] and known.embedder.calls == []


async def test_kb_mining_job_tells_the_owner_once_a_week(session, known, monkeypatch):
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
    await _asked_twice(session)
    pending = await _add(session, OFFICE_Q, OFFICE_A)
    await session.execute(
        sa.update(m.KnowledgeEntry)
        .where(m.KnowledgeEntry.id == pending.id)
        .values(embedding=None)
    )
    await session.execute(
        sa.update(m.KnowledgeEntry)
        .where(m.KnowledgeEntry.id == pending.id)
        .values(question="Ofis manzili qayerda?")
    )
    known.client.answers = [SUGGESTION]
    await worker.kb_mining_job(None, now=NOW)
    assert told == ["💡 Javoblaringizdan 1 ta bilim taklif qilindi: /bilim taklif"]
    embedded = await session.scalar(
        sa.select(m.KnowledgeEntry.embedding.isnot(None)).where(
            m.KnowledgeEntry.id == pending.id
        )
    )
    assert embedded
    runs = (
        sa.select(sa.func.count())
        .select_from(m.ReminderLog)
        .where(m.ReminderLog.kind == kb.MINING_KIND)
    )
    await worker.kb_mining_job(None, now=NOW + timedelta(hours=1))
    await worker.kb_mining_job(None, now=NOW + timedelta(days=3))
    assert len(told) == 1 and len(known.client.calls) == 1
    assert await session.scalar(runs) == 1
    # A week later it runs again (nothing new to suggest: no notice).
    await worker.kb_mining_job(None, now=NOW + timedelta(days=8))
    assert len(told) == 1
    assert await session.scalar(runs) == 2


async def test_kb_mining_job_is_contained(session, known, monkeypatch, caplog):
    from miya.worker import main as worker

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(worker, "session_scope", _scope)

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(kb, "embed_pending", broken)
    monkeypatch.setattr(kb, "mine", broken)
    with caplog.at_level(logging.ERROR, logger="miya.worker.main"):
        await worker.kb_mining_job(None, now=NOW)
    messages = {r.getMessage() for r in caplog.records}
    assert {"knowledge entries not embedded", "knowledge mining failed"} <= messages


# --- the statement allowlist -----------------------------------------------------------


async def _client_question(session, text="Ofisingiz qayerda?"):
    person = await _person(session, "Akmal", telegram_id=AKMAL_CHAT)
    await _monitor(session, AKMAL_CHAT, title="Akmal")
    trigger = await _msg(
        session, text, at=NOW - timedelta(minutes=5), person=person, chat=AKMAL_CHAT
    )
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="auto", by="command", now=NOW
    )
    return person, trigger


async def _kb_row(session, person, trigger, body, **kw):
    return await outbox.create(
        session,
        channel="telegram",
        chat_key=KEY,
        origin="auto_reply",
        authored_by="ai",
        body=body,
        now=NOW,
        intent="general_question",
        person_id=person.id,
        trigger_interaction_id=trigger.id,
        **kw,
    )


async def test_kb_text_is_an_approved_shape(session, known, real_guard):
    """The answer of an active entry the draft was written from, verbatim,
    is a ``kb`` shape: auto. A paraphrase is a free-form statement: a draft
    card. The outbox loads the texts itself: a suggested entry approves
    nothing, and a caller can hand in neither texts nor anything but ids."""
    person, trigger = await _client_question(session)
    entry = await _add(session, OFFICE_Q, OFFICE_A)
    verbatim = await _kb_row(session, person, trigger, OFFICE_A, kb_entry_ids=[entry.id])
    assert verbatim.state == "approved" and verbatim.approved_by == "auto:policy"
    assert verbatim.guard["shapes"] == ["kb"] and verbatim.guard["shapes_ok"] is True
    assert verbatim.guard["kb_entry_ids"] == [entry.id]
    # With a greeting before it, still only approved shapes.
    greeted = await _kb_row(
        session, person, trigger, f"Assalomu alaykum! {OFFICE_A}", kb_entry_ids=[entry.id]
    )
    assert greeted.state == "approved"
    assert greeted.guard["shapes"] == ["courtesy", "kb"]
    paraphrase = await _kb_row(
        session,
        person,
        trigger,
        "Ofisimiz Toshkent shahrida.",
        kb_entry_ids=[entry.id],
    )
    assert paraphrase.state == "draft"
    assert paraphrase.last_error == gate.UNAPPROVED_STATEMENT
    assert paraphrase.guard["shapes"] == ["statement"]
    # The entry not named by the draft approves nothing at creation.
    unnamed = await _kb_row(session, person, trigger, f"{OFFICE_A} ")
    assert unnamed.state == "draft" and unnamed.last_error == gate.UNAPPROVED_STATEMENT
    # A suggestion is not the owner's yet.
    suggested = m.KnowledgeEntry(
        kind="fact",
        answer="Yuk uchun invoys kerak.",
        tags=[],
        state="suggested",
        source="suggested",
        guard={},
        history=[],
    )
    session.add(suggested)
    await session.flush()
    held = await _kb_row(
        session,
        person,
        trigger,
        "Yuk uchun invoys kerak.",
        kb_entry_ids=[suggested.id],
    )
    assert held.state == "draft" and "kb_entry_ids" not in held.guard
    # Texts from the caller, a bare string or a non-int id: refused.
    for kw, code in (
        ({"approved_kb": (OFFICE_A,)}, outbox.APPROVED_TEXTS_REFUSED),
        ({"kb_entry_ids": str(entry.id)}, outbox.BAD_KB_IDS),
        ({"kb_entry_ids": [str(entry.id)]}, outbox.BAD_KB_IDS),
        ({"kb_entry_ids": [True]}, outbox.BAD_KB_IDS),
    ):
        with pytest.raises(outbox.OutboxError, match=code):
            await _kb_row(session, person, trigger, "Rahmat, aka!", **kw)
    # An entry never loosens a red line: even verbatim, a refused text is
    # refused (here written past the writer straight into the table).
    await session.execute(
        sa.update(m.KnowledgeEntry)
        .where(m.KnowledgeEntry.id == entry.id)
        .values(answer="Ofisimiz yonida bir kilosi 5 dollar.")
    )
    priced = await _kb_row(
        session,
        person,
        trigger,
        "Ofisimiz yonida bir kilosi 5 dollar.",
        kb_entry_ids=[entry.id],
    )
    assert priced.state == "blocked"
    # An active entry the kb role passes but the reply role refuses (office
    # hours, a digit and a deadline): it is an approved text, and quoting it
    # verbatim is still blocked, never sent and never drafted.
    hours_text = "Ofisimiz soat 9 dan 18 gacha ishlaydi."
    assert redlines.check_outbound(hours_text, "kb").passed
    assert not redlines.check_outbound(hours_text, "reply").passed
    hours = await _add(session, "Ish vaqtingiz qanday?", hours_text)
    assert await outbox.owner_kb_texts(session, [hours.id]) == {hours.id: hours_text}
    quoted = await _kb_row(session, person, trigger, hours_text, kb_entry_ids=[hours.id])
    assert quoted.state == "blocked" and quoted.approved_by is None


async def test_retired_entry_no_longer_approves(session, known, real_guard):
    """The claim guards a ``kb`` row again with the entries active then: a
    row approved before its entry was retired goes back to the card; one
    whose entry is still active is claimed, its entry ids kept."""
    person, trigger = await _client_question(session)
    keep = await _add(session, OFFICE_Q, OFFICE_A)
    drop = await _add(session, "Qanday hujjatlar kerak?", "Yuk uchun invoys kerak.")
    kept = await _kb_row(session, person, trigger, OFFICE_A, kb_entry_ids=[keep.id])
    dropped = await _kb_row(
        session, person, trigger, "Yuk uchun invoys kerak.", kb_entry_ids=[drop.id]
    )
    assert kept.state == dropped.state == "approved"
    await kb.retire(session, drop.id, by="command", now=NOW)
    claimed = await outbox.claim_ready(session, "telegram", now=NOW, limit=10)
    assert [c.id for c in claimed] == [kept.id]
    rows = {
        r.id: r
        for r in await session.scalars(
            sa.select(m.OutboxMessage).execution_options(populate_existing=True)
        )
    }
    back = rows[dropped.id]
    assert back.state == "draft" and back.last_error == gate.UNAPPROVED_STATEMENT
    assert back.approved_by is None
    assert back.guard["shapes"] == ["statement"]
    assert back.guard["kb_entry_ids"] == [drop.id]
    sent = rows[kept.id]
    assert sent.state == "sending" and sent.guard["kb_entry_ids"] == [keep.id]
    assert sent.guard["shapes"] == ["kb"]


async def test_the_claim_reads_the_active_entries_once(
    session, known, real_guard, monkeypatch
):
    """``outbox._context`` gets the answers of every active entry (it passed
    an empty tuple before JV-15), read once per claim; an unreadable base
    counts as none and never raises into the claim."""
    person, trigger = await _client_question(session)
    entry = await _add(session, OFFICE_Q, OFFICE_A)
    rows = [
        await _kb_row(session, person, trigger, body, kb_entry_ids=[entry.id])
        for body in (OFFICE_A, f"Assalomu alaykum! {OFFICE_A}")
    ]
    assert all(r.state == "approved" for r in rows)
    reads: list[object] = []
    original = kb.active_texts

    async def counting(*args, **kwargs):
        reads.append(args[1:] or kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(kb, "active_texts", counting)
    claimed = await outbox.claim_ready(session, "telegram", now=NOW, limit=10)
    # Both re-guarded rows matched the entry's answer, read once for both.
    assert sorted(c.id for c in claimed) == sorted(r.id for r in rows)
    assert len(reads) == 1
    monkeypatch.setattr(kb, "active_texts", original)
    retired = await _add(session, "Qanday hujjatlar kerak?", "Yuk uchun invoys kerak.")
    await kb.retire(session, retired.id, by="command", now=NOW)
    assert await outbox.owner_kb_texts(session) == {
        e.id: e.answer for e in await _entries(session, "active")
    }

    async def broken(*args, **kwargs):
        raise RuntimeError("gone")

    monkeypatch.setattr(kb, "active_texts", broken)
    assert await outbox.owner_kb_texts(session, [1, 2]) == {}


# --- the schema and the purge ----------------------------------------------------------


def test_check_lists_match_the_migration():
    path = ROOT / "alembic" / "versions" / "0027_knowledge_base.py"
    spec = importlib.util.spec_from_file_location("migration_0027", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.KINDS == m.KB_KINDS
    assert module.STATES == m.KB_STATES
    assert module.SOURCES == m.KB_SOURCES
    assert settings.embed_dim == module.EMBED_DIM
    assert module.down_revision == "0026_owner_style"
    assert len(module.revision) <= 32
    assert "KnowledgeEntry" in m.__all__
    memories = {i.name.removeprefix("ix_memories_") for i in m.Memory.__table__.indexes}
    entries = {
        i.name.removeprefix("ix_knowledge_entries_")
        for i in m.KnowledgeEntry.__table__.indexes
    }
    # The Memory search columns, copied.
    assert {"tsv", "trgm", "embedding_hnsw", "unembedded"} <= memories & entries


async def test_the_database_holds_the_line(session):
    for values in (
        {"kind": "faq", "question": None, "answer": OFFICE_A},
        {"kind": "fact", "answer": "  "},
        {"kind": "note", "answer": OFFICE_A},
        {"kind": "fact", "answer": OFFICE_A, "state": "draft"},
        {"kind": "fact", "answer": OFFICE_A, "source": "model"},
    ):
        row = {"state": "active", "source": "owner", "tags": [], "guard": {}, **values}
        with pytest.raises(sa.exc.IntegrityError):
            async with session.begin_nested():
                session.add(m.KnowledgeEntry(history=[], **row))
                await session.flush()


async def test_purge_takes_mined_entries_and_strikes_names(bound, known):
    """A suggestion goes with the owner's answer it was mined from; an
    entry that names a forgotten person is struck and retired."""
    session = bound
    akmal, _sardor, pairs = await _asked_twice(session)
    known.client.answers = [SUGGESTION]
    await kb.mine(session, now=NOW)
    [mined] = await _entries(session)
    source = await session.get(m.Interaction, mined.source_interaction_id)
    assert {source.id} <= {answered.id for _asked, answered in pairs}
    plan = await purge.plan_chat(session, source.tg_chat_id)
    assert plan.counts["knowledge_entries"] == 1
    assert "bilimlar bazasi yozuvi: 1 ta" in replies.purge_preview(plan)
    await purge.execute(session, plan)
    await session.flush()
    assert await _entries(session) == []
    # An owner entry written before the person was known.
    karim_fact = await _add(session, None, "Ombor ishlarini Karim boshqaradi.")
    karim = await _person(session, "Karim", telegram_id=KARIM_CHAT)
    plan = await purge.plan_person(session, karim)
    await purge.execute(session, plan)
    await session.flush()
    [struck] = await _entries(session)
    assert struck.id == karim_fact.id and struck.state == "retired"
    assert "Karim" not in struck.answer and struck.history[-1]["by"] == "auto:purge"
    assert akmal.id
    # An entry the owner retired before the person was forgotten: the name
    # is struck too, even when no entry is in use, and its history stays.
    old = await _add(session, None, "Yuklarni Olim qabul qiladi.")
    await kb.retire(session, old.id, by="command", now=NOW)
    await session.flush()
    olim = await _person(session, "Olim", telegram_id=KARIM_CHAT + 1)
    assert await kb.count(session, kb.ACTIVE) == 0
    plan = await purge.plan_person(session, olim)
    await purge.execute(session, plan)
    await session.flush()
    gone = next(e for e in await _entries(session) if e.id == old.id)
    assert gone.state == "retired" and "Olim" not in gone.answer
    assert "olim" not in (gone.search_norm or "") and gone.embedding is None
    assert [h["by"] for h in gone.history] == ["command", "command"]


def test_kb_match_threshold_is_in_all_four_places():
    env = dotenv_values(ROOT / ".env.example")
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert env["KB_MATCH_THRESHOLD"] == "0.75"
    above = lines[lines.index("KB_MATCH_THRESHOLD=0.75") - 1]
    assert above.startswith("#")
    assert "`KB_MATCH_THRESHOLD`" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "KB_MATCH_THRESHOLD" in doctor.EXPECTED_ENV_KEYS
    assert Settings(_env_file=None).kb_match_threshold == 0.75
    for bad in ("-0.1", "1.5"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, kb_match_threshold=bad)


def test_the_migration_round_trips():
    url = os.environ.get("MIYA_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("MIYA_MIGRATION_DATABASE_URL is not set")
    from tests.test_outbox import _alembic

    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "0026_owner_style")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            gone = conn.execute(sa.text("SELECT to_regclass('knowledge_entries')"))
            assert gone.scalar() is None
    finally:
        engine.dispose()
    _alembic(url, "upgrade", "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            back = conn.execute(sa.text("SELECT to_regclass('knowledge_entries')"))
            assert back.scalar() == "knowledge_entries"
    finally:
        engine.dispose()
