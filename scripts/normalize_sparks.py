"""One-time backfill for `sparks.md` blank-line spacing.

The Claude Code Routine that previously appended sparks ignored the
Python normalization block in its prompt and used shell append, leading
to entries from 2026-04-23 onward sharing a single Markdown paragraph.

Idempotent: running on an already-correct file produces the same bytes.

Usage:
    python scripts/normalize_sparks.py path/to/sparks.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s")


def normalize_sparks_text(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _ENTRY_RE.match(line):
            # Ensure exactly one blank line precedes every entry, except
            # the very first one (which keeps its existing position).
            if out and out[-1] != "":
                out.append("")
        out.append(line)
    rendered = "\n".join(out).rstrip("\n") + "\n"
    return rendered


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: normalize_sparks.py <sparks.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")
    out = normalize_sparks_text(text)
    if out == text:
        print(f"{path}: already normalized")
        return 0
    path.write_text(out, encoding="utf-8")
    print(f"{path}: normalized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
