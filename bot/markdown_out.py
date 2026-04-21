"""Render a capture row as Markdown with TOML frontmatter.

Structure:

    +++
    id = 42
    kind = "url"
    source = "article"
    url = "https://..."
    title = "The Piece"
    tags = ["a", "b"]
    captured_at = "2026-04-21T14:03:00Z"
    local_date = "2026-04-21"
    iso_week = "2026-W16"
    week_idx = 1888
    +++

    > quote 1
    > quote 2

    one-sentence summary.

    ---

    original raw text here

    ## why?

    > _2026-04-21T14:13:00Z_
    >
    > because the title caught me

"why" captures render **inline inside their parent's file**, not as a separate
file. The parent's github_sha reflects the file after the last why was added.
"""

from __future__ import annotations

import json
import re
from typing import Any

import tomli_w


_MAX_SLUG_LEN = 48


def _row_get(row: Any, key: str) -> Any:
    """Field access that works with both aiosqlite.Row (index-only) and dicts."""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def make_slug(text: str) -> str:
    """URL-/filesystem-safe slug. Preserves ASCII letters/digits, collapses
    whitespace and other runs to single hyphens, caps length.
    """
    s = (text or "").strip().lower()
    # Keep unicode letters/digits for non-English captures; strip other punct.
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s)
    s = s.strip("-")
    if len(s) > _MAX_SLUG_LEN:
        s = s[:_MAX_SLUG_LEN].rstrip("-")
    return s or "untitled"


def file_path_for(row: Any) -> str:
    """GitHub path: `YYYY-wNN/YYYY-MM-DD-...md`. Plan mandates lowercase 'w'.

    Reflections are special-cased to a stable, scannable filename:
    `YYYY-MM-DD-reflection.md`. There's only one reflection per day, so no
    capture id is needed to disambiguate.
    """
    week_key = row["iso_week_key"]       # "2026-W16"
    week_lower = week_key.replace("W", "w")
    local_date = row["local_date"]       # "2026-04-21"

    if row["kind"] == "reflection":
        return f"{week_lower}/{local_date}-reflection.md"

    processed = _parse_json(_row_get(row, "processed"))
    title = (processed or {}).get("title") if processed else ""
    basis = title or (_row_get(row, "raw") or "") or "untitled"
    slug = make_slug(basis)

    # Include capture id so multiple captures in the same day don't collide.
    return f"{week_lower}/{local_date}-{row['id']:06d}-{slug}.md"


def render_capture_markdown(
    row: Any,
    *,
    why_children: list[Any] | None = None,
) -> str:
    """Emit the full Markdown string for a capture row.

    `why_children` is the ordered list of why-rows that belong to this
    capture. None/empty = no why section.
    """
    fm: dict[str, Any] = {
        "id": row["id"],
        "kind": row["kind"],
        "source": row["source"] or "",
        "captured_at": row["created_at"],
        "local_date": row["local_date"],
        "iso_week": row["iso_week_key"],
        "week_idx": row["fz_week_idx"],
    }
    url = _row_get(row, "url")
    if url:
        fm["url"] = url
    parent_id = _row_get(row, "parent_id")
    if parent_id:
        fm["parent_id"] = parent_id

    processed = _parse_json(_row_get(row, "processed")) or {}
    title = processed.get("title")
    if isinstance(title, str) and title.strip():
        fm["title"] = title.strip()
    tags = processed.get("tags")
    if isinstance(tags, list) and tags:
        fm["tags"] = [str(t) for t in tags if str(t)]

    frontmatter = "+++\n" + tomli_w.dumps(fm) + "+++\n"

    body_parts: list[str] = []

    quotes = processed.get("quotes") if isinstance(processed.get("quotes"), list) else []
    for q in quotes:
        if isinstance(q, str) and q.strip():
            body_parts.append("> " + q.strip().replace("\n", "\n> "))
    summary = processed.get("summary")
    if isinstance(summary, str) and summary.strip():
        body_parts.append(summary.strip())

    raw = _row_get(row, "raw")
    if isinstance(raw, str) and raw.strip():
        if body_parts:
            body_parts.append("---")
        body_parts.append(raw.strip())

    if why_children:
        body_parts.append("## why?")
        for child in why_children:
            ts = _row_get(child, "created_at") or ""
            child_raw = (_row_get(child, "raw") or "").strip().replace("\n", "\n> ")
            body_parts.append(f"> _{ts}_\n>\n> {child_raw}")

    body = "\n\n".join(body_parts).rstrip() + "\n"
    return frontmatter + "\n" + body


def _parse_json(raw: Any) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None
