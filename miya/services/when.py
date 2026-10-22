"""Day and time-of-day phrases, read deterministically (JV-03).

«Ertaga 14:00 gacha», «kechki 6 da», «soat 3 da», «завтра в 10»: the day and
the clock time the owner's words name, with no model involved. The records
parser (`/tuzat p12 ertaga 14:00`) reads time this way, and so does the command
mode's cross-check of a scheduled send (JV-20, `miya.jarvis.commands`): a time
the model proposes goes out only when the owner's own words name it here, so
one phrase means one moment everywhere.

The rule is that nothing is guessed. A phrase whose reading depends on
something the text does not say — two different days, two different times,
«soat 7» (morning or evening), «ertalab 15» — returns None, and the caller
asks or keeps what it had. A night hour after midnight said with a day —
«bugun kechasi 2», «ertaga kechasi 12» — is one of these: it may mean that
day's small hours or the night that follows it.

Hours without a part of the day are read on the working day, the way Uzbek
and Russian speakers mean them: 8 to 12 as written, 1 to 6 in the afternoon
(«soat 3» is 15:00), 13 to 23 and anything written with a leading zero
(«09:00») on the 24-hour clock. 7 has two equally likely readings and is
ambiguous, as is 0 without a leading zero. A part of the day settles it:
«ertalab 7» is 07:00, «kechki 7» is 19:00.

Everything here is pure: no database, no network, no settings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

from miya.services.text import fold_apostrophes, to_latin

# The relative day words, in the order the owner uses them; records.parse_date
# reads the same table, so `/tuzat p7 ertaga` and a phrase agree.
RELATIVE_DAYS = {"bugun": 0, "ertaga": 1, "indinga": 2, "kecha": -1}
WEEKDAYS = {
    "dushanba": 0,
    "seshanba": 1,
    "chorshanba": 2,
    "payshanba": 3,
    "juma": 4,
    "shanba": 5,
    "yakshanba": 6,
}

Part = Literal["morning", "afternoon", "evening", "night"]

# Day words in the Latin form `_normalise` produces (Russian is transliterated
# by the same table: «завтра» is "zavtra"). Case endings are allowed after
# the stem: «ertagacha», «dushanbaga», «пятницу».
_DAY_WORDS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bbugun\w*"), 0),
    (re.compile(r"\b(?:ertaga|ertangi)\w*"), 1),
    (re.compile(r"\bindinga\w*"), 2),
    (re.compile(r"\b(?:kecha|kechagi)\b"), -1),
    (re.compile(r"\bsegodnya\w*"), 0),
    (re.compile(r"\bzavtra(?:shn\w*)?\b"), 1),
    (re.compile(r"\bposlezavtra\b"), 2),
    (re.compile(r"\bvchera\w*"), -1),
)
_WEEKDAY_WORDS: tuple[tuple[re.Pattern[str], int], ...] = (
    # "\bshanba" never matches inside «dushanba»: no word boundary there.
    *((re.compile(rf"\b{name}\w*"), weekday) for name, weekday in WEEKDAYS.items()),
    (re.compile(r"\bponedelnik\w*"), 0),
    (re.compile(r"\bvtornik\w*"), 1),
    (re.compile(r"\bsred[auy]\b"), 2),
    (re.compile(r"\bchetverg\w*"), 3),
    (re.compile(r"\bpyatnits\w*"), 4),
    (re.compile(r"\bsubbot\w*"), 5),
    (re.compile(r"\bvoskresen\w*"), 6),
)

# The parts of the day. «kecha» (yesterday) and «kechikdi» (late) are not
# here: only the exact time-of-day forms are.
_PART_WORDS: dict[Part, str] = {
    "morning": r"ertalab\w*|tongda|tonggi|utra|utrom",
    "afternoon": r"kunduzi|tushdan\s+keyin|dnya|dnyom|dnem",
    "evening": r"kechki|kechqurun\w*|kechga|vechera|vecherom",
    "night": r"kechasi|tunda|tungi|nochi|nochyu",
}


def _parts(*, with_dnya: bool) -> str:
    """The part-of-day alternation, one named group per part. «дня» counts
    only right after the number («в 3 дня»): elsewhere «3 дня» is three days
    far more often than three in the afternoon."""
    groups = []
    for part, words in _PART_WORDS.items():
        if not with_dnya:
            words = words.replace("|dnya", "")
        groups.append(f"(?P<{part}>{words})")
    return "|".join(groups)


_PART_BEFORE = _parts(with_dnya=False)
_PART_AFTER = _parts(with_dnya=True)
# A part of the day said apart from the number.
_LOOSE_PART = re.compile(rf"\b(?:{_PART_BEFORE})\b")

_HOUR = r"(?P<hour>\d{1,2})"
_MINUTE = r"(?:[:.](?P<minute>\d{2}))?"
_PREP = r"(?:v|k|do|okolo)"
# A number followed by one of these is a quantity, not a clock time:
# «в 10 раз», «до 5 числа», «3 soat 5 daqiqa».
_NOT_A_CLOCK = (
    r"(?!\s*(?:raz|km|kg|sht|%|mln|ming|tys|dollar|sum|so'm|chisl|kun|den\b|"
    r"nedel|mes|god|let|chel|minut|daqiqa|sek|ta\b|-))"
)

# Each pattern is one way of saying a time. They are tried in this order and
# each match is blanked out before the next pattern runs, so «kechki soat 6»
# is one time, not a part of the day plus a bare «soat 6».
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # «kechki 6», «ertalab soat 9:30», «вечером в 6».
    re.compile(
        rf"\b(?:{_PART_BEFORE})\s+(?:{_PREP}\s+)?(?:soat\s*)?"
        rf"{_HOUR}{_MINUTE}(?!\d){_NOT_A_CLOCK}"
    ),
    # «в 6 вечера», «к 10 часам утра».
    re.compile(
        rf"\b{_PREP}\s+{_HOUR}{_MINUTE}(?!\d)\s*(?:chas\w*\s+)?(?:{_PART_AFTER})\b"
    ),
    # «soat 14», «soat 3 da», «soat 14.00 gacha».
    # A number right before «soat» makes it a duration: «3 soat 5 daqiqa».
    re.compile(rf"(?<!\d)(?<!\d\s)\bsoat\s*{_HOUR}{_MINUTE}(?!\d){_NOT_A_CLOCK}"),
    # «в 10», «к 14:00», «до 18».
    re.compile(rf"\b{_PREP}\s+{_HOUR}{_MINUTE}(?!\d){_NOT_A_CLOCK}"),
    # «14:00», «9:30».
    re.compile(r"(?<![\d:.])(?P<hour>\d{1,2}):(?P<minute>\d{2})(?![\d:])"),
    # «18 gacha», «5gacha» (by that hour). «5-gacha» is a date and stays out.
    re.compile(r"(?<![\d.,:-])(?P<hour>\d{1,2})\s*gacha\b"),
)


# Two numbers offered as alternatives or a range — «soat 3 yoki 4 da»,
# «5-6 da», «в 3 или 4» — name no single time.
_ALTERNATIVE = re.compile(
    r"(?<![\d.,-])\d{1,2}(?:[:.]\d{2})?\s*(?:yoki|ili|-|–)\s*\d{1,2}(?![\d.,-])"
)


@dataclass(frozen=True, slots=True)
class When:
    """What a phrase names: a day, a clock time, or both."""

    day: date | None
    at: time | None

    def moment(self, tz: tzinfo) -> datetime | None:
        """The local moment, when both the day and the time are known."""
        if self.day is None or self.at is None:
            return None
        return datetime.combine(self.day, self.at, tzinfo=tz)


def next_weekday(today: date, weekday: int) -> date:
    """The next such weekday strictly after today: «dushanba» said on a
    Monday is next week's, as `/tuzat` has always read it."""
    ahead = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead)


