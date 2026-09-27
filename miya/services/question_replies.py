"""Answer a question batch by typing (WP-84): "1 ha 2 yo'q 3 bajarildi".

Deterministic: no model call, no guess. The text is read token by token;
a number opens an item's answer, words up to its verb close it, and an
amount before "ha" (claims only) is a correction first. "hammasi ha" /
"hammasi yo'q" answer every item. Anything the grammar does not cover
makes the whole text "not a batch reply" and it is handled as usual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from miya.services.text import fold_apostrophes

# Typed verb → the words a button's label starts with, apostrophes folded.
VERBS: dict[str, tuple[str, ...]] = {
    "ha": ("ha",),
    "xa": ("ha",),
    "yoq": ("yoq",),
    "bajarildi": ("bajarildi",),
    "boglandim": ("boglandim",),
    "oqi": ("oqi",),
    "kerak emas": ("kerak", "emas"),
    "ertalab": ("ertalab",),
    "javob berdim": ("javob", "berdim"),
    "kirim": ("kirim",),
    "chiqim": ("chiqim",),
    "pul emas": ("pul", "emas"),
}
_ALL = ("hammasi", "hammasiga", "barchasi")


@dataclass(slots=True)
class Answer:
    number: int
    verb: str  # a key of VERBS, canonical ("xa" → "ha")
    amount_text: str | None = None


def _norm(text: str) -> list[str]:
    folded = fold_apostrophes(text or "").lower().replace("'", "")
    return re.sub(r"[,;.!]+", " ", folded).split()


def _verb_at(tokens: list[str], i: int) -> tuple[str, int] | None:
    """The verb starting at tokens[i], and how many tokens it spans."""
    for size in (2, 1):
        words = " ".join(tokens[i : i + size])
        if len(tokens[i : i + size]) == size and words in VERBS:
            return ("ha" if words == "xa" else words), size
    return None


def parse(text: str, count: int) -> list[Answer] | None:
    """The answers, or None when the text is not a batch reply. ``count``
    is how many items the batch had; a number outside 1..count is not one."""
    tokens = _norm(text)
    if not tokens:
        return None
    if tokens[0] in _ALL and len(tokens) >= 2:
        found = _verb_at(tokens, 1)
        if found is None or 1 + found[1] != len(tokens) or found[0] not in ("ha", "yoq"):
            return None
        return [Answer(n, found[0]) for n in range(1, count + 1)]

    answers: list[Answer] = []
    i = 0
    while i < len(tokens):
        if not tokens[i].isdigit():
            return None
        number = int(tokens[i])
        if not 1 <= number <= count or any(a.number == number for a in answers):
            return None
        i += 1
        amount: list[str] = []
        while i < len(tokens) and _verb_at(tokens, i) is None:
            amount.append(tokens[i])
            i += 1
        if i >= len(tokens):
            return None
        verb, size = _verb_at(tokens, i)
        i += size
        if amount and verb != "ha":
            return None  # a correction is only ever followed by "ha"
        answers.append(Answer(number, verb, " ".join(amount) or None))
    return answers or None


def button_for(row, verb: str):
    """The button of ``row`` whose label starts with the verb's words."""
    want = VERBS[verb]
    for button in row:
        words = _norm(re.sub(r"^\d+\s*", "", button.text or ""))
        words = [w for w in words if re.search(r"[a-z]", w)]  # emojis out
        if tuple(words[: len(want)]) == want:
            return button
    return None
