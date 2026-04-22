"""Nitter-based X/Twitter scraping.

Why: X blocks its own API for reads on the free tier ($200/mo for Basic).
Exa can't scrape fresh tweets. Nitter is the last reliable frontend — but
most public instances gate with Anubis (JS proof-of-work), which plain
httpx can't solve.

Strategy:
  1. For each Nitter instance in the configured list, in order:
     a. Rewrite the x.com URL to point at that instance.
     b. Try plain httpx first (free, fast — some instances/tweets may
        serve direct content without Anubis).
     c. If Anubis or rate-limited, fall back to Zyte's browserHtml, which
        runs a real headless browser that solves the PoW.
     d. Extract the tweet text from the page's og:description meta tag
        (cleanest), falling back to the .tweet-content div.
  2. Return on the first successful extract — remaining instances are
     not contacted. If all instances fail, return None and let the caller
     degrade to a bare-URL capture.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TWEET_CONTENT_RE = re.compile(
    r'<div[^>]+class=["\'][^"\']*tweet-content[^"\']*["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TIMEOUT = 15.0
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


@dataclass
class TweetContent:
    url: str
    author: str | None  # display name (e.g. "notthreadguy")
    text: str
    via: str  # "direct" or "zyte" — for diagnostics/logging


def _rewrite_to_nitter(x_url: str, instance: str) -> str | None:
    p = urlparse(x_url)
    host = (p.hostname or "").lower()
    if not (host in ("x.com", "twitter.com") or host.endswith(".x.com") or host.endswith(".twitter.com")):
        return None
    return f"https://{instance}{p.path or '/'}"


def _unescape(text: str) -> str:
    for a, b in (("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(a, b)
    return text


def _extract_text(html: str) -> str:
    m = _OG_DESC_RE.search(html)
    if m:
        return _unescape(m.group(1)).strip()
    m = _TWEET_CONTENT_RE.search(html)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ""


def _extract_author(html: str) -> str | None:
    m = _OG_TITLE_RE.search(html)
    if not m:
        return None
    # Nitter og:title is typically `Author Name (@handle) / status / ...`
    return _unescape(m.group(1)).strip() or None


def _is_anubis_challenge(html: str) -> bool:
    """Anubis challenge pages embed their name + a 'not a bot' header."""
    low = html.lower()
    return "anubis" in low or "not a bot" in low


async def _fetch_direct(
    nitter_url: str, client: httpx.AsyncClient,
) -> str | None:
    """Plain httpx GET. Returns HTML if it looks like real Nitter content,
    else None (Anubis challenge, 4xx/5xx, empty body, etc.).
    """
    try:
        resp = await client.get(
            nitter_url,
            headers={"User-Agent": _UA, "Accept": "text/html"},
            follow_redirects=True,
            timeout=_TIMEOUT,
        )
    except Exception as e:
        log.debug("nitter direct fetch failed for %s: %s", nitter_url, e)
        return None
    if resp.status_code != 200 or not resp.text:
        return None
    if _is_anubis_challenge(resp.text):
        return None
    return resp.text


async def _fetch_via_zyte(
    nitter_url: str, *, zyte_api_key: str, client: httpx.AsyncClient,
) -> str | None:
    """Zyte browserHtml — runs a real headless browser that solves the PoW.
    Returns rendered HTML (with Anubis solved) or None on any failure.
    """
    try:
        resp = await client.post(
            "https://api.zyte.com/v1/extract",
            json={"url": nitter_url, "browserHtml": True},
            auth=(zyte_api_key, ""),
            timeout=60.0,  # PoW + render can take several seconds
        )
    except Exception as e:
        log.warning("nitter zyte fetch failed for %s: %s: %s", nitter_url, type(e).__name__, e)
        return None
    if resp.status_code != 200:
        log.warning("nitter zyte returned %s for %s", resp.status_code, nitter_url)
        return None
    try:
        html = resp.json().get("browserHtml") or ""
    except Exception:
        return None
    if not html or _is_anubis_challenge(html):
        # Zyte's browser couldn't complete the PoW within its render budget.
        return None
    return html


async def fetch_tweet(
    x_url: str,
    *,
    instances: list[str] | str,
    zyte_api_key: str,
    client: httpx.AsyncClient | None = None,
) -> TweetContent | None:
    """Fetch tweet text for an x.com URL via Nitter.

    `instances` is a list of Nitter hostnames (or a comma-separated string)
    tried in order. For each: direct httpx first (free), then Zyte fallback
    if the instance gates with Anubis. Returns on the first success —
    remaining instances are not contacted.

    Returns None if all paths fail or the URL isn't an X URL.
    """
    if isinstance(instances, str):
        instances = [i.strip() for i in instances.split(",") if i.strip()]
    if not instances:
        return None
    # _rewrite_to_nitter only inspects the input URL's host — if it rejects
    # the URL for one instance, it rejects it for all. Check once up front.
    if _rewrite_to_nitter(x_url, instances[0]) is None:
        return None

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        for instance in instances:
            nitter_url = f"https://{instance}{urlparse(x_url).path or '/'}"

            html = await _fetch_direct(nitter_url, client)
            via = "direct"
            if html is None and zyte_api_key:
                html = await _fetch_via_zyte(nitter_url, zyte_api_key=zyte_api_key, client=client)
                via = f"zyte:{instance}"
            if html is None:
                log.info("nitter: %s failed, trying next instance", instance)
                continue

            text = _extract_text(html)
            if not text:
                log.info("nitter: %s returned HTML but no tweet text, trying next", instance)
                continue

            return TweetContent(
                url=x_url,
                author=_extract_author(html),
                text=text,
                via=via,
            )
        return None
    finally:
        if owned:
            await client.aclose()
