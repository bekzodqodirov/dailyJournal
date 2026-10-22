"""The owner's texting style (JV-14): profile, few-shot examples, per-person tone.

What MIYA learns from, and only that: the owner's own words (the JV-10
authorship predicate: never a reply MIYA sent), typed into private chats
with real people, newest ``STYLE_SAMPLE_SIZE`` of them, archive imports
included. Never a chat the owner marked personal (an active ``never`` reply
rule, JV-12), the Telegram service chat, Saved Messages, a bot, or a chat
the owner switched off.

* **Traits** are deterministic counts (``compute_traits``): greetings,
  closings, honorifics, siz/sen, frequent phrases, emoji, length,
  punctuation, Latin/Cyrillic and Russian words.
* **Prose** (``prompt_text``) is REASON_MODEL's description of the traits,
  refused when it holds a digit, a money word, a link or a name; the
  deterministic ``style_prompt.fallback_prose`` stands in when no model may
  write it. Usage is recorded as ``jarvis_style`` and counts towards the
  Jarvis cost cap; above the cap no call is made.
* **Examples** are context-reply pairs: a client's message and the owner's
  reply to it, admitted deny-by-default (``admit_reply``, ``admit_context``):
  the reply passes the red-line guard in the owner role with no hit at all
  (so a reply that promises, names a time or a price never teaches the AI
  to), and neither text carries a digit, a link, a handle, hidden
  characters or a person's name other than the counterparty's. The texts
  are joined by id, never copied, and are checked again whenever they are
  read: an edit, a ``/unut`` or a chat marked personal later applies.
* **Tone** per person (``owner_style_people``): honorific, siz/sen, script,
  language and greeting, from the owner's own messages to them.

A new profile is a ``draft``; the owner activates it (``activate``), and at
most one is ``active``. ``build_profile`` skips when ``input_hash`` is
unchanged. Below ``STYLE_MIN_SAMPLES`` no profile is built.

The owner's stall overrides live on the active profile (``stalls``); only
those passing the stall role are ever used (``stall_overrides``,
``stall_texts``), which ``outbox`` reads for the statement allowlist.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from miya.config import settings
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.models import (
    ChatMonitor,
    Interaction,
    OwnerStyleExample,
    OwnerStylePerson,
    OwnerStyleProfile,
    Person,
    ReplyRule,
)
from miya.jarvis import costs, redlines, stalls, style_prompt
from miya.jarvis.style_prompt import ExamplePair, ReplyStyle, Tone
from miya.services import authorship
from miya.services.embeddings import EmbeddingError, get_embedder
from miya.services.extraction import API_FAILURES, get_client
from miya.services.recaps import MONEY_WORDS
from miya.services.text import (
    STOPWORDS,
    fold_apostrophes,
    name_pattern,
    normalise_for_search,
)
from miya.services.usage import record_anthropic_usage

log = logging.getLogger(__name__)

# Bump when the sampler, the traits or the scrubber change: every profile's
# input_hash changes with it, so the next refresh rebuilds.
STYLE_VERSION: Final = "style-v1"
STYLE_OPERATION: Final = "jarvis_style"
# Serialises builds (the worker's refresh, the owner's «Qayta tuzish»).
BUILD_LOCK: Final = "jarvis:style:build"

DRAFT: Final = "draft"
ACTIVE: Final = "active"
RETIRED: Final = "retired"
SOURCE_HISTORY: Final = "history"
BY_REFRESH: Final = "auto:refresh"

TELEGRAM_SERVICE_ID: Final = 777000
# The client message an owner reply answers: the one right before it in the
# chat, at most this long before.
CONTEXT_MAX_GAP: Final = timedelta(hours=6)
# A text longer than an outbound reply may be is no example of one.
EXAMPLE_MAX_CHARS: Final = redlines.MAX_CHARS
MAX_EXAMPLES: Final = 60
EXAMPLES_PER_PROMPT: Final = 4
CARD_EXAMPLES: Final = 3
# Per-person tone: from this many of the owner's newest messages to them,
# and only for someone with at least the minimum.
PERSON_SAMPLE: Final = 40
PERSON_MIN_SAMPLES: Final = 3
# A built profile older than this is rebuilt (when its input changed).
REFRESH_EVERY: Final = timedelta(days=7)
# The prose call.
PROSE_MAX_TOKENS: Final = 700
PROSE_TIMEOUT_SECONDS: Final = 60
# Trait list lengths.
TOP_GREETINGS: Final = 5
TOP_HONORIFICS: Final = 5
TOP_PHRASES: Final = 8
PHRASE_MIN_COUNT: Final = 3
# Emoji level by the share of messages with one.
EMOJI_MEDIUM_PCT: Final = 10
EMOJI_HIGH_PCT: Final = 30
# Per-person script and language: one side at or above this share wins.
DOMINANT_PCT: Final = 80
UNSAFE_VERDICTS: Final = frozenset({"spam", "scam", "hijack"})

Status = Literal["built", "unchanged", "too_few"]
EmojiLevel = Literal["low", "medium", "high"]


def _keys(raw: str) -> frozenset[str]:
    """A lexicon written naturally (comma-separated), in the form compared:
    ``normalise_for_search``, like ``text.STOPWORDS``."""
    return frozenset(k for k in (normalise_for_search(w) for w in raw.split(",")) if k)


GREETINGS: Final = _keys(
    """
    assalomu alaykum, assalom alaykum, assalomu aleykum, assalom aleykum,
    salom alaykum, salom aleykum, salomaleykum, salom, salomlar, va alaykum assalom,
    vaalaykum assalom, xayrli tong, xayrli kun, xayrli kech, hayrli tong, hayrli kun,
    ассалому алайкум, ассалом алайкум, салом, салом алайкум, ваалайкум ассалом,
    здравствуйте, здравствуй, привет, добрый день, доброе утро, добрый вечер,
    privet, zdravstvuyte, dobriy den
    """
)
CLOSINGS: Final = _keys(
    """
    rahmat, katta rahmat, raxmat, rahmat sizga, xayr, ko'rishguncha, sog' bo'ling,
    omad, ishlaringizga omad, рахмат, катта рахмат, хайр, спасибо, большое спасибо,
    пока, до свидания, всего доброго, spasibo, poka
    """
)
HONORIFICS: Final = _keys(
    """
    aka, akajon, akaxon, akam, opa, opajon, opam, uka, ukajon, ukam, singlim,
    ustoz, domla, hoji, xola, amaki, brat, bratan, bro, ака, акажон, акам, опа,
    опажон, ука, укажон, устоз, домла, брат, братан
    """
)
# The owner's courtesy and acknowledgement words, counted wherever they
# stand; frequent word pairs and triples are found besides.
SIGNATURE: Final = _keys(
    """
    xo'p bo'ladi, xo'p, hop, ko'rib beraman, ko'rib chiqaman, qarab beraman,
    bilib beraman, aniqlab beraman, mayli, bo'pti, tushunarli, tushundim, albatta,
    yaxshi, zo'r, ok, okey, hozir, ладно, хорошо, понял, договорились, давай,
    хоп, майли, хўп бўлади, кўриб бераман
    """
)
SIZ_WORDS: Final = _keys(
    """
    siz, sizga, sizni, sizdan, sizda, sizning, sizlar, sizlarga, sizlarni,
    sizlarning, sizlardan, сиз, сизга, сизни, сиздан, вы, вам, вас, ваш, ваша,
    ваше, ваши, вами
    """
)
SEN_WORDS: Final = _keys(
    """
    sen, senga, seni, sendan, senda, sening, сен, сенга, сени, сендан, ты, тебе,
    тебя, твой, твоя, твоё, твои, тобой
    """
)
# Russian words typed in Latin letters by an Uzbek writer.
RUSSIAN_LATIN: Final = _keys(
    """
    davay, normalno, ladno, konechno, prosto, voobshe, xorosho, khorosho, spasibo,
    poka, privet, srazu, seychas, ponyal, dogovorilis, kstati, uje, uzhe, mojno,
    mozhno, izvinite, zdravstvuyte, bratan, tochno, nado, budet
    """
)
# Words that are never a person's name on their own, though a contact may
# be saved under one («Aka», «Mijoz»).
NOT_NAMES: Final = (
    HONORIFICS
    | GREETINGS
    | CLOSINGS
    | STOPWORDS
    | _keys("mijoz, klient, client, sklad, ombor, dostavka, bank, kassa, ofis, office")
)

_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
_SENTENCE_END = re.compile(r"[.!?…\n]+")
_DIGIT = re.compile(r"\d")
_LINK = re.compile(
    r"(?i)https?://|www\.|t\.me/|\b[\w-]+\.(?:uz|ru|com|net|org|io|me|tj|kz)\b|@\w"
)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_EMOJI_RANGES: Final = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE0F, 0xFE0F),
)


# --- pure text helpers ------------------------------------------------------------------


def _words(text: str) -> list[str]:
    """The words as typed, lower case, apostrophes folded."""
    return _WORD.findall(fold_apostrophes(text).casefold())


def _key(word: str) -> str:
    return normalise_for_search(word)


def is_emoji(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _EMOJI_RANGES)


def _hidden(text: str) -> bool:
    """Bidi overrides, zero-width marks and other format characters."""
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def private_data_free(text: str) -> bool:
    """No digit (so no figure, code, phone or date), no link, handle or
    e-mail, no hidden characters."""
    return not (
        _DIGIT.search(text) or _LINK.search(text) or _EMAIL.search(text) or _hidden(text)
    )


@dataclass(frozen=True, slots=True)
class NameIndex:
    """Every person's names, to refuse a text that names anyone but its own
    counterparty. ``pattern`` runs on ``normalise_for_search`` text."""

    pattern: re.Pattern[str] | None = None
    by_person: Mapping[int, tuple[str, ...]] = field(default_factory=dict)

    def names_of(self, person_id: int | None) -> tuple[str, ...]:
        if person_id is None:
            return ()
        return self.by_person.get(person_id, ())

    def other_named(self, text: str, person_id: int | None) -> bool:
        """``text`` names someone who is not ``person_id``."""
        if self.pattern is None:
            return False
        words = normalise_for_search(text)
        own = self.names_of(person_id)
        forms = {f for f in (normalise_for_search(n) for n in own) if f}
        parts = forms | {p for f in forms for p in f.split()}
        allowed = name_pattern(sorted(parts))
        if allowed is not None:
            words = allowed.sub(lambda m: " " * len(m.group(0)), words)
        return self.pattern.search(words) is not None


def name_forms(names: Iterable[str]) -> list[str]:
    """The names worth matching: each whole name and its first word, never
    a word that is also an honorific, a greeting or a common word."""
    out: list[str] = []
    for name in names:
        form = normalise_for_search(name or "")
        if not form:
            continue
        for candidate in (form, form.split()[0]):
            if len(candidate) >= 3 and candidate not in NOT_NAMES:
                out.append(candidate)
    return list(dict.fromkeys(out))


def build_name_index(
    people: Iterable[tuple[int, str | None, Sequence[str] | None]],
    *,
    owner_aliases: Iterable[str] = (),
) -> NameIndex:
    """The index over ``(person_id, display_name, aliases)`` rows and the
    owner's own aliases (an owner alias is never a counterparty's name)."""
    by_person: dict[int, tuple[str, ...]] = {}
    every: list[str] = []
    for person_id, display_name, aliases in people:
        names = tuple(n for n in (display_name, *(aliases or ())) if n)
        by_person[person_id] = names
        every.extend(names)
    every.extend(a for a in owner_aliases if a and not a.startswith("@"))
    return NameIndex(name_pattern(name_forms(every)), by_person)


def admit_reply(text: str | None, *, names: NameIndex, person_id: int | None) -> bool:
    """Deny by default: an owner reply is an example only with letters, at
    most a reply's length, no private data, no other person's name and no
    red-line hit at all in the owner role (no promise, time, price,
    guarantee, money or link: the AI must not learn to promise)."""
    if not text or not text.strip() or len(text) > EXAMPLE_MAX_CHARS:
        return False
    if not _words(text) or not private_data_free(text):
        return False
    if names.other_named(text, person_id):
        return False
    verdict = redlines.check_outbound(text, "owner")
    return verdict.passed and not verdict.hits


def admit_context(
    text: str | None,
    *,
    names: NameIndex,
    person_id: int | None,
    meta: Mapping[str, Any] | None = None,
) -> bool:
    """A client message an example answers: letters, at most a reply's
    length, no private data and no other person's name, and never one the
    shield called spam or a scam. Its red lines are the client's own (they
    may ask the price); the owner's reply is what must pass them."""
    if not text or not text.strip() or len(text) > EXAMPLE_MAX_CHARS:
        return False
    if not _words(text) or not private_data_free(text):
        return False
    safety = (meta or {}).get("safety")
    if isinstance(safety, Mapping) and safety.get("verdict") in UNSAFE_VERDICTS:
        return False
    return not names.other_named(text, person_id)


