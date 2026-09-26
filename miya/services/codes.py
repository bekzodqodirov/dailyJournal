"""Client codes (GS367) and waybill numbers (YW26-004715), WP-29.

The owner's clients carry a code in their names ("GS367 Akmal"), and fuzzy
name matching used to treat GS368 as a spelling of GS367 — a debt could land
on the wrong client. Codes are therefore read by one regex and compared
exactly, never fuzzily. Pure functions only.
"""

from __future__ import annotations

import functools
import re

from miya.config import settings

# Each Latin prefix letter also accepts its Cyrillic look-alike: a phone
# keyboard in the other layout still writes the same code.
LOOKALIKE = {
    "G": "GГ",
    "S": "SС",
    "Y": "YУ",
    "W": "W",
    "K": "KК",
    "A": "AА",
    "B": "BВ",
    "C": "CС",
    "E": "EЕ",
    "H": "HН",
    "M": "MМ",
    "O": "OО",
    "P": "PР",
    "T": "TТ",
    "X": "XХ",
}
# Grammatical endings a code may carry: "GS367ga", "GS367ning".
UZ_SUFFIXES = frozenset(
    {
        "ga",
        "ka",
        "qa",
        "ni",
        "ning",
        "niki",
        "da",
        "dan",
        "dagi",
        "lar",
        "larga",
        "larni",
        "larning",
        "га",
        "ка",
        "қа",
        "ни",
        "нинг",
        "ники",
        "да",
        "дан",
        "даги",
        "лар",
        "ларга",
        "ларни",
        "ларнинг",
    }
)
_LETTERS = "0-9A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ'"
_TAIL = "A-Za-zА-Яа-яЎўҚқҒғҲҳ'"


def _prefixes(raw: str) -> tuple[str, ...]:
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def _prefix_class(prefix: str) -> str:
    return "".join(f"[{re.escape(LOOKALIKE.get(ch, ch))}]" for ch in prefix)


@functools.lru_cache(maxsize=8)
def _client_code_re(prefixes: tuple[str, ...], digits: int) -> re.Pattern[str]:
    alternatives = "|".join(_prefix_class(p) for p in prefixes)
    return re.compile(
        rf"(?<![{_LETTERS}])(?P<p>{alternatives})[ \t]{{0,2}}[-–—.#№:_]?[ \t]{{0,2}}"
        rf"(?P<d>\d{{1,{digits}}})(?!\d)(?P<tail>[{_TAIL}]*)",
        re.IGNORECASE,
    )


def client_code_re() -> re.Pattern[str]:
    """CLIENT_CODE_RE for the configured prefixes and digit count."""
    return _client_code_re(
        _prefixes(settings.client_code_prefixes), settings.client_code_max_digits
    )


def _canonical_prefix(matched: str) -> str:
    folded = matched.upper()
    for prefix in _prefixes(settings.client_code_prefixes):
        if len(prefix) == len(folded) and all(
            ch in LOOKALIKE.get(p, p) + LOOKALIKE.get(p, p).lower()
            for ch, p in zip(folded, prefix, strict=True)
        ):
            return prefix
    return folded


def _canonical(match: re.Match[str]) -> str:
    digits = match.group("d")
    if settings.client_code_strip_leading_zeros:
        digits = digits.lstrip("0") or "0"
    return _canonical_prefix(match.group("p")) + digits


def _counts(match: re.Match[str]) -> bool:
    tail = match.group("tail")
    return tail == "" or tail.lower() in UZ_SUFFIXES


def canonical_client_code(text: str) -> str | None:
    """'gs-0367' → 'GS367'; None unless the whole text is exactly one code."""
    stripped = (text or "").strip()
    match = client_code_re().fullmatch(stripped)
    if match is None or match.group("tail"):
        return None
    return _canonical(match)


def find_client_codes(text: str) -> list[str]:
    """Every code in free text, canonical, de-duplicated, in order."""
    found: list[str] = []
    for match in client_code_re().finditer(text or ""):
        if _counts(match):
            code = _canonical(match)
            if code not in found:
                found.append(code)
    return found


_EDGE = re.compile(r"^[\s()\[\]—–\-,:]+|[\s()\[\]—–\-,:]+$")


def split_codes(name: str) -> tuple[list[str], str]:
    """(codes, the name without them): 'Akmal (GS367)' → (['GS367'], 'Akmal')."""
    codes: list[str] = []
    kept: list[str] = []
    last = 0
    for match in client_code_re().finditer(name or ""):
        if not _counts(match):
            continue
        code = _canonical(match)
        if code not in codes:
            codes.append(code)
        kept.append(name[last : match.start()])
        last = match.end()
    kept.append((name or "")[last:])
    rest = " ".join(" ".join(kept).split())
    rest = re.sub(r"\(\s*\)", " ", rest)
    rest = " ".join(_EDGE.sub("", rest).split())
    return codes, rest


def strip_codes(text: str) -> str:
    return split_codes(text)[1]


@functools.lru_cache(maxsize=8)
def _waybill_re(prefixes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(p) for p in prefixes)
    return re.compile(
        rf"(?<![0-9A-Za-z])(?P<p>{alternatives})[ \t]?(?P<y>\d{{2}})[ \t]?[-–—]?[ \t]?"
        r"(?P<n>\d{4,8})(?!\d)",
        re.IGNORECASE,
    )


def waybill_re() -> re.Pattern[str]:
    return _waybill_re(_prefixes(settings.waybill_prefixes))


def canonical_waybill(match: re.Match[str]) -> str:
    return f"{match.group('p').upper()}{match.group('y')}-{match.group('n')}"


def find_waybills(text: str) -> list[str]:
    found: list[str] = []
    for match in waybill_re().finditer(text or ""):
        waybill = canonical_waybill(match)
        if waybill not in found:
            found.append(waybill)
    return found


@functools.lru_cache(maxsize=8)
def _join_re(prefixes: tuple[str, ...], digits: int) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(p.lower()) for p in prefixes)
    return re.compile(rf"\b({alternatives})[\s\-_#№]*(\d{{1,{digits}}})\b")


def join_pattern() -> re.Pattern[str]:
    """For text.normalise_for_search: 'gs 367' → 'gs367' (lower-case, Latin)."""
    return _join_re(
        _prefixes(settings.client_code_prefixes), settings.client_code_max_digits
    )
