"""Classify an incoming Telegram message into a capture kind + dispatch URL scraping.

Stage 2 supports: text, url, voice, image. Voice + image wiring lands in Turn 3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from bot.config import Settings
from bot.ingest import exa, generic, hn, nitter, youtube, zyte
from bot.ingest.urls import classify_url, extract_url

log = logging.getLogger(__name__)

Kind = Literal["text", "url", "voice", "image"]


def classify_text(text: str) -> tuple[Kind, str | None]:
    """Return (kind, extracted_url). For messages containing a URL, kind='url'."""
    url = extract_url(text or "")
    if url is not None:
        return "url", url
    return "text", None


@dataclass
class UrlScrapeResult:
    """Output of scraping a URL. `content` is what the LLM will process."""
    source: str                       # hn|reddit|x|article
    payload: dict[str, Any]
    content: str
    error: str | None = None


async def scrape_url(url: str, *, settings: Settings) -> UrlScrapeResult:
    """Route a URL to the right scraper based on its shape.

    Never raises; on failure returns a result with `error` set and
    `content` falling back to the URL itself.
    """
    kind = classify_url(url)

    if kind == "hn":
        item_id = hn.extract_item_id(url)
        if item_id is not None:
            story = await hn.fetch_story(item_id)
            if story is not None:
                return UrlScrapeResult(
                    source="hn",
                    payload=hn.to_payload(story),
                    content=hn.to_processing_content(story),
                )
        return UrlScrapeResult(
            source="hn", payload={}, content=url, error="hn fetch failed",
        )

    if kind == "x":
        # X blocks its own API on the free tier and blocks Exa's crawler.
        # Nitter (via Zyte for Anubis PoW when needed) is the reliable
        # path — free httpx first, Zyte only on challenge.
        tweet = await nitter.fetch_tweet(
            url,
            instances=settings.NITTER_INSTANCES,
            zyte_api_key=settings.ZYTE_API_KEY,
        )
        if tweet is None:
            return UrlScrapeResult(
                source="x", payload={}, content=url,
                error=(
                    "nitter fetch failed (instance down / PoW unsolvable / tweet "
                    "inaccessible). paste the tweet text as a regular message "
                    "if you want the content; reply with /highlight to link it."
                ),
            )
        return UrlScrapeResult(
            source="x",
            payload={
                "title": tweet.author,
                "author": tweet.author,
                "text": tweet.text,
                "via": tweet.via,
            },
            content=(f"{tweet.author}\n\n{tweet.text}" if tweet.author else tweet.text),
        )

    if kind == "youtube":
        yt = await youtube.fetch_transcript(url)
        if yt is None:
            return UrlScrapeResult(
                source="youtube", payload={}, content=url,
                error=(
                    "youtube transcript unavailable "
                    "(private / no captions / rate-limited). "
                    "try again later, or paste a transcript manually."
                ),
            )
        # Content for LLM: title + transcript. process.process_capture caps
        # the LLM call at 30k chars already, so long podcasts are bounded.
        header = yt.title or yt.video_id
        if yt.author:
            header = f"{header}\nby {yt.author}"
        content = f"{header}\n\n{yt.text}"
        return UrlScrapeResult(
            source="youtube",
            payload={
                "video_id": yt.video_id,
                "title": yt.title,
                "author": yt.author,
                "text": yt.text,
                "language_code": yt.language_code,
                "is_auto_generated": yt.is_auto_generated,
            },
            content=content,
        )

    if kind == "reddit":
        if not settings.EXA_API_KEY:
            return UrlScrapeResult(
                source=kind, payload={}, content=url, error="EXA_API_KEY not configured",
            )
        ec = await exa.fetch_content(url, api_key=settings.EXA_API_KEY)
        if ec is None:
            return UrlScrapeResult(
                source=kind, payload={}, content=url, error="exa returned no content",
            )
        return UrlScrapeResult(
            source=kind,
            payload={
                "title": ec.title,
                "author": ec.author,
                "text": ec.text,
            },
            content=(ec.title + "\n\n" + ec.text) if ec.title else ec.text,
        )

    # generic article
    try:
        article = await generic.extract_article(url)
    except Exception as e:
        article = None
        generic_error: str | None = str(e)[:200]
    else:
        generic_error = None

    # If generic came back with thin/raw content and Zyte is configured, retry via Zyte.
    needs_retry = article is None or article.method == "raw"
    if needs_retry and settings.ZYTE_API_KEY:
        zyte_article = await zyte.extract_with_zyte(url, api_key=settings.ZYTE_API_KEY)
        if zyte_article is not None and zyte_article.method != "raw":
            article = zyte_article
            generic_error = None

    if article is None:
        return UrlScrapeResult(
            source="article",
            payload={},
            content=url,
            error=generic_error or "article extraction failed",
        )

    return UrlScrapeResult(
        source="article",
        payload={
            "title": article.title,
            "text": article.text,
            "method": article.method,
        },
        content=(article.title + "\n\n" + article.text) if article.title else article.text,
    )