def example_intent(context: str, reply: str) -> str:
    """The reply kind an example shows: what the client's message asked for
    when it escalates, else a greeting, a question or a general answer."""
    reasons = redlines.escalation_reasons(redlines.classify_inbound(context))
    if reasons:
        return reasons[0]
    shapes = redlines.check_shapes(reply).shapes
    if shapes and all(shape == "courtesy" for shape in shapes):
        return "greeting"
    if "?" in reply:
        return "info_gathering"
    return "general_question"


# --- traits -----------------------------------------------------------------------------


def _prefix_match(keys: Sequence[str], lexicon: frozenset[str]) -> int:
    """How many leading word keys form one entry of ``lexicon`` (longest)."""
    for n in range(min(4, len(keys)), 0, -1):
        if " ".join(keys[:n]) in lexicon:
            return n
    return 0


def _suffix_match(keys: Sequence[str], lexicon: frozenset[str]) -> int:
    for n in range(min(4, len(keys)), 0, -1):
        if " ".join(keys[-n:]) in lexicon:
            return n
    return 0


def _spelling(words: Sequence[str]) -> str:
    return " ".join(words)


def _top(
    counts: Counter[str],
    spellings: Mapping[str, Counter[str]],
    limit: int,
    *,
    longest_first: bool = False,
) -> list[list[Any]]:
    """``[[label, count], ...]``, most frequent first, ties by key (or the
    longer phrase first): the label is the owner's own most frequent
    spelling."""
    ranked = sorted(
        counts.items(),
        key=lambda item: (
            -item[1],
            -len(item[0].split()) if longest_first else 0,
            item[0],
        ),
    )[:limit]
    out: list[list[Any]] = []
    for key, count in ranked:
        spelled = sorted(spellings[key].items(), key=lambda s: (-s[1], s[0]))
        out.append([spelled[0][0], count])
    return out


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def _greeting_of(words: Sequence[str], keys: Sequence[str]) -> tuple[str, str] | None:
    n = _prefix_match(keys, GREETINGS)
    if not n:
        return None
    return " ".join(keys[:n]), _spelling(words[:n]).capitalize()


