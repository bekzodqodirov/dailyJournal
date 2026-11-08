"""Talking to MIYA like an assistant (owner request, 2026-11-08).

«Salom» and «Akmalning qarzi qancha» came back as «📝 Yozib oldim». The rules
now read more questions and small talk on their own; what they still read as
a plain note is asked of the router (EXTRACT_MODEL), and a chat is answered
in its own mode. The model is stubbed throughout.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import rag, router
from miya.services.rag import RagAnswer
from tests.test_close_and_correct import bound  # noqa: F401
from tests.test_rag import _StubClient, _text_response

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0)


@pytest.fixture(autouse=True)
def no_embedder(monkeypatch):
    def none():
        raise RuntimeError("no embedder in tests")

    monkeypatch.setattr(rag, "get_embedder", none)
    monkeypatch.setattr(handlers, "get_embedder", none)


# --- what the rules read alone ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        # Small talk alone is a chat, with or without "?" or an address.
        ("Salom", "chat"),
        ("Assalomu alaykum, MIYA!", "chat"),
        ("Rahmat", "chat"),
        ("Katta rahmat aka", "chat"),
        ("Qalaysan?", "chat"),
        ("Nima gap?", "chat"),
        ("Ассалому алайкум", "chat"),
        ("Спасибо", "chat"),
        # A question word that ends the message, in any script.
        ("Akmalning qarzi qancha", "question"),
        ("Konteyner qayerda", "question"),
        ("Bu kim", "question"),
        ("Қанча қарзим бор", "question"),
        ("Қачон тўлайди", "question"),
        # The "-mi" particle, typed.
        ("Akmal pul berdimi", "question"),
        ("yuk keldimi", "question"),
        ("hujjat tayyormi", "question"),
        ("Hali ochiqmi", "question"),
        ("shundaymi", "question"),
        ("Пулни олдингизми", "question"),
        # Asking for what the records hold.
        ("Qarzlarimni ko'rsat", "question"),
        ("Qarzlarimni ko'rsating", "question"),
        ("Menga bugungi ishlarimni ayt", "question"),
        ("Ertangi rejani tuz", "question"),
        # General help and Russian or English imperatives: the router reads
        # them, as «Ertaga Akmalga yordam ber» is as often a task.
        ("Bu gapni ruschaga tarjima qil", "note"),
        ("Python nima ekanini tushuntirib ber", "note"),
        ("Ertaga Akmalga yordam ber", "note"),
        ("Akmalga hujjatni tushuntir", "note"),
        ("Akmal bilan nima bo'lgandi tushuntirib ber", "note"),
        ("Покажи долги", "note"),
        # Shown to someone else or later: the owner's task, not a question.
        ("Akmalga hujjatlarni ko'rsat", "note"),
        ("Ertaga ko'rsat", "note"),
        # Notes stay notes.
        ("Akmalga 5 mln berdim", "note"),
        ("u qancha to'lashini aytdi", "note"),
        ("Akmal ko'rsat dedi", "note"),
        ("Salom, Akmalga 5 mln berdim", "note"),
        ("Rahimi keldi", "note"),
        ("bu uning rasmi", "note"),
        ("uning rejimi", "note"),
        # «hech qachon», «hech kim» end notes; Russian «-ами/-ыми» is no particle.
        ("Muammo chiqmagan hech qachon.", "note"),
        ("Hech kim", "note"),
        ("Оплата наличными.", "note"),
        ("Можно частями.", "note"),
        ("oplata nalichnymi", "note"),
        ("ertaga soat 15:00 da uchrashuv", "note"),
        ("Akmal bilan gaplashgandik, 5 mln beradi", "note"),
        # A fact before the imperative or the question word: the router reads
        # it (and keeps a fact a note), as it reads any figure or long text.
        ("Akmal 3 mln qaytardi, qolganini hisobla", "note"),
        ("Akmal 3 mln qaytardi qolgani qancha", "note"),
        ("100 dollarni 12 ga hisobla", "note"),
        ("Akmal ming dollar qaytardi qolgani qancha", "note"),
        ("Akmal yarim million qaytardi qolgani qancha", "note"),
        ("Sardorga yuz dollar qarz berdim qaytaradimi", "note"),
        ("Akmal qarzini qaytardi, qarzlarimni ko'rsat", "note"),
        ("Akmal pulni qaytardi menga qolgan qarzlarni ayt", "note"),
        ("Bugun yangi mijoz keldi familiyasi Kim", "note"),
        ("Akmal Sardorning hodimi", "note"),
        # «Quda» (an in-law) is no Russian «куда».
        ("Quda 10 mln berdi", "note"),
        ("Quda bilan uchrashdim", "note"),
        # A clause before the last word, a name, a possessive, «yozib ber».
        ("Akmal qaytaraman dedi, bilmadim qachon", "note"),
        ("Akmal qaytaraman dedi bilmadim qachon", "note"),
        ("Sardor yukni oldi, keldimi", "note"),
        ("Mijoz Viktor Kim", "note"),
        ("Akmal Sardorning xodimi", "note"),
        ("bir necha", "note"),
        ("Ertaga Akmal bilan uchrashuv, yozib ber", "note"),
        ("Yozib ber: Akmal besh yuz dollar qarz oldi", "note"),
        # The rules cannot tell this one: the router reads it.
        ("Bugun nima qilishim kerak", "note"),
    ],
)
def test_the_rules_read_questions_and_small_talk(text, mode):
    assert rag.classify(text, commands=False) == mode


@pytest.mark.parametrize(
    ("heard", "mode"),
    [
        # Speech keeps the broad "-mi" reading it always had...
        ("Akmal ofisdami", "question"),
        ("ishlar yaxshimi", "question"),
        ("Akmal pulni berdimi", "question"),
        # ...but a Russian plural or a forwarded voice asks nothing.
        ("он годами не платил", "note"),
        ("оплата наличными", "note"),
    ],
)
def test_spoken_questions_keep_the_broad_particle(heard, mode):
    assert handlers._spoken_mode(heard, commands=False) == mode


def test_a_forwarded_voice_is_read_as_before():
    assert handlers._spoken_mode("Akmal ofisdami", commands=False, owner=False) == "note"


def test_small_talk_needs_a_courtesy_and_nothing_else():
    assert rag.is_small_talk("Salom MIYA")
    assert not rag.is_small_talk("MIYA")  # an address alone is no greeting
    assert not rag.is_small_talk("Salom, konteyner keldi")
    assert not rag.is_small_talk("")
    assert not rag.is_small_talk("rahmat " * 9)  # too long to be only courtesy


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("Salom aka", "note"),
        ("Aka yordam bering", "note"),
        ("Akmal pul berdimi", "note"),
        ("Qarzlarimni ko'rsat", "note"),
        ("Akmalning qarzi qancha", "note"),
        # What a forwarded message always asked with, it still asks with.
        ("Akmal qachon keladi?", "question"),
        ("qancha qarzim bor", "question"),
    ],
)
def test_a_forwarded_message_is_read_as_before(text, mode):
    """Someone else's words: never the owner's small talk or imperative."""
    assert rag.classify(text, commands=False, owner=False) == mode


def test_a_command_still_outranks_small_talk_and_questions():
    asked = "Sardorga yozib yubor: ertaga kelasanmi?"
    assert rag.classify(asked) == "command"
    assert rag.classify(asked, commands=False) == "question"
    assert rag.classify("Sardorga yozib yubor: rahmat") == "command"


# --- the router ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "route"),
    [
        ('{"route": "chat"}', "chat"),
        ('```json\n{"route": "question"}\n```', "question"),
        ('{"route": "note"}', "note"),
    ],
)
def test_parse_reads_the_strict_json(reply, route):
    assert router.parse(reply) == route


@pytest.mark.parametrize(
    "reply",
    ["", "chat", '{"route": "command"}', '{"route": "chat", "why": "x"}', "[1]"],
)
def test_parse_refuses_anything_else(reply):
    with pytest.raises(router.RouteError):
        router.parse(reply)


def test_user_content_carries_the_last_exchange_and_clips():
    turn = rag.HistoryTurn(question="Akmal qachon keladi?", answer="Ertaga.")
    content = router.user_content("rahmat " + "x" * 3000, (turn,))
    assert content.startswith("EARLIER:\nOWNER: Akmal qachon keladi?\nMIYA: Ertaga.")
    assert content.split("MESSAGE:\n")[1].startswith("rahmat")
    assert len(content.split("MESSAGE:\n")[1]) == router.MAX_INPUT_CHARS
    assert router.user_content("salom") == "MESSAGE:\nsalom"
    assert router.user_content("Vali ham", (turn,), replied=True).startswith(
        "REPLYING TO:\nOWNER: Akmal qachon keladi?"
    )
    assert "REPLYING TO block" in router.SYSTEM_PROMPT


class _Client(_StubClient):
    """The stub client, with the per-call options the router sets."""

    def __init__(self, responses: list) -> None:
        super().__init__(responses)
        self.options: list[dict] = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


def _refused() -> SimpleNamespace:
    return SimpleNamespace(
        content=[], stop_reason="refusal", usage=_text_response("").usage
    )


def _api_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


@pytest.fixture
def client(monkeypatch):
    stub = _Client([])
    monkeypatch.setattr(router, "get_client", lambda: stub)
    monkeypatch.setattr(settings, "router_enabled", True)
    return stub


async def _spent(session) -> list[m.UsageLog]:
    await session.flush()
    return list(
        await session.scalars(
            sa.select(m.UsageLog).where(m.UsageLog.operation == router.OPERATION)
        )
    )


async def test_route_asks_once_and_records_the_spend(session, client):
    client.responses = [_text_response('{"route": "chat"}')]

    assert await router.route(session, "salom, ishlar zo'rmi") == "chat"

    [call] = client.calls
    assert call["model"] == settings.extract_model
    assert call["system"] == router.SYSTEM_PROMPT
    assert call["max_tokens"] == router.MAX_TOKENS
    assert call["messages"] == [
        {"role": "user", "content": "MESSAGE:\nsalom, ishlar zo'rmi"}
    ]
    assert client.options == [{"timeout": router.TIMEOUT_SECONDS, "max_retries": 1}]
    assert len(await _spent(session)) == 1


@pytest.mark.parametrize(
    "response",
    [
        _api_error(),
        TimeoutError(),
        _text_response("I think it is a chat"),
        _text_response('{"route": "command"}'),
    ],
)
async def test_any_failure_is_none(session, client, response):
    client.responses = [response]
    assert await router.route(session, "bugun nima qilishim kerak") is None


async def test_a_refusal_is_none_but_its_spend_is_kept(session, client):
    client.responses = [_refused()]
    assert await router.route(session, "bugun nima qilishim kerak") is None
    assert len(await _spent(session)) == 1


async def test_off_or_empty_never_calls(session, client, monkeypatch):
    assert await router.route(session, "   ") is None
    monkeypatch.setattr(settings, "router_enabled", False)
    assert await router.route(session, "bugun nima qilishim kerak") is None
    assert client.calls == []


async def test_no_key_is_none(session, monkeypatch):
    # The conftest default: the client cannot be built at all.
    monkeypatch.setattr(settings, "router_enabled", True)
    assert await router.route(session, "bugun nima qilishim kerak") is None


# --- the bot: what the owner types -------------------------------------------------


class _Sent:
    message_id = 901


def _message(text: str, *, forwarded: bool = False, reply_to: int | None = None):
    message = SimpleNamespace(
        text=text,
        date=NOW,
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        reply_to_message=(
            SimpleNamespace(message_id=reply_to) if reply_to is not None else None
        ),
        forward_origin=SimpleNamespace(type="user") if forwarded else None,
        forward_date=None,
        sent=[],
    )

    async def answer(body, **kwargs):
        message.sent.append((body, kwargs.get("reply_markup")))
        return _Sent()

    message.answer = answer
    return message


async def _typing(message):
    return None


@pytest.fixture
def answered(monkeypatch):
    """rag.answer_full, recorded: (text, mode) of every answer asked for."""
    seen: list[tuple[str, str | None]] = []

    async def _answer_full(session, text, *, mode=None, **kwargs):
        seen.append((text, mode))
        return RagAnswer(text=f"javob: {mode}", mode=mode or "question")

    monkeypatch.setattr(handlers.rag, "answer_full", _answer_full)
    monkeypatch.setattr(handlers, "_typing", _typing)
    return seen


@pytest.fixture
def noted(monkeypatch):
    """The note pipeline, recorded: the ids of the rows extracted as notes."""
    ids: list[int] = []

    async def _process(session, interaction):
        ids.append(interaction.id)
        return SimpleNamespace(ok=True, applied=None)

    monkeypatch.setattr(handlers, "process_interaction", _process)
    monkeypatch.setattr(handlers, "_receipt", lambda result: ("📝 Yozib oldim.", None))
    return ids


async def _row(session, text: str) -> m.Interaction:
    return await session.scalar(
        sa.select(m.Interaction).where(m.Interaction.raw_text == text)
    )


async def test_the_router_turns_a_note_into_a_question(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    client.responses = [_text_response('{"route": "question"}')]

    message = _message("Bugun nima qilishim kerak")
    await handlers.on_text(message)

    assert answered == [("Bugun nima qilishim kerak", "question")]
    assert noted == []
    [(body, keyboard)] = message.sent
    assert body == "javob: question"
    # The router's call is never final: the answer can still become a note.
    assert keyboard == keyboards.save_as_note((await _row(bound, message.text)).id)
    row = await _row(bound, message.text)
    assert row.meta["kind"] == "question" and row.meta["routed"] == "model"
    assert row.meta["mode"] == "question"


async def test_the_router_turns_a_note_into_a_chat(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    client.responses = [_text_response('{"route": "chat"}')]

    await handlers.on_text(_message("Toshkentda bugun havo qanaqa bo'larkan"))

    assert answered == [("Toshkentda bugun havo qanaqa bo'larkan", "chat")]
    assert noted == []


async def test_a_note_the_router_agrees_with_is_extracted(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    client.responses = [_text_response('{"route": "note"}')]

    message = _message("Akmal bilan ertaga uchrashamiz")
    await handlers.on_text(message)

    assert answered == [] and len(noted) == 1
    assert message.sent == [("📝 Yozib oldim.", None)]


async def test_a_router_outage_keeps_the_note(bound, answered, noted):  # noqa: F811
    # The conftest default: the router cannot reach the model.
    await handlers.on_text(_message("Bugun nima qilishim kerak"))

    assert answered == [] and len(noted) == 1


async def test_small_talk_is_answered_without_the_router_or_a_note_button(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    message = _message("Salom MIYA")
    await handlers.on_text(message)

    assert client.calls == []
    assert answered == [("Salom MIYA", "chat")]
    assert noted == []
    assert message.sent == [("javob: chat", None)]
    assert "routed" not in (await _row(bound, "Salom MIYA")).meta


async def test_a_question_the_rules_read_never_reaches_the_router(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    await handlers.on_text(_message("Akmalning qarzi qancha"))

    assert client.calls == []
    assert answered == [("Akmalning qarzi qancha", "question")]


async def test_a_forwarded_message_is_never_routed(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    await handlers.on_text(_message("Bugun nima qilishim kerak", forwarded=True))

    assert client.calls == [] and answered == [] and len(noted) == 1


async def test_a_forwarded_plea_for_help_is_kept_as_a_note(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    await handlers.on_text(
        _message("Aka yordam bering, keyingi hafta qaytaraman", forwarded=True)
    )

    assert client.calls == [] and answered == [] and len(noted) == 1


async def _chat_answer(session) -> None:
    session.add(
        m.Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=NOW,
            raw_text="Salom",
            processed=True,
            meta={
                "kind": "question",
                "mode": "chat",
                "answer": "Va alaykum assalom!",
                "answer_message_id": 4242,
            },
        )
    )
    await session.flush()


async def test_a_fact_replied_to_a_chat_answer_is_kept(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    await _chat_answer(bound)
    client.responses = [_text_response('{"route": "note"}')]

    await handlers.on_text(_message("Akmalga 5 mln qarz berdim", reply_to=4242))

    content = client.calls[0]["messages"][0]["content"]
    assert content.startswith("REPLYING TO:\nOWNER: Salom\nMIYA: Va alaykum assalom!")
    assert answered == [] and len(noted) == 1


async def test_send_words_with_jarvis_off_are_never_routed(
    bound,  # noqa: F811
    client,
    answered,
    noted,
    monkeypatch,
):
    # Rule 20: with Jarvis off «… yozib yubor» is read as before, a note.
    monkeypatch.setattr(settings, "jarvis_enabled", False)

    await handlers.on_text(_message("Sardorga yozib yubor: ertaga soat 10 da kelsin"))

    assert client.calls == [] and answered == [] and len(noted) == 1


async def test_a_reply_asks_on_when_the_router_is_down(
    bound,  # noqa: F811
    answered,
    noted,
):
    # WP-75 as before: with no router, a reply to an answer is a question.
    await _chat_answer(bound)

    await handlers.on_text(_message("u qaysi kartaga tashlaydi", reply_to=4242))

    assert answered == [("u qaysi kartaga tashlaydi", "question")] and noted == []


async def test_the_router_sees_the_last_exchange(
    bound,  # noqa: F811
    client,
    answered,
    noted,
):
    bound.add(
        m.Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            occurred_at=NOW,
            raw_text="Akmal qachon keladi?",
            processed=True,
            meta={"kind": "question", "mode": "question", "answer": "Ertaga."},
        )
    )
    await bound.flush()
    client.responses = [_text_response('{"route": "chat"}')]

    await handlers.on_text(_message("zo'r, unda kutaman"))

    content = client.calls[0]["messages"][0]["content"]
    assert content.startswith("EARLIER:\nOWNER: Akmal qachon keladi?\nMIYA: Ertaga.")
    assert answered == [("zo'r, unda kutaman", "chat")]


# --- the bot: what the owner says ----------------------------------------------------


async def test_a_spoken_note_the_router_reads_as_a_chat_is_answered(
    bound,  # noqa: F811
    client,
    monkeypatch,
):
    client.responses = [_text_response('{"route": "chat"}')]
    rag_client = _StubClient([_text_response("Va alaykum assalom! Yaxshiman.")])
    monkeypatch.setattr(rag, "get_client", lambda: rag_client)
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        transcript="salom bugun kayfiyatim zo'r",
    )
    bound.add(row)
    await bound.flush()

    reply = await handlers._spoken_question(
        bound, row, "salom bugun kayfiyatim zo'r", _message("")
    )

    assert reply is not None and reply.startswith("🎙 <i>Eshitganim:</i>")
    assert "Yaxshiman" in reply
    assert row.meta["mode"] == "chat" and row.meta["routed"] == "model"
    assert "MODE: chat" in rag_client.calls[0]["messages"][-1]["content"]


async def test_a_spoken_note_stays_a_note_when_the_router_is_down(bound):  # noqa: F811
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        transcript="Akmal bilan ertaga uchrashamiz",
    )
    bound.add(row)
    await bound.flush()

    assert (
        await handlers._spoken_question(
            bound, row, "Akmal bilan ertaga uchrashamiz", _message("")
        )
        is None
    )


# --- the chat answer itself ---------------------------------------------------------


async def test_a_chat_fetches_no_evidence_and_says_its_mode(session, monkeypatch):
    stub = _StubClient([_text_response("Python — dasturlash tili.")])
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    async def no_search(*args, **kwargs):
        raise AssertionError("a chat must not search the records first")

    monkeypatch.setattr(rag.recall, "search", no_search)

    # «haqida» would fetch evidence for a question; a chat never does.
    result = await rag.answer_full(
        session, "Python haqida tushuntirib ber", mode="chat", now=NOW
    )

    assert result.text == "Python — dasturlash tili." and result.mode == "chat"
    content = stub.calls[0]["messages"][-1]["content"]
    assert "MODE: chat\n" in content and content.endswith("Python haqida tushuntirib ber")
    await session.flush()
    spent = await session.scalars(
        sa.select(m.UsageLog.operation).where(m.UsageLog.operation == rag.CHAT_OPERATION)
    )
    assert list(spent) == [rag.CHAT_OPERATION]


async def test_a_chat_still_guards_money_figures(session, monkeypatch):
    stub = _StubClient([_text_response("Taxminan 5 mln so'm bo'ladi.")])
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    result = await rag.answer_full(
        session, "rahmat, yana bir narsa", mode="chat", now=NOW
    )

    assert "5 mln" not in result.text and "summa tekshirilmadi" in result.text


def test_the_prompts_name_the_chat_mode_and_the_router_labels():
    assert "MODE: chat" in rag.RAG_SYSTEM_PROMPT
    assert "Eslatma sifatida yozib qo'y" in rag.RAG_SYSTEM_PROMPT
    assert json.dumps({"route": "<note|question|chat>"}) in router.SYSTEM_PROMPT
    assert replies.OPERATION_LABEL[router.OPERATION]
    assert replies.OPERATION_LABEL[rag.CHAT_OPERATION]
    assert replies.voice_question_header("salom", chat=True).startswith(
        "🎙 <i>Eshitganim:</i>"
    )
    assert replies.voice_question_header("qachon?").startswith("🎙 <i>Savolingiz:</i>")
