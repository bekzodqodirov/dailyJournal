"""Text normalisation shared by the detectors and the userbot.

One table for the apostrophes people type for oʻ / gʻ, so an alias in .env,
a question in a chat and a transliterated name all fold the same way.
Extend it here and nowhere else.
"""

from __future__ import annotations

# Every apostrophe-like character an Uzbek keyboard or phone produces for the
# tutuq belgisi, folded to the ASCII one:
#   ʻ U+02BB modifier letter turned comma   (the official Uzbek Latin form)
#   ʼ U+02BC modifier letter apostrophe     (most Android keyboards)
#   ‘ U+2018 left single quotation mark     (smart quotes)
#   ’ U+2019 right single quotation mark    (smart quotes, iOS)
#   ` U+0060 grave accent                   (typed on a Latin layout)
#   ʹ U+02B9 modifier letter prime          (some transliteration keyboards)
APOSTROPHES = str.maketrans(
    {
        "ʻ": "'",
        "ʼ": "'",
        "‘": "'",
        "’": "'",
        "`": "'",
        "ʹ": "'",
    }
)


def fold_apostrophes(text: str) -> str:
    """Pure: the same text with every apostrophe variant as ASCII ``'``."""
    return text.translate(APOSTROPHES)
