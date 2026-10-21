"""Stall templates: what the client is told while the owner decides (§5.3).

A stall is never generated. It is one of the fixed texts below, keyed by
reason × script (Latin, Cyrillic, Russian) × honorific, written in the
owner's own register; the owner may override one (``owner_style_profiles.
stalls``, JV-14), and an override is used only while it passes the red-line
guard in the ``stall`` role. Every template passes that guard too (a test
pins it): a stall promises nothing, names no price, no time and no figure,
and invents no fact («I'm in a meeting»). A template, and an override that
passed the stall role, is also an approved shape for the statement allowlist
(``redlines.check_shapes``), so a stall may go out on its own once the
``stall`` intent graduates. At most one stall goes out per
open escalation; the responder (JV-16) holds that line.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from miya.jarvis import redlines
from miya.jarvis.redlines import Script

Reason = Literal["price", "deadline", "guarantee", "payment_claim", "general", "default"]
Honorific = Literal["aka", "neutral"]

REASONS: Final[tuple[Reason, ...]] = (
    "price",
    "deadline",
    "guarantee",
    "payment_claim",
    "general",
    "default",
)
SCRIPTS: Final[tuple[Script, ...]] = ("latin", "cyrillic", "russian")
HONORIFICS: Final[tuple[Honorific, ...]] = ("aka", "neutral")

# reason -> script -> honorific -> text. «aka» is how the owner addresses
# their clients; the neutral form drops it and says «Siz». Russian has one
# form: «вы» is already the polite address.
TEMPLATES: Final[Mapping[Reason, Mapping[Script, Mapping[Honorific, str]]]] = {
    "price": {
        "latin": {
            "aka": "Aka, narx bo'yicha hozir aniqlashtirib, o'zim sizga yozaman.",
            "neutral": "Narx bo'yicha hozir aniqlashtirib, o'zim Sizga yozaman.",
        },
        "cyrillic": {
            "aka": "Ака, нарх бўйича ҳозир аниқлаштириб, ўзим ёзаман.",
            "neutral": "Нарх бўйича ҳозир аниқлаштириб, ўзим Сизга ёзаман.",
        },
        "russian": {
            "aka": "Здравствуйте! По цене уточню и сам вам напишу.",
            "neutral": "Здравствуйте! По цене уточню и сам вам напишу.",
        },
    },
    "deadline": {
        "latin": {
            "aka": "Aka, muddati bo'yicha aniqlab, o'zim xabar beraman.",
            "neutral": "Muddati bo'yicha aniqlab, o'zim Sizga xabar beraman.",
        },
        "cyrillic": {
            "aka": "Ака, муддати бўйича аниқлаб, ўзим хабар бераман.",
            "neutral": "Муддати бўйича аниқлаб, ўзим Сизга хабар бераман.",
        },
        "russian": {
            "aka": "Здравствуйте! По срокам уточню и сам вам напишу.",
            "neutral": "Здравствуйте! По срокам уточню и сам вам напишу.",
        },
    },
    "guarantee": {
        "latin": {
            "aka": "Aka, aniq bilib, keyin o'zim aytaman.",
            "neutral": "Aniq bilib, keyin o'zim Sizga aytaman.",
        },
        "cyrillic": {
            "aka": "Ака, аниқ билиб, кейин ўзим айтаман.",
            "neutral": "Аниқ билиб, кейин ўзим Сизга айтаман.",
        },
        "russian": {
            "aka": "Здравствуйте! Всё уточню и сам вам напишу.",
            "neutral": "Здравствуйте! Всё уточню и сам вам напишу.",
        },
    },
    "payment_claim": {
        "latin": {
            "aka": "Rahmat, aka. Tekshirib, o'zim yozaman.",
            "neutral": "Rahmat. Tekshirib, o'zim Sizga yozaman.",
        },
        "cyrillic": {
            "aka": "Раҳмат, ака. Текшириб, ўзим ёзаман.",
            "neutral": "Раҳмат. Текшириб, ўзим Сизга ёзаман.",
        },
        "russian": {
            "aka": "Спасибо! Проверю и сам вам напишу.",
            "neutral": "Спасибо! Проверю и сам вам напишу.",
        },
    },
    "general": {
        "latin": {
            "aka": "Savolingizni oldim, aka. Aniqlashtirib, o'zim javob beraman.",
            "neutral": "Savolingizni oldim. Aniqlashtirib, o'zim Sizga javob beraman.",
        },
        "cyrillic": {
            "aka": "Саволингизни олдим, ака. Аниқлаштириб, ўзим жавоб бераман.",
            "neutral": "Саволингизни олдим. Аниқлаштириб, ўзим Сизга жавоб бераман.",
        },
        "russian": {
            "aka": "Здравствуйте! Уточню и сам вам отвечу.",
            "neutral": "Здравствуйте! Уточню и сам вам отвечу.",
        },
    },
    # The owner's own stall, for a draft the guard refused when the client's
    # message itself carried no red line.
    "default": {
        "latin": {
            "aka": "Aka, hozir buni aniqlashtirib, o'zim tez orada yozvoraman.",
            "neutral": "Hozir buni aniqlashtirib, o'zim Sizga tez orada yozvoraman.",
        },
        "cyrillic": {
            "aka": "Ака, ҳозир буни аниқлаштириб, ўзим тез орада ёзвораман.",
            "neutral": "Ҳозир буни аниқлаштириб, ўзим Сизга тез орада ёзвораман.",
        },
        "russian": {
            "aka": "Здравствуйте! Сейчас уточню и сам вам напишу.",
            "neutral": "Здравствуйте! Сейчас уточню и сам вам напишу.",
        },
    },
}

# Which stall answers which escalation reason (redlines signals and flags,
# escalations.reasons).
_REASON_OF: Final[Mapping[str, Reason]] = {
    "price": "price",
    "discount": "price",
    "payment_terms": "price",
    "deal": "price",
    "deadline": "deadline",
    "promise": "guarantee",
    "guarantee": "guarantee",
    "payment_claim": "payment_claim",
}


def reason_for(reasons: Iterable[str]) -> Reason:
    """The one stall for a set of escalation reasons. One family of reasons
    (price, discount and terms are one) gets its own stall; several
    families, or anything else (identity, complaint, legal, other people),
    get the general one. No reason at all is the owner's default stall."""
    given = [r for r in reasons if r != "injection"]
    if not given:
        return "default"
    families = {_REASON_OF.get(r, "general") for r in given}
    return families.pop() if len(families) == 1 else "general"


