"""What a client's message asks for (docs/jarvis-plan.md §6.2, JV-16).

Deterministic first: a message that is only a courtesy (a greeting, thanks,
a farewell, read by the statement allowlist's closed courtesy set) is a
``greeting`` with no model call. Anything else is read by EXTRACT_MODEL,
which answers strict JSON ``{"intent": "<one of INTENTS>"}``; anything else
it says is a parse failure, never a guess. The red-line signals
(``redlines.classify_inbound``) are read before this and never reach it: a
price, deadline, promise, payment claim, identity, complaint, legal or
other-people message goes to the owner without an intent call.

* ``AUTO_ELIGIBLE`` may ever be sent on their own (``general_question``
  only when a knowledge-base entry matched: ``gate.auto_shapes_ok``, which
  ``outbox.create`` and the claim apply, wants a verbatim entry, a ``kb``
  shape, in such a text; questions and courtesy alone are a draft card).
* ``ESCALATE`` always go to the owner, with a stall.
* ``OTHER`` is drafted for the owner's card and never sent on its own
  (``gate.AUTO_INTENTS`` does not hold it).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.jarvis import costs, envelope, redlines

GREETING: Final = "greeting"
INFO_GATHERING: Final = "info_gathering"
GENERAL_QUESTION: Final = "general_question"
OTHER: Final = "other"
AUTO_ELIGIBLE: Final[tuple[str, ...]] = (GREETING, INFO_GATHERING, GENERAL_QUESTION)
ESCALATE: Final[tuple[str, ...]] = (
    "price",
    "discount",
    "payment_terms",
    "deadline",
    "promise",
    "guarantee",
    "payment_claim",
    "identity",
    "complaint",
    "legal",
    "other_people",
)
INTENTS: Final[tuple[str, ...]] = (*AUTO_ELIGIBLE, *ESCALATE, OTHER)

SOURCE_RULES: Final = "rules"
SOURCE_MODEL: Final = "model"

MAX_TOKENS: Final = 60
TIMEOUT_SECONDS: Final = 30

SYSTEM_PROMPT: Final = f"""\
You sort one client's latest chat messages to a small business owner in \
Uzbekistan (a cargo and logistics business) into exactly one intent:
- greeting: only a greeting, thanks or a farewell;
- info_gathering: the client describes what they need or answers a question \
(what goods, from where to where, how heavy), with nothing asked of the owner \
but to go on;
- general_question: a general question about the business that a fixed fact \
could answer (where the office is, what is shipped, which documents are needed);
- price, discount, payment_terms: a price, a discount or payment terms;
- deadline: when something happens or how long it takes;
- promise, guarantee: the client wants a promise or a guarantee;
- payment_claim: the client says they paid or sent money;
- identity: the client asks whether they talk to a bot, a person or the owner;
- complaint, legal: a complaint, or a legal threat;
- other_people: a question about another person or another person's goods;
- other: anything else.

{envelope.RULE}
The client's latest messages are the CLIENT lines after the last OWNER or MIYA line.

Answer with JSON only: {{"intent": "<intent>"}}"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class IntentError(ValueError):
    """The model's answer is not the JSON asked for."""


@dataclass(frozen=True, slots=True)
class Intent:
    intent: str
    source: str

    @property
    def auto_eligible(self) -> bool:
        return self.intent in AUTO_ELIGIBLE

    @property
    def escalates(self) -> bool:
        return self.intent in ESCALATE


def deterministic(text: str) -> str | None:
    """``greeting`` when every sentence of ``text`` is a courtesy from the
    closed set; None when a model must read it."""
    shapes = redlines.check_shapes(text or "")
    if shapes.sentences and all(s.shape == "courtesy" for s in shapes.sentences):
        return GREETING
    return None


def parse(reply: str) -> str:
    """The intent of the model's strict JSON answer; raises IntentError."""
    try:
        data = json.loads(_FENCE.sub("", (reply or "").strip()))
    except (ValueError, TypeError) as exc:
        raise IntentError("not_json") from exc
    if not isinstance(data, dict) or set(data) != {"intent"}:
        raise IntentError("bad_shape")
    value = data["intent"]
    if not isinstance(value, str) or value not in INTENTS:
        raise IntentError("bad_intent")
    return value


async def classify(
    session: AsyncSession,
    *,
    text: str,
    thread: str,
    now: datetime,
    source_interaction_id: int | None = None,
) -> Intent:
    """The intent of the client's latest messages (``text``), read in their
    ``thread`` (an envelope). Raises ``costs.ModelUnavailable`` (the cap, a
    failed or refused call) or ``IntentError`` (an answer that is not the
    JSON asked for); the caller sends the chat to the owner."""
    found = deterministic(text)
    if found is not None:
        return Intent(found, SOURCE_RULES)
    reply = await costs.ask(
        session,
        model=settings.extract_model,
        operation=costs.INTENT_OPERATION,
        system=SYSTEM_PROMPT,
        user=thread,
        max_tokens=MAX_TOKENS,
        timeout_seconds=TIMEOUT_SECONDS,
        now=now,
        source_interaction_id=source_interaction_id,
    )
    return Intent(parse(reply), SOURCE_MODEL)


__all__ = [
    "AUTO_ELIGIBLE",
    "ESCALATE",
    "GENERAL_QUESTION",
    "GREETING",
    "INFO_GATHERING",
    "INTENTS",
    "OTHER",
    "SYSTEM_PROMPT",
    "Intent",
    "IntentError",
    "classify",
    "deterministic",
    "parse",
]