def _closing_of(words: Sequence[str], keys: Sequence[str]) -> tuple[str, str] | None:
    trimmed = list(keys)
    shown = list(words)
    while trimmed and trimmed[-1] in HONORIFICS:
        trimmed.pop()
        shown.pop()
    n = _suffix_match(trimmed, CLOSINGS)
    if not n:
        return None
    return " ".join(trimmed[-n:]), _spelling(shown[-n:]).capitalize()


def _address_counts(keys: Iterable[str]) -> tuple[int, int]:
    siz = sen = 0
    for key in keys:
        if key in SIZ_WORDS or key.endswith(("ingiz", "ngiz", "siz", "sizmi")):
            siz += 1
        elif key in SEN_WORDS or key.endswith(("san", "sanmi")):
            sen += 1
    return siz, sen


def _russian_words(text: str, keys: Sequence[str]) -> int:
    if redlines.script_of(text) == "russian":
        return len(keys)
    return sum(1 for k in keys if k in RUSSIAN_LATIN)


def _ngrams(keys: Sequence[str], words: Sequence[str]) -> dict[str, str]:
    """Word pairs and triples of one message: key -> the owner's spelling."""
    found: dict[str, str] = {}
    for n in (2, 3):
        for i in range(len(keys) - n + 1):
            part = keys[i : i + n]
            edges = (part[0], part[-1])
            if any(k in STOPWORDS or k in HONORIFICS for k in edges):
                # «… aka», «va …»: the phrase is the words between.
                continue
            found.setdefault(" ".join(part), _spelling(words[i : i + n]))
    return found


def _phrase_safe(phrase: str) -> bool:
    verdict = redlines.check_outbound(phrase, "owner")
    return verdict.passed and not verdict.hits


def compute_traits(
    texts: Sequence[str], *, names: NameIndex | None = None
) -> dict[str, Any]:
    """The deterministic traits of the owner's messages: the same texts give
    the same traits. Counts are per message unless they say words; a
    phrase that names a person or carries a digit is never listed, and each
    phrase says whether it passes the red lines (``safe``): only safe ones
    ever reach a model or a reply."""
    n = len(texts)
    greetings: Counter[str] = Counter()
    closings: Counter[str] = Counter()
    honorifics: Counter[str] = Counter()
    phrases: Counter[str] = Counter()
    spelled: dict[str, Counter[str]] = {}
    emoji_messages = 0
    emoji_counts: Counter[str] = Counter()
    total_words = 0
    sentence_words: list[int] = []
    punctuation = Counter[str]()
    scripts = Counter[str]()
    russian = 0
    siz = sen = 0

    def note(counter: Counter[str], key: str, spelling: str) -> None:
        counter[key] += 1
        spelled.setdefault(key, Counter())[spelling] += 1

    for text in texts:
        words = _words(text)
        keys = [_key(w) for w in words]
        pairs = [(w, k) for w, k in zip(words, keys, strict=True) if k]
        words = [w for w, _ in pairs]
        keys = [k for _, k in pairs]
        total_words += len(keys)
        for sentence in _SENTENCE_END.split(text):
            count = len(_words(sentence))
            if count:
                sentence_words.append(count)
        greeting = _greeting_of(words, keys)
        if greeting is not None:
            note(greetings, *greeting)
        closing = _closing_of(words, keys)
        if closing is not None:
            note(closings, *closing)
        seen_honorifics = {
            k: w for w, k in zip(words, keys, strict=True) if k in HONORIFICS
        }
        for key, word in seen_honorifics.items():
            note(honorifics, key, word)
        message_phrases: dict[str, str] = {}
        for key in SIGNATURE:
            parts = key.split()
            for i in range(len(keys) - len(parts) + 1):
                if keys[i : i + len(parts)] == parts:
                    message_phrases.setdefault(key, _spelling(words[i : i + len(parts)]))
                    break
        for key, spelling in _ngrams(keys, words).items():
            message_phrases.setdefault(key, spelling)
        for key, spelling in message_phrases.items():
            note(phrases, key, spelling)
        found = [ch for ch in text if is_emoji(ch) and ch != "\ufe0f"]
        if found:
            emoji_messages += 1
            emoji_counts.update(dict.fromkeys(found, 1))
        stripped = text.rstrip()
        if stripped.endswith(("...", "…")):
            punctuation["ellipsis"] += 1
        elif stripped.endswith("?"):
            punctuation["question"] += 1
        elif stripped.endswith("!"):
            punctuation["exclamation"] += 1
        elif stripped.endswith("."):
            punctuation["period"] += 1
        elif stripped.endswith(")"):
            punctuation["smiley"] += 1
        else:
            punctuation["none"] += 1
        first = next((ch for ch in text if ch.isalpha()), "")
        if first and first.isupper():
            punctuation["capitalised"] += 1
        written = redlines.script_of(text)
        if written is not None:
            scripts[written] += 1
        russian += _russian_words(text, keys)
        a, b = _address_counts(keys)
        siz += a
        sen += b

    def listed(key: str) -> bool:
        if _DIGIT.search(key):
            return False
        if names is not None and names.other_named(key, None):
            return False
        if key in GREETINGS or key in CLOSINGS:
            return False
        return phrases[key] >= PHRASE_MIN_COUNT and (
            key in SIGNATURE or phrases[key] * 100 >= 2 * n
        )

    phrase_counts = Counter({k: c for k, c in phrases.items() if listed(k)})
    chosen: list[list[Any]] = []
    ranked = _top(phrase_counts, spelled, len(phrase_counts), longest_first=True)
    for label, count in ranked:
        if any(_redundant(label, count, c[0], c[1]) for c in chosen):
            continue
        chosen.append([label, count, _phrase_safe(label)])
        if len(chosen) >= TOP_PHRASES:
            break
    lettered = sum(scripts.values())
    emoji_pct = _pct(emoji_messages, n)
    level: EmojiLevel = (
        "high"
        if emoji_pct >= EMOJI_HIGH_PCT
        else "medium"
        if emoji_pct >= EMOJI_MEDIUM_PCT
        else "low"
    )
    return {
        "version": STYLE_VERSION,
        "messages": n,
        "greetings": _top(greetings, spelled, TOP_GREETINGS),
        "closings": _top(closings, spelled, TOP_GREETINGS),
        "honorifics": _top(honorifics, spelled, TOP_HONORIFICS),
        "address": {"siz": siz, "sen": sen},
        "phrases": chosen,
        "emoji": {
            "messages": emoji_messages,
            "pct": emoji_pct,
            "level": level,
            "top": [
                e for e, _ in sorted(emoji_counts.items(), key=lambda i: (-i[1], i[0]))
            ][:5],
        },
        "words": {
            "total": total_words,
            "avg": round(total_words / n, 1) if n else 0.0,
            "sentence_avg": (
                round(sum(sentence_words) / len(sentence_words), 1)
                if sentence_words
                else 0.0
            ),
        },
        "punctuation": {
            **{k: punctuation[k] for k in sorted(punctuation)},
            "none_pct": _pct(punctuation["none"], n),
            "capitalised_pct": _pct(punctuation["capitalised"], n),
        },
        "script": {
            "latin": scripts["latin"],
            "cyrillic": scripts["cyrillic"],
            "russian": scripts["russian"],
            "latin_pct": _pct(scripts["latin"], lettered),
            "cyrillic_pct": _pct(scripts["cyrillic"] + scripts["russian"], lettered),
        },
        "russian_words_pct": _pct(russian, total_words),
    }


