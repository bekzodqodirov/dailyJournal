"""Export the owner's labelled payment texts for parser regression tests (WP-82).

    python -m miya.tools.payments_corpus --out /data/exports/payments.jsonl [--all]

One JSON object per line: {text, channel, verdict, reason, owner_label}.
Card numbers next to a mask character become "*0000" and phone numbers are
masked, but merchants and names stay: the file is server-only and never
committed. Only rows the owner labelled (booked from /tekshir, marked
"not money", voided) are written unless --all is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import sqlalchemy as sa

from miya.db.models import Interaction
from miya.db.session import engine, session_scope

# A card mask: "*1234", "**** 1234", "8600 **** **** 1234", "xx1234".
_CARD = re.compile(r"(?:[*xX•]+[\s-]?)+\d{4,}|\d{4,}(?=[\s-]?[*xX•])")
# A phone number: +998…, 998…, or 9 digits after a space, dashes allowed.
_PHONE = re.compile(r"\+?\b(?:998)?[\s-]?\(?\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b")


def anonymise(text: str) -> str:
    """Mask card digits and phone numbers; keep everything else."""
    text = _CARD.sub("*0000", text or "")
    return _PHONE.sub("+998XXXXXXXXX", text)


def row_of(interaction: Interaction) -> dict:
    money = ((interaction.media or {}).get("money") or {}) if interaction.media else {}
    return {
        "text": anonymise(interaction.raw_text or ""),
        "channel": money.get("channel"),
        "verdict": money.get("verdict"),
        "reason": money.get("reason"),
        "owner_label": money.get("owner_label"),
    }


async def export(out: Path, *, include_all: bool = False) -> int:
    stmt = sa.select(Interaction).where(Interaction.media["money"].isnot(None))
    if not include_all:
        stmt = stmt.where(Interaction.media["money"].has_key("owner_label"))
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with session_scope() as session:
        rows = await session.scalars(stmt.order_by(Interaction.id))
        with out.open("w", encoding="utf-8") as fh:
            for interaction in rows:
                fh.write(json.dumps(row_of(interaction), ensure_ascii=False) + "\n")
                written += 1
    out.chmod(0o600)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/data/exports/payments.jsonl")
    parser.add_argument("--all", action="store_true", help="unlabelled rows too")
    args = parser.parse_args(argv)

    async def _run() -> int:
        try:
            return await export(Path(args.out), include_all=args.all)
        finally:
            await engine.dispose()

    written = asyncio.run(_run())
    print(f"{written} row(s) written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
