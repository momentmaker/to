"""Generic article extraction: httpx fetch → readability-lxml → trafilatura fallback.

The "fallback" trips when readability returns empty or very short main-content
(e.g. on JS-heavy pages or unusual DOM structures). Site-specific scrapers
(zyte/hn/exa) are layered on top of this in their own modules.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (to-commonplace-bot)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
_MIN_READABLE_CHARS = 400
_MAX_HTML_BYTES = 5_000_000  # 5 MB cap


@dataclass
class ExtractedArticle:
    title: str | None
    text: str
    method: str  # 'readability' | 'trafilatura' | 'raw'


async def fetch_html(url: str, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None) -> str:
    owned = client is None
    if owned:
        client = httpx.AsyncClient(headers=_DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.content
        if len(content) > _MAX_HTML_BYTES:
            raise ValueError(f"response too large ({len(content)} bytes)")
        # Decode defensively — mis-declared charsets shouldn't kill a capture.
        encoding = resp.encoding or "utf-8"
        return content.decode(encoding, errors="replace")
    finally:
        if owned:
            await client.aclose()


def _extract_readability(html: str) -> ExtractedArticle | None:
    try:
        from readability import Document
    except ImportError:  # pragma: no cover
        return None
    try:
        doc = Document(html)
        summary_html = doc.summary(html_partial=True)
        title = doc.short_title()
        # Strip tags cheaply
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", summary_html)
        text = _re.sub(r"\s+", " ", text).strip()
        if len(text) < _MIN_READABLE_CHARS:
            return None
        return ExtractedArticle(title=title, text=text, method="readability")
    except Exception as e:
        log.debug("readability failed: %s", e)
        return None


def _extract_trafilatura(html: str) -> ExtractedArticle | None:
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return None
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        if not text or len(text.strip()) < 40:
            return None
        metadata = trafilatura.extract_metadata(html)
        title = metadata.title if metadata else None
        return ExtractedArticle(title=title, text=text.strip(), method="trafilatura")
    except Exception as e:
        log.debug("trafilatura failed: %s", e)
        return None


def _raw_text_fallback(html: str) -> str:
    """Last-resort plain-text when readability + trafilatura both fail.

    Strips tags so downstream LLM ingest doesn't waste tokens on HTML markup.
    """
    import re as _re
    # Drop script/style blocks entirely
    cleaned = _re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=_re.DOTALL | _re.IGNORECASE)
    cleaned = _re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:2000]


async def extract_article(
    url: str,
    *,
    html: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ExtractedArticle:
    """Fetch and extract. `html` can be provided to skip fetching (used by tests
    and by scrapers that fetched via alternate transport like Zyte).
    """
    if html is None:
        html = await fetch_html(url, client=client)

    # Run synchronous extractors off the event loop.
    result = await asyncio.to_thread(_extract_readability, html)
    if result is not None:
        return result
    result = await asyncio.to_thread(_extract_trafilatura, html)
    if result is not None:
        return result
    return ExtractedArticle(title=None, text=_raw_text_fallback(html), method="raw")