def _key_of_label(label: str) -> str:
    return " ".join(_key(w) for w in _words(label))


def _redundant(label: str, count: int, other: str, other_count: int) -> bool:
    """A phrase inside another listed one as often as it (or holding it as
    often): the same habit counted twice."""
    a, b = f" {_key_of_label(label)} ", f" {_key_of_label(other)} "
    return count == other_count and (a in b or b in a)


# --- per-person tone --------------------------------------------------------------------


def tone_of(texts: Sequence[str]) -> Tone:
    """How the owner writes to one person, from their messages to them."""
    honorifics: Counter[str] = Counter()
    greetings: Counter[str] = Counter()
    spelled: dict[str, Counter[str]] = {}
    siz = sen = 0
    latin = cyrillic = lettered = 0
    russian_messages = 0
    for text in texts:
        words = _words(text)
        keys = [_key(w) for w in words]
        for key in {k for k in keys if k in HONORIFICS}:
            honorifics[key] += 1
        greeting = _greeting_of(words, keys)
        if greeting is not None:
            greetings[greeting[0]] += 1
            spelled.setdefault(greeting[0], Counter())[greeting[1]] += 1
        a, b = _address_counts(keys)
        siz += a
        sen += b
        written = redlines.script_of(text)
        if written is None:
            continue
        lettered += 1
        if written == "latin":
            latin += 1
        else:
            cyrillic += 1
        if written == "russian" or sum(1 for k in keys if k in RUSSIAN_LATIN) * 2 >= max(
            len(keys), 1
        ):
            russian_messages += 1
    honorific = None
    if honorifics:
        honorific = sorted(honorifics.items(), key=lambda i: (-i[1], i[0]))[0][0][:16]
    greeting_label = None
    if greetings:
        key = sorted(greetings.items(), key=lambda i: (-i[1], i[0]))[0][0]
        greeting_label = sorted(spelled[key].items(), key=lambda s: (-s[1], s[0]))[0][0]
    script = None
    lang = None
    if lettered:
        if _pct(latin, lettered) >= DOMINANT_PCT:
            script = "latin"
        elif _pct(cyrillic, lettered) >= DOMINANT_PCT:
            script = "cyrillic"
        else:
            script = "mixed"
        ru = _pct(russian_messages, lettered)
        lang = "ru" if ru >= DOMINANT_PCT else "uz" if ru <= 100 - DOMINANT_PCT else "mix"
    return Tone(
        honorific=honorific,
        address_form="siz" if siz > sen else "sen" if sen > siz else "unknown",
        script=script,
        lang=lang,
        greeting=greeting_label,
    )


# --- the sampler ------------------------------------------------------------------------


def no_media(media: Any) -> sa.ColumnElement[bool]:
    """A text message: no media at all. A Python None stored explicitly is
    the JSON ``null``, not SQL NULL, so both count."""
    return sa.or_(media.is_(None), sa.func.jsonb_typeof(media) == "null")


def chat_key_of(tg_chat_id: Any) -> sa.ColumnElement[str]:
    """``chatkey.telegram`` in SQL."""
    return sa.func.concat("tg:", tg_chat_id)


def personal_clause(tg_chat_id: Any, person_id: Any) -> sa.ColumnElement[bool]:
    """The chat or its person holds an active ``never`` reply rule (JV-12):
    by the chat's key, the address snapshot of its user, or the person."""
    key = chat_key_of(tg_chat_id)
    return (
        sa.select(ReplyRule.id)
        .where(
            ReplyRule.state == "active",
            ReplyRule.mode == "never",
            sa.or_(
                ReplyRule.chat_key == key,
                ReplyRule.address == key,
                ReplyRule.person_id == person_id,
            ),
        )
        .exists()
    )


def learnable_clause() -> list[sa.ColumnElement[bool]]:
    """The owner's own typed words in a monitored private chat with a real
    person: never MIYA's reply, a personal chat, the service chat, Saved
    Messages or a bot."""
    clauses: list[sa.ColumnElement[bool]] = [
        authorship.owner_words_clause(),
        Interaction.source == InteractionSource.telegram_userbot,
        Interaction.tg_chat_id > 0,
        Interaction.tg_chat_id != TELEGRAM_SERVICE_ID,
        Interaction.raw_text.isnot(None),
        sa.func.btrim(Interaction.raw_text) != "",
        no_media(Interaction.media),
        ChatMonitor.chat_type == ChatType.private,
        ChatMonitor.monitor_enabled.is_(True),
        sa.not_(
            sa.func.coalesce(sa.func.lower(Person.telegram_username), "").like("%bot")
        ),
        sa.not_(personal_clause(Interaction.tg_chat_id, Interaction.person_id)),
    ]
    if settings.owner_telegram_id is not None:
        clauses.append(Interaction.tg_chat_id != settings.owner_telegram_id)
    return clauses


