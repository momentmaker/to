"""Post-ingest LLM processing: extract {title, tags, quotes, summary}.

Does NOT carry the orchurator voice — this is structured extraction, cache the
SYSTEM_INGEST prefix and let the model return deterministic JSON.
"""

from __future__ import annotations

import json
import logging
import re

import aiosqlite

from bot.config import Settings
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.prompts import SYSTEM_INGEST

log = logging.getLogger(__name__)


_MAX_CONTENT_CHARS = 30_000  # cap long articles before sending to LLM


def _coerce_json(raw: str) -> dict | None:
    """LLMs sometimes wrap JSON in code fences or prose. Extract first JSON object."""
    if not raw:
        return None
    # Strip code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fallback: find the outermost {...} block
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _normalize_processed(obj: dict) -> dict:
    """Pin the shape: title(str), tags(list[str]), quotes(list[str]), summary(str)."""
    title = obj.get("title")
    if not isinstance(title, str):
        title = ""
    tags: list[str] = []
    seen: set[str] = set()
    for t in obj.get("tags") or []:
        if not isinstance(t, (str, int)):
            continue
        norm = str(t).strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        tags.append(norm)
    quotes = [
        q.strip() for q in (obj.get("quotes") or [])
        if isinstance(q, str) and q.strip()
    ]
    summary = obj.get("summary")
    if not isinstance(summary, str):
        summary = ""
    return {"title": title, "tags": tags, "quotes": quotes, "summary": summary}


async def process_capture(
    *,
    content: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> dict:
    """Run SYSTEM_INGEST over `content` and return the normalized processed JSON.

    Content is truncated to _MAX_CONTENT_CHARS before sending.
    """
    truncated = content[:_MAX_CONTENT_CHARS]
    response = await call_llm(
        purpose="ingest",
        system_blocks=[SYSTEM_INGEST],
        messages=[Message(role="user", content=truncated)],
        max_tokens=1024,
        settings=settings,
        providers=providers,
        conn=conn,
    )
    obj = _coerce_json(response.text) or {}
    return _normalize_processed(obj)


async def mark_processed(
    conn: aiosqlite.Connection, *, capture_id: int, processed: dict
) -> None:
    await conn.execute(
        "UPDATE captures SET processed = ?, status = 'processed' WHERE id = ?",
        (json.dumps(processed), capture_id),
    )
    await conn.commit()


async def mark_failed(
    conn: aiosqlite.Connection, *, capture_id: int, error: str
) -> None:
    await conn.execute(
        "UPDATE captures SET status = 'failed', error = ? WHERE id = ?",
        (error[:500], capture_id),
    )
    await conn.commit()