def _normalise(text: str) -> str:
    """NFKC, apostrophes folded, lower case, Cyrillic in Latin."""
    folded = fold_apostrophes(unicodedata.normalize("NFKC", text or ""))
    return to_latin(folded.casefold())


def _hour_on_part(hour: int, part: Part) -> int | None:
    """The 24-hour clock hour of «<part> <hour>», or None when they disagree."""
    if part == "morning":
        return hour if 1 <= hour <= 11 else None
    if part == "afternoon":
        if hour == 12 or 13 <= hour <= 18:
            return hour
        return hour + 12 if 1 <= hour <= 6 else None
    if part == "evening":
        if 13 <= hour <= 23:
            return hour
        return hour + 12 if 1 <= hour <= 11 else None
    # night
    if hour in (0, 12):
        return 0
    if 1 <= hour <= 5 or 21 <= hour <= 23:
        return hour
    return hour + 12 if 9 <= hour <= 11 else None


def _bare_hour(hour: int, written: str) -> int | None:
    """An hour said without a part of the day, on the working day."""
    if len(written) == 2 and written.startswith("0"):
        return hour  # «09:00»: a 24-hour clock reading
    if 13 <= hour <= 23 or 8 <= hour <= 12:
        return hour
    if 1 <= hour <= 6:
        return hour + 12
    return None  # 7 and a bare 0: two readings