def honorific_of(value: str | None) -> Honorific:
    """«aka» when the owner calls this person aka (owner_style_people), else
    the neutral form."""
    return "aka" if (value or "").strip().casefold() in ("aka", "ака") else "neutral"


def template(reason: Reason, script: Script, honorific: Honorific) -> str:
    return TEMPLATES[reason][script][honorific]


def override_key(reason: Reason, script: Script, honorific: Honorific) -> str:
    """The key of an owner override in ``owner_style_profiles.stalls``."""
    return f"{reason}:{script}:{honorific}"


@dataclass(frozen=True, slots=True)
class Stall:
    text: str
    reason: Reason
    script: Script
    honorific: Honorific
    source: Literal["template", "owner"]
    verdict: redlines.Verdict


def choose(
    reasons: Iterable[str],
    *,
    script: Script | None,
    honorific: str | None,
    overrides: Mapping[str, object] | None = None,
) -> Stall:
    """The stall to queue for these escalation reasons, in the client's
    script and the owner's form of address for them. An owner override is
    used only when it passes the guard in the ``stall`` role; otherwise the
    fixed template is. Both are checked against the client's script: an
    override written in another script is not used."""
    reason = reason_for(reasons)
    written: Script = script or "latin"
    form = honorific_of(honorific)
    override = (overrides or {}).get(override_key(reason, written, form))
    if isinstance(override, str) and override.strip():
        # An override that passes the stall role is a stall for the
        # statement allowlist too (§5.2 step 3).
        verdict = redlines.check_outbound(
            override.strip(),
            "stall",
            client_script=written,
            stall_texts=(override.strip(),),
        )
        if verdict.passed:
            return Stall(override.strip(), reason, written, form, "owner", verdict)
    text = template(reason, written, form)
    verdict = redlines.check_outbound(text, "stall", client_script=written)
    return Stall(text, reason, written, form, "template", verdict)


def for_message(
    client_text: str,
    reasons: Iterable[str],
    *,
    honorific: str | None,
    overrides: Mapping[str, object] | None = None,
) -> Stall:
    """``choose`` in the script the client wrote ``client_text`` in."""
    return choose(
        reasons,
        script=redlines.script_of(client_text),
        honorific=honorific,
        overrides=overrides,
    )


def all_templates() -> list[tuple[Reason, Script, Honorific, str]]:
    return [
        (reason, script, form, TEMPLATES[reason][script][form])
        for reason in REASONS
        for script in SCRIPTS
        for form in HONORIFICS
    ]


__all__ = [
    "HONORIFICS",
    "REASONS",
    "SCRIPTS",
    "TEMPLATES",
    "Honorific",
    "Reason",
    "Stall",
    "all_templates",
    "choose",
    "for_message",
    "honorific_of",
    "override_key",
    "reason_for",
    "template",
]