@dataclass(frozen=True, slots=True)
class Sample:
    """One of the owner's messages, and the client message it answers."""

    interaction_id: int
    person_id: int | None
    tg_chat_id: int
    text: str
    occurred_at: datetime
    context_id: int | None = None
    context_text: str | None = None
    context_meta: Mapping[str, Any] | None = None


async def sample(session: AsyncSession, *, limit: int | None = None) -> list[Sample]:
    """The newest ``STYLE_SAMPLE_SIZE`` learnable messages, newest first, each
    with the message right before it in the chat when that is the client's
    text within CONTEXT_MAX_GAP."""
    size = limit if limit is not None else settings.style_sample_size
    picked = (
        sa.select(
            Interaction.id,
            Interaction.person_id,
            Interaction.tg_chat_id,
            Interaction.raw_text,
            Interaction.occurred_at,
        )
        .select_from(Interaction)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .outerjoin(Person, Person.id == Interaction.person_id)
        .where(*learnable_clause())
        .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
        .limit(size)
        .subquery("picked")
    )
    prev = aliased(Interaction)
    before = (
        sa.select(
            prev.id.label("context_id"),
            prev.direction.label("context_direction"),
            prev.raw_text.label("context_text"),
            prev.media.label("context_media"),
            prev.meta.label("context_meta"),
            prev.occurred_at.label("context_at"),
        )
        .where(
            prev.tg_chat_id == picked.c.tg_chat_id,
            sa.tuple_(prev.occurred_at, prev.id)
            < sa.tuple_(picked.c.occurred_at, picked.c.id),
        )
        .order_by(prev.occurred_at.desc(), prev.id.desc())
        .limit(1)
        .lateral("before")
    )
    rows = await session.execute(
        sa.select(picked, before)
        .select_from(picked)
        .outerjoin(before, sa.true())
        .order_by(picked.c.occurred_at.desc(), picked.c.id.desc())
    )
    out: list[Sample] = []
    for row in rows.mappings():
        context_ok = (
            row["context_id"] is not None
            and row["context_direction"] is Direction.in_
            and not row["context_media"]
            and row["context_text"] is not None
            and row["context_at"] >= row["occurred_at"] - CONTEXT_MAX_GAP
        )
        out.append(
            Sample(
                interaction_id=int(row["id"]),
                person_id=row["person_id"],
                tg_chat_id=int(row["tg_chat_id"]),
                text=str(row["raw_text"]).strip(),
                occurred_at=row["occurred_at"],
                context_id=int(row["context_id"]) if context_ok else None,
                context_text=str(row["context_text"]).strip() if context_ok else None,
                context_meta=row["context_meta"] if context_ok else None,
            )
        )
    return out


def input_hash(samples: Sequence[Sample]) -> str:
    """What a profile was built from: the messages, their words and their
    contexts. An edit, a purge or a new message changes it."""
    digest = hashlib.sha256(STYLE_VERSION.encode())
    for s in sorted(samples, key=lambda s: s.interaction_id):
        digest.update(
            f"{s.interaction_id}:{s.context_id}:{s.text}\x1f"
            f"{s.context_text or ''}\x1e".encode()
        )
    return digest.hexdigest()


async def name_index(session: AsyncSession) -> NameIndex:
    rows = await session.execute(
        sa.select(Person.id, Person.display_name, Person.aliases)
    )
    return build_name_index(
        ((int(r[0]), r[1], r[2]) for r in rows),
        owner_aliases=settings.owner_aliases_parsed,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """An admitted example before it is stored."""

    interaction_id: int
    context_id: int
    person_id: int | None
    context: str
    reply: str
    intent: str
    script: str | None


def select_examples(samples: Sequence[Sample], names: NameIndex) -> list[Candidate]:
    """The context-reply pairs that pass the scrubber, newest first, at most
    MAX_EXAMPLES."""
    out: list[Candidate] = []
    for s in samples:
        if s.context_id is None or s.context_text is None:
            continue
        if not admit_reply(s.text, names=names, person_id=s.person_id):
            continue
        if not admit_context(
            s.context_text, names=names, person_id=s.person_id, meta=s.context_meta
        ):
            continue
        out.append(
            Candidate(
                interaction_id=s.interaction_id,
                context_id=s.context_id,
                person_id=s.person_id,
                context=s.context_text,
                reply=s.text,
                intent=example_intent(s.context_text, s.text),
                script=redlines.script_of(s.text),
            )
        )
        if len(out) >= MAX_EXAMPLES:
            break
    return out


async def _embed(texts: list[str]) -> list[list[float] | None]:
    """One vector per text, or None for each when embedding is unavailable:
    the examples are kept and found by intent instead."""
    if not texts:
        return []
    try:
        vectors = await get_embedder().embed(texts)
    except EmbeddingError as exc:
        log.warning(
            "style examples not embedded",
            extra={"examples": len(texts), "error": type(exc).__name__},
        )
        return [None] * len(texts)
    if len(vectors) != len(texts):
        return [None] * len(texts)
    return [list(v) for v in vectors]


# --- the prose --------------------------------------------------------------------------


async def write_prose(
    session: AsyncSession,
    traits: Mapping[str, Any],
    examples: Sequence[ExamplePair],
    *,
    names: NameIndex,
    now: datetime,
) -> tuple[str, str]:
    """(prompt_text, writer): REASON_MODEL's style guide, or the
    deterministic one when the cost cap is reached, the call fails or its
    answer is refused. Never raises on a model failure."""
    fallback = style_prompt.fallback_prose(traits)
    if await costs.over_cap(session, now):
        log.info("style prose not written: cost cap", extra={"op": STYLE_OPERATION})
        return fallback, style_prompt.FALLBACK_MODEL
    model = settings.reason_model
    try:
        client = get_client().with_options(timeout=PROSE_TIMEOUT_SECONDS, max_retries=1)
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=PROSE_MAX_TOKENS,
                system=style_prompt.PROSE_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": style_prompt.prose_input(traits, examples),
                    }
                ],
            ),
            timeout=PROSE_TIMEOUT_SECONDS + 5,
        )
        await record_anthropic_usage(
            session, model=model, operation=STYLE_OPERATION, usage=response.usage
        )
        reply = "".join(
            str(getattr(part, "text", ""))
            for part in response.content
            if part.type == "text"
        )
        prose = style_prompt.parse_prose(reply, names=names.pattern, money=MONEY_WORDS)
    except (TimeoutError, *API_FAILURES, ValueError, TypeError) as exc:
        log.warning("style prose unavailable", extra={"error": type(exc).__name__})
        return fallback, style_prompt.FALLBACK_MODEL
    if prose is None:
        log.warning("style prose refused", extra={"op": STYLE_OPERATION})
        return fallback, style_prompt.FALLBACK_MODEL
    return prose, model[:64]


