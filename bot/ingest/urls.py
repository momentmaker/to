"""URL shape classification. Decides which scraper to route to."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

UrlKind = Literal["hn", "reddit", "x", "youtube", "generic"]

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,);]") if m else None


def classify_url(url: str) -> UrlKind:
    host = (urlparse(url).hostname or "").lower()
    if host in ("news.ycombinator.com", "hn.algolia.com"):
        return "hn"
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    if host in ("x.com", "twitter.com") or host.endswith(".x.com") or host.endswith(".twitter.com"):
        return "x"
    if host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com"):
        return "youtube"
    return "generic"
