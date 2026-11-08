"""Is the owner's message a note, a question or a chat? (owner request, 2026-11-08)

The owner writes to MIYA as to an assistant, and «Salom» or «Akmalning qarzi
qancha» used to come back as «📝 Yozib oldim». The rules go first
(``rag.classify``: a "?", a question word, the "-mi" particle, a command, an
opinion, small talk); only what they leave a plain note reaches this module,
and EXTRACT_MODEL is asked one short question with a strict JSON answer,
``{"route": "note" | "question" | "chat"}``.

Any failure is None and the caller keeps the note: no key, an outage, a
timeout, a refusal, or an answer that is not the JSON asked for. A message is
never lost because the router could not read it, and a wrong "question" or
"chat" still carries «📝 Eslatma sifatida yozib qo'y» under its answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.services.extraction import API_FAILURES, get_client
from miya.services.usage import record_anthropic_usage

log = logging.getLogger(__name__)

NOTE: Final = "note"
QUESTION: Final = "question"
CHAT: Final = "chat"
ROUTES: Final[tuple[str, ...]] = (NOTE, QUESTION, CHAT)

OPERATION: Final = "route"
MAX_TOKENS: Final = 30
TIMEOUT_SECONDS: Final = 15
# The router reads what kind of message this is, not all of it: a long
# dictated note is cut here (it is still extracted whole).
MAX_INPUT_CHARS: Final = 1500
HISTORY_CHARS: Final = 400
REFUSAL_STOP: Final = "refusal"

SYSTEM_PROMPT: Final = """\
You route one message the owner of a small cargo business in Uzbekistan \
(China–Uzbekistan freight) sent to MIYA, his personal assistant in Telegram. \
MIYA keeps his journal: it writes down what he tells it (debts, payments, \
promises, tasks, meetings, what happened) and answers questions from those \
records. Decide what he wants from THIS message:
- note: he tells MIYA something to keep — a fact, an event, a debt, a \
payment, a promise, a plan, a task, a reminder for later, a thought. \
Examples: "Akmalga 5 mln berdim", "ertaga soat 3 da Sardor bilan \
uchrashuv", "konteyner bojxonadan chiqdi", "Vali GS367 yukini oldi", \
"Akmalga qo'ng'iroq qilish kerak".
- question: he asks about his own records, people, money, plans or what \
happened, or asks MIYA to show or list something from them. Examples: \
"bugun nima qilishim kerak", "Akmal bilan oxirgi marta qachon gaplashdim", \
"kimlarga qarzim bor ekan", "ertangi uchrashuvlarim".
- chat: he talks to MIYA as to an assistant, not about his records: a \
greeting, thanks, small talk, a general-knowledge question, or asking it to \
explain, translate, calculate or write something. Examples: "qalesan, \
ishlar yaxshimi", "rahmat senga", "Xitoy yangi yili qachon boshlanadi", \
"100 ni 12 ga bo'lsa nechchi", "Akmalga uzr so'rab chiroyli xat yozib ber".
If the message states any fact worth keeping, it is a note, even when it \
also greets or asks. When unsure, it is a note.
The message may be in Uzbek (Latin or Cyrillic), Russian or English, often \
mixed and with typos. An EARLIER block, when present, is MIYA's last \
exchange with him, for context only. A REPLYING TO block means he answered \
that very exchange: a reply that states no new fact asks on ("Vali ham", \
"o'tgan oydagisi", "keyin-chi") and is a question; when unsure about a \
reply, it is a question.

Answer with JSON only: {"route": "<note|question|chat>"}"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class RouteError(ValueError):
    """The model's answer is not the JSON asked for."""


class Turn(Protocol):
    """An earlier exchange (``rag.HistoryTurn`` has this shape)."""

    @property
    def question(self) -> str: ...

    @property
    def answer(self) -> str: ...


def parse(reply: str) -> str:
    """The route of the model's strict JSON answer; raises RouteError."""
    try:
        data = json.loads(_FENCE.sub("", (reply or "").strip()))
    except (ValueError, TypeError) as exc:
        raise RouteError("not_json") from exc
    if not isinstance(data, dict) or set(data) != {"route"}:
        raise RouteError("bad_shape")
    value = data["route"]
    if not isinstance(value, str) or value not in ROUTES:
        raise RouteError("bad_route")
    return value


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def user_content(
    text: str, history: Sequence[Turn] = (), *, replied: bool = False
) -> str:
    """The one user turn: the last exchange, if any (the one he replied to,
    when ``replied``), then the message."""
    parts: list[str] = []
    if history:
        last = history[-1]
        label = "REPLYING TO" if replied else "EARLIER"
        parts.append(
            f"{label}:\n"
            f"OWNER: {_clip(last.question, HISTORY_CHARS)}\n"
            f"MIYA: {_clip(last.answer, HISTORY_CHARS)}"
        )
    parts.append(f"MESSAGE:\n{_clip(text, MAX_INPUT_CHARS)}")
    return "\n\n".join(parts)


async def route(
    session: AsyncSession,
    text: str,
    *,
    history: Sequence[Turn] = (),
    replied: bool = False,
) -> str | None:
    """'note', 'question' or 'chat' for a message the rules left a note;
    None when the router is off, the text is empty or the call fails in any
    way. Never raises. The usage of an answered call is recorded.
    ``replied``: the message replies to the last turn of ``history``."""
    if not settings.router_enabled or not (text or "").strip():
        return None
    model = settings.extract_model
    try:
        client = get_client().with_options(timeout=TIMEOUT_SECONDS, max_retries=1)
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": user_content(text, history, replied=replied),
                    }
                ],
            ),
            timeout=TIMEOUT_SECONDS + 5,
        )
    except (TimeoutError, *API_FAILURES) as exc:
        error = type(exc).__name__
        log.warning("router call failed: %s", error, extra={"error": error})
        return None
    await record_anthropic_usage(
        session, model=model, operation=OPERATION, usage=getattr(response, "usage", None)
    )
    if getattr(response, "stop_reason", None) == REFUSAL_STOP:
        log.warning("router call refused", extra={"error": REFUSAL_STOP})
        return None
    reply = "".join(
        str(getattr(part, "text", ""))
        for part in getattr(response, "content", None) or []
        if getattr(part, "type", None) == "text"
    )
    try:
        return parse(reply)
    except RouteError as exc:
        log.warning("router answer unreadable: %s", exc, extra={"error": str(exc)})
        return None


__all__ = [
    "CHAT",
    "NOTE",
    "OPERATION",
    "QUESTION",
    "ROUTES",
    "SYSTEM_PROMPT",
    "RouteError",
    "parse",
    "route",
    "user_content",
]
