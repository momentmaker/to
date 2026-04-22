"""Exa /contents endpoint for Reddit URLs.

Reddit blocks scraping heavily; Exa's extracted text does the work for us.

  https://docs.exa.ai/reference/get-contents
  POST https://api.exa.ai/contents  {ids: [url], text: true}

**X/Twitter is NOT routed here** — empirically, Exa cannot fetch fresh
tweets: `/contents` returns an empty results list regardless of livecrawl
setting (X blocks their crawler), and their `category: "tweet"` filter
has been deprecated. `bot/ingest/router.py` short-circuits X URLs to a
bare-URL capture with a helpful `scrape_error` explaining the limitation.
See the README section on X captures for the recommended user workflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_EXA_URL = "https://api.exa.ai/contents"
_TIMEOUT = 20.0


@dataclass
class ExaContent:
    url: str
    title: str | None
    author: str | None
    text: str


async def fetch_content(
    url: str, *, api_key: str, client: httpx.AsyncClient | None = None,
) -> ExaContent | None:
    if not api_key:
        return None
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.post(
            _EXA_URL,
            json={"ids": [url], "text": True},
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        text = r.get("text") or ""
        if not text.strip():
            return None
        return ExaContent(
            url=r.get("url") or url,
            title=r.get("title"),
            author=r.get("author"),
            text=text.strip(),
        )
    except Exception as e:
        log.warning("exa fetch failed for %s: %s", url, e)
        return None
    finally:
        if owned:
            await client.aclose()
