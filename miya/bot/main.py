"""Assistant bot entrypoint (aiogram) — implemented in Phase 1."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "Assistant bot hali yozilmagan — Phase 1 da keladi.\n"
        "The assistant bot arrives in Phase 1 (spec §12).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
