"""Import the owner's client-code list: make import-clients FILE=… [APPLY=1].

Without --apply it only prints what would happen; with it, the attach and
create rows are written in one transaction. Conflicts are listed, never
changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from miya.db.session import engine, session_scope
from miya.services import client_import


def render(plan: client_import.ImportPlan, applied: dict | None) -> str:
    counts = plan.counts()
    lines = [
        "attach: {attach}  create: {create}  same: {same}  "
        "conflict: {conflict}  bad: {bad}".format(**counts)
    ]
    for planned in plan.conflict:
        lines.append(
            f"  conflict line {planned.row.line_no}: {planned.code} "
            f"file={planned.row.name!r} other={planned.against!r}"
        )
    for planned in plan.bad:
        lines.append(f"  bad line {planned.row.line_no}: {planned.row.code_raw!r}")
    if applied is None:
        lines.append("dry run: nothing written (add --apply)")
    else:
        lines.append(f"written: {applied['attach']} code(s), {applied['create']} new")
    return "\n".join(lines)


async def _main(path: str, apply: bool) -> str:
    try:
        rows = client_import.read_rows(path)
        async with session_scope() as session:
            plan = await client_import.plan_import(session, rows)
            applied = None
            if apply:
                applied = await client_import.apply_import(session, plan, by="import")
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    return render(plan, applied)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="CSV or .xlsx with kod / ism columns")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(_main(args.file, args.apply)))
    except client_import.BadFile as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
