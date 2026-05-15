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
    # The URL to persist as the capture's canonical link. None means "use the
    # URL the user pasted". Only HN sets this — when an HN story links out, the
    # outbound article is the thing worth saving, not the HN permalink.
    canonical_url: str | None = None


async def _extract_article(
    url: str, *, settings: Settings
) -> tuple[Any, str | None]:
    """Readable-article extraction: generic first, Zyte retry on thin/raw.

    Returns (article, error). `article` is the extracted object (with
    .title/.text/.method) or None on total failure; `error` is a short
    diagnostic string or None. Shared by the generic-article branch and the
    HN branch so an HN-discovered article gets the same robustness as a
    directly-pasted one.
    """
    try:
        article = await generic.extract_article(url)
    except Exception as e:
        article = None
        error: str | None = str(e)[:200]
    else:
        error = None

    needs_retry = article is None or article.method == "raw"
    if needs_retry and settings.ZYTE_API_KEY:
        zyte_article = await zyte.extract_with_zyte(url, api_key=settings.ZYTE_API_KEY)
        if zyte_article is not None and zyte_article.method != "raw":
            article = zyte_article
            error = None
    return article, error


def _article_fields(article: Any) -> tuple[dict[str, Any], str]:
    """Shared (payload, content) shape for a successfully extracted article.

    The generic-article branch and the HN article-primary branch both build
    this; sharing it keeps the two from drifting if the shape changes.
    """
    payload = {
        "title": article.title,
        "text": article.text,
        "method": article.method,
    }
    content = (
        (article.title + "\n\n" + article.text)
        if article.title else article.text
    )
    return payload, content


async def scrape_url(url: str, *, settings: Settings) -> UrlScrapeResult:
    """Route a URL to the right scraper based on its shape.

    Never raises; on failure returns a result with `error` set and
    `content` falling back to the URL itself.
    """
    kind = classify_url(url)

    if kind == "hn":
        item_id = hn.extract_item_id(url)
        story = await hn.fetch_story(item_id) if item_id is not None else None
        if story is None:
            # R7: HN fetch itself failed — bare-url contract.
            return UrlScrapeResult(
                source="hn", payload={}, content=url, error="hn fetch failed",
            )

        hn_payload = hn.to_payload(story)
        hn_content = hn.to_processing_content(story)

        # R2: self-post (Ask/Show/Tell HN), no outbound link — the HN thread
        # IS the capture.
        if not story.url:
            return UrlScrapeResult(
                source="hn", payload=hn_payload, content=hn_content,
            )

        # R3: outbound link is itself a routable source (tweet / video /
        # reddit / another HN item). Don't deep-scrape it — canonical points
        # at the real thing, content stays the HN discussion + bare link.
        if classify_url(story.url) != "generic":
            return UrlScrapeResult(
                source="hn",
                payload=hn_payload,
                content=hn_content,
                canonical_url=story.url,
            )

        # R4: plain article — scrape it with the same robustness a directly
        # pasted article gets (shared _extract_article). R6: any failure,
        # including an unforeseen raise from the helper, degrades to the HN
        # discussion rather than breaking the capture.
        try:
            article, _ = await _extract_article(story.url, settings=settings)
        except Exception as e:
            log.debug("hn article extract raised for %s: %s", story.url, e)
            article = None

        if article is None:
            return UrlScrapeResult(
                source="hn",
                payload=hn_payload,
                content=hn_content,
                canonical_url=story.url,
                error="article extraction failed; HN discussion retained",
            )

        # R4 success: the article body is the capture. HN story + comments
        # stay nested in the payload as discourse (R5); article title/text
        # surface at the top level so the weekly-digest / daily-tweet
        # shape-readers pick them up, same as a directly-pasted article.
        art_payload, art_content = _article_fields(article)
        return UrlScrapeResult(
            source="hn",
            payload={**hn_payload, **art_payload},
            content=art_content,
            canonical_url=story.url,
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
        yt = await youtube.fetch(url)
        # None only for non-YouTube URLs (shouldn't happen given classify_url
        # already routed us here, but handle defensively).
        if yt is None:
            return UrlScrapeResult(
                source="youtube", payload={}, content=url,
                error=youtube.FAIL_UNKNOWN,
            )
        # Build LLM content from whatever metadata + transcript we got.
        # Telegram-preview minimum: title + description. Transcript is a bonus.
        parts: list[str] = []
        if yt.title:
            parts.append(yt.title)
        if yt.author:
            parts.append(f"by {yt.author}")
        if yt.description:
            parts.append("")  # blank line before description
            parts.append(yt.description)
        if yt.text:
            parts.append("")  # blank line before transcript
            parts.append(yt.text)
        content = "\n".join(parts) if parts else url
        return UrlScrapeResult(
            source="youtube",
            payload={
                "video_id": yt.video_id,
                "title": yt.title,
                "author": yt.author,
                "description": yt.description,
                "text": yt.text,
                "language_code": yt.language_code,
                "is_auto_generated": yt.is_auto_generated,
                # Preserve the classified transcript failure in the payload
                # even when we don't surface it as scrape_error (i.e. when
                # metadata was enough to build a useful capture). Otherwise
                # you'd look at a `.md` six months later and have no idea
                # why `text` is empty.
                "transcript_error": yt.transcript_error,
            },
            content=content,
            # Only surface transcript_error as scrape_error when we truly
            # have nothing else to show (no title, no description). If we
            # got metadata, the capture is useful even without captions;
            # don't alarm the user. The payload still records the reason.
            error=(
                yt.transcript_error
                if (yt.transcript_error and not yt.title and not yt.description)
                else None
            ),
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
    article, generic_error = await _extract_article(url, settings=settings)
    if article is None:
        return UrlScrapeResult(
            source="article",
            payload={},
            content=url,
            error=generic_error or "article extraction failed",
        )

    art_payload, art_content = _article_fields(article)
    return UrlScrapeResult(source="article", payload=art_payload, content=art_content)
