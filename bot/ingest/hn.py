"""Hacker News scraping via the Firebase API.

Fetches the story + up to top-10 first-level comments. HN's own API is free,
fast, and doesn't need scraping.

  https://github.com/HackerNews/API
  https://hacker-news.firebaseio.com/v0/item/{id}.json
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

_BASE = "https://hacker-news.firebaseio.com/v0"
_TIMEOUT = 10.0
_MAX_COMMENTS = 10


@dataclass
class HnStory:
    id: int
    title: str | None
    url: str | None
    by: str | None
    score: int | None
    text: str | None           # "Ask HN" / "Show HN" body, if any
    comments: list[dict]       # top-N first-level comments


def extract_item_id(url: str) -> int | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    ids = qs.get("id")
    if ids and ids[0].isdigit():
        return int(ids[0])
    # Fallback: /item/12345 or /items/12345 (Algolia)
    m = re.search(r"/items?/(\d+)", parsed.path)
    if m:
        return int(m.group(1))
    return None


def _strip_html(s: str | None) -> str | None:
    if not s:
        return s
    # HN comment bodies come as HTML. Plain-text conversion suffices for our use.
    s = re.sub(r"<p>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


async def _fetch_item(client: httpx.AsyncClient, item_id: int) -> dict | None:
    try:
        r = await client.get(f"{_BASE}/item/{item_id}.json", timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug("hn item %s fetch failed: %s", item_id, e)
        return None


async def fetch_story(item_id: int, *, client: httpx.AsyncClient | None = None) -> HnStory | None:
    owned = client is None
    if owned:
        client = httpx.AsyncClient()
    try:
        item = await _fetch_item(client, item_id)
        if item is None:
            return None
        kid_ids = item.get("kids") or []
        # Fetch up to N first-level comments concurrently
        kid_ids = kid_ids[:_MAX_COMMENTS]
        comments_raw = await asyncio.gather(
            *[_fetch_item(client, kid) for kid in kid_ids],
            return_exceptions=False,
        )
        comments: list[dict] = []
        for c in comments_raw:
            if c is None or c.get("deleted") or c.get("dead"):
                continue
            comments.append({
                "id": c.get("id"),
                "by": c.get("by"),
                "text": _strip_html(c.get("text")),
                "time": c.get("time"),
            })
        return HnStory(
            id=item.get("id") or item_id,
            title=item.get("title"),
            url=item.get("url"),
            by=item.get("by"),
            score=item.get("score"),
            text=_strip_html(item.get("text")),
            comments=comments,
        )
    finally:
        if owned:
            await client.aclose()


def to_processing_content(story: HnStory) -> str:
    """Flatten an HN story + comments into a single blob the LLM can read."""
    parts: list[str] = []
    if story.title:
        parts.append(story.title)
    if story.text:
        parts.append(story.text)
    if story.url:
        parts.append(f"[link: {story.url}]")
    if story.comments:
        parts.append("\n--- top comments ---")
        for c in story.comments:
            body = c.get("text") or ""
            by = c.get("by") or "anon"
            parts.append(f"> {by}: {body}")
    return "\n\n".join(parts)


def to_payload(story: HnStory) -> dict[str, Any]:
    return {
        "story": {
            "id": story.id,
            "title": story.title,
            "url": story.url,
            "by": story.by,
            "score": story.score,
            "text": story.text,
        },
        "comments": story.comments,
    }
