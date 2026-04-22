"""Exa contents endpoint for X/Reddit URLs.

X and Reddit block scraping heavily. Exa's /contents endpoint returns the
page's extracted text without us needing to solve their bot defenses.

  https://docs.exa.ai/reference/get-contents
  POST https://api.exa.ai/contents  {ids: [url], text: true, livecrawl: "always"}

`livecrawl: "always"` + `livecrawlTimeout: 10000` is what makes fresh
tweets work. "fallback" falls back to cache when live fails silently,
and "fallback" from-no-cache-to-failed-livecrawl returns an empty
result. "always" forces a fresh fetch every time and the longer timeout
gives Exa enough runway to crawl. Higher per-request cost than
cache-only, but the alternative is empty results on freshly-posted
content.
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
            json={
                "ids": [url],
                "text": True,
                "livecrawl": "always",
                "livecrawlTimeout": 10000,
            },
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
