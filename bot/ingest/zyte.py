"""Zyte API scraper for JS-heavy pages.

Lifted from the housingassist pattern (bot/listing_check.py:138-160):
  POST https://api.zyte.com/v1/extract
  BasicAuth = (ZYTE_API_KEY, "")
  body: {url, browserHtml: true}
  response: {browserHtml: "<full rendered HTML>"}

We pipe the rendered HTML into our generic extractor so downstream code doesn't
care where the HTML came from.
"""

from __future__ import annotations

import logging

import httpx

from bot.ingest.generic import ExtractedArticle, extract_article

log = logging.getLogger(__name__)

_ZYTE_URL = "https://api.zyte.com/v1/extract"
_TIMEOUT = 30.0


async def fetch_html_via_zyte(
    url: str, *, api_key: str, client: httpx.AsyncClient | None = None,
) -> str | None:
    if not api_key:
        return None
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.post(
            _ZYTE_URL,
            json={"url": url, "browserHtml": True},
            auth=(api_key, ""),
        )
        resp.raise_for_status()
        data = resp.json()
        html = data.get("browserHtml")
        return html if html else None
    except Exception as e:
        log.warning("zyte fetch failed for %s: %s", url, e)
        return None
    finally:
        if owned:
            await client.aclose()


async def extract_with_zyte(
    url: str, *, api_key: str, client: httpx.AsyncClient | None = None,
) -> ExtractedArticle | None:
    """Fetch via Zyte and run the generic extractor on the rendered HTML."""
    html = await fetch_html_via_zyte(url, api_key=api_key, client=client)
    if html is None:
        return None
    return await extract_article(url, html=html)