# --- profiles ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildResult:
    status: Status
    profile: OwnerStyleProfile | None
    sample_count: int
    example_count: int


def _entry(
    field: str, old: object, new: object, *, by: str, now: datetime
) -> dict[str, Any]:
    return {"at": now.isoformat(), "field": field, "old": old, "new": new, "by": by[:16]}


async def latest_profile(session: AsyncSession) -> OwnerStyleProfile | None:
    found: OwnerStyleProfile | None = await session.scalar(
        sa.select(OwnerStyleProfile).order_by(OwnerStyleProfile.version.desc()).limit(1)
    )
    return found


async def active_profile(session: AsyncSession) -> OwnerStyleProfile | None:
    found: OwnerStyleProfile | None = await session.scalar(
        sa.select(OwnerStyleProfile).where(OwnerStyleProfile.state == ACTIVE)
    )
    return found


async def current_profile(session: AsyncSession) -> OwnerStyleProfile | None:
    """What ``/uslub`` shows: a draft newer than the active profile, else the
    active one, else the newest draft."""
    active = await active_profile(session)
    query = sa.select(OwnerStyleProfile).where(OwnerStyleProfile.state == DRAFT)
    if active is not None:
        query = query.where(OwnerStyleProfile.version > active.version)
    draft: OwnerStyleProfile | None = await session.scalar(
        query.order_by(OwnerStyleProfile.version.desc()).limit(1)
    )
    return draft or active


async def example_count(session: AsyncSession, profile_id: int) -> int:
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OwnerStyleExample)
        .where(OwnerStyleExample.profile_id == profile_id)
    )
    return int(count or 0)


async def refresh_due(session: AsyncSession, *, now: datetime) -> bool:
    """No profile yet, or the newest is older than REFRESH_EVERY."""
    latest = await latest_profile(session)
    return latest is None or latest.created_at <= now - REFRESH_EVERY


def min_samples() -> int:
    """STYLE_MIN_SAMPLES, never above STYLE_SAMPLE_SIZE: a smaller sample
    size set on its own still builds a profile."""
    return min(settings.style_min_samples, settings.style_sample_size)


async def build_profile(
    session: AsyncSession, *, now: datetime, by: str = BY_REFRESH, force: bool = False
) -> BuildResult:
    """Sample, count, scrub, describe and store a new ``draft`` profile with
    its examples, and refresh every person's tone.

    Skips (``unchanged``) when the newest profile was built from the same
    input, unless ``force`` (the owner's «Qayta tuzish»). Below
    ``min_samples()`` nothing is built (``too_few``). The owner's stall
    overrides carry over from the active profile."""
    await session.execute(
        sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext(BUILD_LOCK)))
    )
    samples = await sample(session)
    latest = await latest_profile(session)
    minimum = min_samples()
    if len(samples) < minimum:
        log.info(
            "style profile not built: too few messages",
            extra={"samples": len(samples), "minimum": minimum},
        )
        return BuildResult("too_few", latest, len(samples), 0)
    digest = input_hash(samples)
    if not force and latest is not None and latest.input_hash == digest:
        return BuildResult(
            "unchanged",
            latest,
            latest.sample_count,
            await example_count(session, latest.id),
        )
    names = await name_index(session)
    traits = compute_traits([s.text for s in samples], names=names)
    candidates = select_examples(samples, names)
    pairs = [_pair(c.context, c.reply, names.names_of(c.person_id)) for c in candidates]
    prose, writer = await write_prose(session, traits, pairs, names=names, now=now)
    active = await active_profile(session)
    version = (
        int(
            await session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(OwnerStyleProfile.version), 0))
            )
            or 0
        )
        + 1
    )
    profile = OwnerStyleProfile(
        version=version,
        state=DRAFT,
        traits=traits,
        prompt_text=prose,
        stalls=stalls.approved_overrides(active.stalls if active is not None else None),
        sample_count=len(samples),
        window_start=min(s.occurred_at for s in samples),
        window_end=max(s.occurred_at for s in samples),
        input_hash=digest,
        model=writer[:64],
        history=[_entry("state", None, DRAFT, by=by, now=now)],
    )
    session.add(profile)
    await session.flush()
    vectors = await _embed([c.context for c in candidates])
    stored = 0
    for candidate, vector in zip(candidates, vectors, strict=True):
        # One savepoint per example: a message purged since it was sampled
        # fails only its own row (rule 25).
        try:
            async with session.begin_nested():
                session.add(
                    OwnerStyleExample(
                        profile_id=profile.id,
                        interaction_id=candidate.interaction_id,
                        context_interaction_id=candidate.context_id,
                        intent=candidate.intent[:24],
                        script=candidate.script,
                        source=SOURCE_HISTORY,
                        embedding=vector,
                    )
                )
                await session.flush()
            stored += 1
        except Exception as exc:
            log.error(
                "style example not stored",
                extra={
                    "profile_id": profile.id,
                    "interaction_id": candidate.interaction_id,
                    "context_interaction_id": candidate.context_id,
                    "error": type(exc).__name__,
                },
            )
    await refresh_people(session)
    log.info(
        "style profile built",
        extra={
            "profile_id": profile.id,
            "version": version,
            "samples": len(samples),
            "examples": stored,
            "writer": "model" if writer != style_prompt.FALLBACK_MODEL else "rules",
        },
    )
    return BuildResult("built", profile, len(samples), stored)