# The hours after midnight that «kechasi 2» names: before dawn.
_DAWN = time(6, 0)
_NIGHT_WORD = re.compile(rf"\b(?:{_PART_WORDS['night']})\b")


def is_small_hours_of_a_night(text: str, at: time) -> bool:
    """Whether «<night word> <at>» names an hour after midnight. Said with a
    day («bugun kechasi 2») it has two readings: the small hours of that day,
    already past by its evening, or of the night after it."""
    return at < _DAWN and _NIGHT_WORD.search(_normalise(text)) is not None


def _part_of(match: re.Match[str]) -> Part | None:
    groups = match.groupdict()
    for part in _PART_WORDS:
        if groups.get(part):
            return part
    return None


class _Ambiguous(Exception):
    """Raised inside the scan: the text names a time it does not settle."""


def _scan_times(text: str) -> list[time]:
    """Every clock time the text names, in order; raises _Ambiguous."""
    work = text
    found: list[tuple[str, int, int, Part | None]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(work):
            hour_text = match.group("hour")
            hour = int(hour_text)
            minute = int(match.groupdict().get("minute") or 0)
            if hour > 23 or minute > 59:
                raise _Ambiguous
            found.append((hour_text, hour, minute, _part_of(match)))
        work = pattern.sub(lambda m: " " * len(m.group(0)), work)
    if not found:
        return []
    if _ALTERNATIVE.search(text):
        raise _Ambiguous
    # A part of the day said apart from the number («kechqurun gaplashamiz,
    # soat 7 da») settles the hours that came without one; two different
    # parts left over settle nothing.
    loose = {
        part
        for match in _LOOSE_PART.finditer(work)
        if (part := _part_of(match)) is not None
    }
    if len(loose) > 1:
        raise _Ambiguous
    default = next(iter(loose), None)
    times: list[time] = []
    for hour_text, hour, minute, part in found:
        part = part or default
        resolved = (
            _hour_on_part(hour, part) if part is not None else _bare_hour(hour, hour_text)
        )
        if resolved is None:
            raise _Ambiguous
        times.append(time(resolved, minute))
    return times


def _scan_days(text: str, today: date) -> list[date]:
    days = [
        today + timedelta(days=offset)
        for pattern, offset in _DAY_WORDS
        for _ in pattern.finditer(text)
    ]
    days += [
        next_weekday(today, weekday)
        for pattern, weekday in _WEEKDAY_WORDS
        for _ in pattern.finditer(text)
    ]
    return days


def _one(values: list[time]) -> time | None:
    distinct = set(values)
    if len(distinct) > 1:
        raise _Ambiguous
    return next(iter(distinct), None)


def parse_time(text: str) -> time | None:
    """The one clock time the text names; None when it names none, names two
    different ones, or names one it does not settle («soat 7»)."""
    try:
        return _one(_scan_times(_normalise(text)))
    except _Ambiguous:
        return None


def parse(text: str, *, today: date) -> When | None:
    """The day and the time the text names, either of which may be absent.

    None when the text names neither, or when either is ambiguous: two
    different days («ertaga, yo'q, dushanba» on a Thursday), two different
    times, a time with two readings, or a day with a night hour after
    midnight («bugun kechasi 2»). A day without a time is a day; a
    time without a day is a time, and the caller decides what day it means.
    """
    normal = _normalise(text)
    try:
        at = _one(_scan_times(normal))
    except _Ambiguous:
        return None
    days = set(_scan_days(normal, today))
    if len(days) > 1:
        return None
    day = next(iter(days), None)
    if day is None and at is None:
        return None
    if day is not None and at is not None and is_small_hours_of_a_night(normal, at):
        return None
    return When(day=day, at=at)


__all__ = [
    "RELATIVE_DAYS",
    "WEEKDAYS",
    "When",
    "is_small_hours_of_a_night",
    "next_weekday",
    "parse",
    "parse_time",
]