async def activate(
    session: AsyncSession, version: int, *, by: str, now: datetime
) -> OwnerStyleProfile | None:
    """The owner's «✅ Faollashtirish»: this draft becomes the one active
    profile, the previous one is retired and so is every older draft. None
    for a version that is gone, retired or older than the active one (a
    stale card); an active one is returned as it is.

    The owner's stall overrides are edited on the active profile only
    (``set_stall_override``), so the activated draft takes the active
    profile's overrides as they are now, not its copy from build time."""
    row: OwnerStyleProfile | None = await session.scalar(
        sa.select(OwnerStyleProfile)
        .where(OwnerStyleProfile.version == version)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.state == RETIRED:
        return None
    if row.state == ACTIVE:
        return row
    current: OwnerStyleProfile | None = await session.scalar(
        sa.select(OwnerStyleProfile)
        .where(OwnerStyleProfile.state == ACTIVE)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is not None and row.version < current.version:
        return None
    if current is not None:
        current.state = RETIRED
        current.history = [
            *(current.history or []),
            _entry("state", ACTIVE, RETIRED, by=by, now=now),
        ]
        await session.flush()
        row.stalls = stalls.approved_overrides(current.stalls)
    superseded = await session.scalars(
        sa.select(OwnerStyleProfile)
        .where(
            OwnerStyleProfile.state == DRAFT,
            OwnerStyleProfile.version < row.version,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for draft in superseded:
        # An older draft's card reads as stale from now on.
        draft.state = RETIRED
        draft.history = [
            *(draft.history or []),
            _entry("state", DRAFT, RETIRED, by=by, now=now),
        ]
    row.state = ACTIVE
    row.approved_at = now
    row.approved_by = by[:16]
    row.history = [*(row.history or []), _entry("state", DRAFT, ACTIVE, by=by, now=now)]
    await session.flush()
    log.info(
        "style profile activated", extra={"profile_id": row.id, "version": row.version}
    )
    return row


# --- per-person tone, stored ------------------------------------------------------------


async def _person_texts(
    session: AsyncSession, person_ids: Sequence[int] | None = None
) -> dict[int, list[str]]:
    """The owner's newest PERSON_SAMPLE learnable messages to each person."""
    ranked = (
        sa.select(
            Interaction.person_id.label("person_id"),
            Interaction.raw_text.label("text"),
            sa.func.row_number()
            .over(
                partition_by=Interaction.person_id,
                order_by=(Interaction.occurred_at.desc(), Interaction.id.desc()),
            )
            .label("rank"),
        )
        .select_from(Interaction)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .outerjoin(Person, Person.id == Interaction.person_id)
        .where(Interaction.person_id.isnot(None), *learnable_clause())
    )
    if person_ids is not None:
        ranked = ranked.where(Interaction.person_id.in_(list(person_ids)))
    sub = ranked.subquery("ranked")
    rows = await session.execute(
        sa.select(sub.c.person_id, sub.c.text)
        .where(sub.c.rank <= PERSON_SAMPLE)
        .order_by(sub.c.person_id, sub.c.rank)
    )
    out: dict[int, list[str]] = {}
    for person_id, text in rows:
        out.setdefault(int(person_id), []).append(str(text))
    return out


async def _store_tone(
    session: AsyncSession, person_id: int, texts: Sequence[str]
) -> None:
    tone = tone_of(texts)
    values = {
        "honorific": tone.honorific,
        "address_form": tone.address_form,
        "script": tone.script,
        "lang": tone.lang,
        "greeting": tone.greeting,
        "sample_count": len(texts),
        "updated_at": sa.func.now(),
    }
    await session.execute(
        pg_insert(OwnerStylePerson)
        .values(person_id=person_id, **values)
        .on_conflict_do_update(index_elements=[OwnerStylePerson.person_id], set_=values)
    )


async def refresh_people(session: AsyncSession) -> int:
    """Every person's tone from the owner's messages to them; a person with
    too few (or whose chat became personal) loses theirs. Returns how many
    rows were written."""
    texts = await _person_texts(session)
    kept: list[int] = []
    for person_id, rows in texts.items():
        if len(rows) < PERSON_MIN_SAMPLES:
            continue
        # One savepoint per person: someone forgotten meanwhile fails only
        # their own row (rule 25).
        try:
            async with session.begin_nested():
                await _store_tone(session, person_id, rows)
            kept.append(person_id)
        except Exception as exc:
            log.error(
                "style tone not stored",
                extra={"person_id": person_id, "error": type(exc).__name__},
            )
    await session.execute(
        sa.delete(OwnerStylePerson).where(OwnerStylePerson.person_id.not_in(kept))
        if kept
        else sa.delete(OwnerStylePerson)
    )
    await session.flush()
    return len(kept)


async def refresh_person(session: AsyncSession, person_id: int) -> bool:
    """One person's tone again (``people.merge_into``); True when stored."""
    texts = (await _person_texts(session, [person_id])).get(person_id, [])
    if len(texts) < PERSON_MIN_SAMPLES:
        await session.execute(
            sa.delete(OwnerStylePerson).where(OwnerStylePerson.person_id == person_id)
        )
        await session.flush()
        return False
    await _store_tone(session, person_id, texts)
    await session.flush()
    return True


async def tone_for(session: AsyncSession, person_id: int | None) -> Tone:
    """How the owner writes to this person; the neutral default when unknown
    (the plan's Q2: «Siz», and «aka» only where the history uses it)."""
    if person_id is None:
        return Tone()
    row = await session.get(OwnerStylePerson, person_id)
    if row is None:
        return Tone()
    return Tone(
        honorific=row.honorific,
        address_form=row.address_form,
        script=row.script,
        lang=row.lang,
        greeting=row.greeting,
    )


# --- examples, read back ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StyleExample:
    """A stored example as it reads now, the counterparty's name replaced."""

    id: int
    intent: str | None
    script: str | None
    pair: ExamplePair


def _pair(context: str, reply: str, names: Sequence[str]) -> ExamplePair:
    """The pair with the counterparty's names (whole, and each first word)
    replaced by ``style_prompt.PLACEHOLDER``."""
    forms = list(names)
    for name in names:
        first = name.split()[0] if name.split() else ""
        if len(first) >= 3 and normalise_for_search(first) not in NOT_NAMES:
            forms.append(first)
    return ExamplePair(
        context=style_prompt.with_placeholder(context, forms),
        reply=style_prompt.with_placeholder(reply, forms),
    )


async def examples_for(
    session: AsyncSession,
    profile: OwnerStyleProfile | None = None,
    *,
    text: str | None = None,
    intent: str | None = None,
    script: str | None = None,
    limit: int = EXAMPLES_PER_PROMPT,
) -> list[StyleExample]:
    """The profile's (default: the active one's) examples nearest to
    ``text`` by meaning, preferring ``intent`` and ``script``; without a
    ``text`` (or with no embedding) the newest. Every one is read back and
    scrubbed again: an edited message that no longer passes, a purged one,
    or a chat marked personal since, is never shown."""
    profile = profile or await active_profile(session)
    if profile is None or limit <= 0:
        return []
    context = aliased(Interaction)
    query = (
        sa.select(
            OwnerStyleExample.id,
            OwnerStyleExample.intent,
            OwnerStyleExample.script,
            Interaction.raw_text,
            Interaction.person_id,
            context.raw_text,
            context.meta,
        )
        .select_from(OwnerStyleExample)
        .join(Interaction, Interaction.id == OwnerStyleExample.interaction_id)
        .join(context, context.id == OwnerStyleExample.context_interaction_id)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .outerjoin(Person, Person.id == Interaction.person_id)
        .where(OwnerStyleExample.profile_id == profile.id, *learnable_clause())
    )
    order: list[sa.ColumnElement[Any]] = []
    if intent:
        order.append(sa.case((OwnerStyleExample.intent == intent, 0), else_=1))
    if script:
        order.append(sa.case((OwnerStyleExample.script == script, 0), else_=1))
    vector = None
    if text and text.strip():
        vector = (await _embed([text.strip()]))[0]
    if vector is not None:
        order.append(
            sa.func.coalesce(OwnerStyleExample.embedding.cosine_distance(vector), 2.0)
        )
    order += [Interaction.occurred_at.desc(), OwnerStyleExample.id.desc()]
    rows = await session.execute(query.order_by(*order).limit(limit * 4))
    names = await name_index(session)
    out: list[StyleExample] = []
    for example_id, kind, written, reply_text, person_id, context_text, meta in rows:
        if not admit_reply(reply_text, names=names, person_id=person_id):
            continue
        if not admit_context(context_text, names=names, person_id=person_id, meta=meta):
            continue
        out.append(
            StyleExample(
                id=int(example_id),
                intent=kind,
                script=written,
                pair=_pair(
                    str(context_text).strip(),
                    str(reply_text).strip(),
                    names.names_of(person_id),
                ),
            )
        )
        if len(out) >= limit:
            break
    return out


async def for_reply(
    session: AsyncSession,
    *,
    person_id: int | None,
    text: str | None,
    intent: str | None = None,
) -> ReplyStyle | None:
    """What a reply drafter (JV-16) reads: the active profile's guide, this
    person's tone and the nearest examples. None without an active profile."""
    profile = await active_profile(session)
    if profile is None:
        return None
    written = redlines.script_of(text or "")
    examples = await examples_for(
        session, profile, text=text, intent=intent, script=written
    )
    return ReplyStyle(
        prompt_text=profile.prompt_text,
        tone=await tone_for(session, person_id),
        examples=tuple(e.pair for e in examples),
    )


# --- the owner's card -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StyleCard:
    """``/uslub`` in plain values (replies.py renders it, never importing
    miya.jarvis)."""

    version: int
    state: str
    sample_count: int
    greetings: tuple[str, ...]
    address: tuple[str, ...]
    avg_words: int
    phrases: tuple[str, ...]
    emoji_level: str
    latin_pct: int
    cyrillic_pct: int
    russian_pct: int
    examples: tuple[ExamplePair, ...]


def _int(block: object, key: str) -> int:
    if not isinstance(block, Mapping):
        return 0
    value = block.get(key)
    return int(value) if isinstance(value, int | float) else 0


async def card(session: AsyncSession, profile: OwnerStyleProfile) -> StyleCard:
    traits = profile.traits or {}
    honorifics = [row[0] for row in traits.get("honorifics") or [] if row]
    address = traits.get("address") or {}
    siz, sen = _int(address, "siz"), _int(address, "sen")
    forms = (["siz"] if siz else []) + (["sen"] if sen else [])
    if siz and sen:
        forms.sort(key=lambda f: -(siz if f == "siz" else sen))
    words = traits.get("words") or {}
    avg = words.get("avg") if isinstance(words, Mapping) else 0
    emoji = traits.get("emoji") or {}
    script = traits.get("script") or {}
    examples = await examples_for(session, profile, limit=CARD_EXAMPLES)
    return StyleCard(
        version=profile.version,
        state=profile.state,
        sample_count=profile.sample_count,
        greetings=tuple(row[0] for row in traits.get("greetings") or [] if row)[:3],
        address=(*honorifics[:3], *forms),
        avg_words=max(round(float(avg)), 1) if isinstance(avg, int | float) else 1,
        phrases=tuple(row[0] for row in traits.get("phrases") or [] if row)[:5],
        emoji_level=str(emoji.get("level", "low"))
        if isinstance(emoji, Mapping)
        else "low",
        latin_pct=_int(script, "latin_pct"),
        cyrillic_pct=_int(script, "cyrillic_pct"),
        russian_pct=int(traits.get("russian_words_pct") or 0),
        examples=tuple(e.pair for e in examples),
    )


# --- the owner's stall overrides --------------------------------------------------------


async def stall_overrides(session: AsyncSession) -> dict[str, str]:
    """The active profile's overrides that pass the stall role now, by
    ``stalls.override_key``: what ``stalls.choose`` takes."""
    profile = await active_profile(session)
    return stalls.approved_overrides(profile.stalls if profile is not None else None)


async def stall_texts(session: AsyncSession) -> tuple[str, ...]:
    """The override texts the statement allowlist counts as stalls
    (``gate.Context.stall_texts``)."""
    profile = await active_profile(session)
    return stalls.approved_texts(profile.stalls if profile is not None else None)


async def set_stall_override(
    session: AsyncSession,
    *,
    reason: stalls.Reason,
    script: redlines.Script,
    honorific: stalls.Honorific,
    text: str | None,
    by: str,
    now: datetime,
) -> bool:
    """Store (or, with no text, remove) the owner's own wording of one stall
    on the active profile. Refused (False) without an active profile, or
    when the text does not pass the red-line guard in the stall role."""
    profile: OwnerStyleProfile | None = await session.scalar(
        sa.select(OwnerStyleProfile)
        .where(OwnerStyleProfile.state == ACTIVE)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if profile is None:
        return False
    key = stalls.override_key(reason, script, honorific)
    current = dict(profile.stalls or {})
    if text and text.strip():
        if not stalls.approved_overrides({key: text}):
            return False
        new: str | None = text.strip()
        current[key] = new
    else:
        new = None
        current.pop(key, None)
    profile.stalls = current
    profile.history = [
        *(profile.history or []),
        _entry("stall", key, "set" if new else "removed", by=by, now=now),
    ]
    await session.flush()
    return True


__all__ = [
    "ACTIVE",
    "DRAFT",
    "RETIRED",
    "STYLE_OPERATION",
    "BuildResult",
    "Candidate",
    "NameIndex",
    "Sample",
    "StyleCard",
    "StyleExample",
    "activate",
    "active_profile",
    "admit_context",
    "admit_reply",
    "build_name_index",
    "build_profile",
    "card",
    "compute_traits",
    "current_profile",
    "example_count",
    "example_intent",
    "examples_for",
    "for_reply",
    "input_hash",
    "latest_profile",
    "learnable_clause",
    "min_samples",
    "name_index",
    "personal_clause",
    "private_data_free",
    "refresh_due",
    "refresh_people",
    "refresh_person",
    "sample",
    "select_examples",
    "set_stall_override",
    "stall_overrides",
    "stall_texts",
    "tone_for",
    "tone_of",
    "write_prose",
]
