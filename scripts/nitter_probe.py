#!/usr/bin/env python3
"""Probe a list of Nitter instances for a tweet URL — find which one works.

Usage:
    python3 scripts/nitter_probe.py "https://x.com/user/status/123"

Tries each instance in the list, reports:
  - HTTP status
  - content length
  - extracted tweet text (if extractable)
  - latency

Pick the best instance, commit its hostname as the default for NITTER_INSTANCE.
"""
from __future__ import annotations

import re
import sys
import time
from urllib.parse import urlparse

import httpx


# Community-maintained list. Current as of early 2026 — instances rotate
# in and out of service as X blocks them. Ordered by perceived reliability.
INSTANCES = [
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.tiekoetter.com",
    "nitter.poast.org",
    "nitter.cz",
    "nitter.fdn.fr",
    "nitter.1d4.us",
    "nitter.kavin.rocks",
    "nitter.unixfox.eu",
]

TIMEOUT = 8.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 to-commonplace-probe"


def x_url_to_nitter_path(url: str) -> str | None:
    """Convert https://x.com/user/status/ID → /user/status/ID (the path)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not (host in ("x.com", "twitter.com") or host.endswith(".x.com") or host.endswith(".twitter.com")):
        return None
    # Strip leading slash re-added per instance
    return p.path or "/"


_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TWEET_CONTENT_RE = re.compile(
    r'<div[^>]+class=["\'][^"\']*tweet-content[^"\']*["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)


def extract_tweet_text(html: str) -> str:
    """Pull the tweet text out of a Nitter page. Tries og:description first
    (cleanest), falls back to the .tweet-content div."""
    m = _OG_DESC_RE.search(html)
    if m:
        # Unescape HTML entities roughly
        text = m.group(1)
        text = text.replace("&quot;", '"').replace("&#39;", "'")
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        return text.strip()
    m = _TWEET_CONTENT_RE.search(html)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1))
        return text.strip()
    return ""


def probe(instance: str, path: str) -> dict:
    url = f"https://{instance}{path}"
    t0 = time.monotonic()
    try:
        resp = httpx.get(
            url, timeout=TIMEOUT,
            headers={"User-Agent": UA, "Accept": "text/html"},
            follow_redirects=True,
        )
        elapsed = time.monotonic() - t0
        text = extract_tweet_text(resp.text) if resp.status_code == 200 else ""
        return {
            "url": url, "status": resp.status_code,
            "elapsed_ms": int(elapsed * 1000),
            "content_len": len(resp.text),
            "tweet_text": text,
        }
    except Exception as e:
        return {
            "url": url, "status": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python3 scripts/nitter_probe.py <x-tweet-url>", file=sys.stderr)
        return 2
    x_url = sys.argv[1]
    path = x_url_to_nitter_path(x_url)
    if path is None:
        print(f"error: not an x.com / twitter.com URL: {x_url}", file=sys.stderr)
        return 2
    print(f"probing path: {path}\n")
    winners = []
    for inst in INSTANCES:
        r = probe(inst, path)
        status = r.get("status", 0)
        elapsed = r.get("elapsed_ms", 0)
        if status == 200 and r.get("tweet_text"):
            preview = r["tweet_text"][:200].replace("\n", " ")
            print(f"✓ {inst:35s} {status}  {elapsed:>5d}ms  {len(r['tweet_text'])}ch  {preview!r}")
            winners.append(inst)
        elif "error" in r:
            print(f"✗ {inst:35s} ERR    {elapsed:>5d}ms  {r['error']}")
        else:
            print(f"✗ {inst:35s} {status}  {elapsed:>5d}ms  len={r.get('content_len', 0)}  (no tweet text found)")

    print()
    if winners:
        print(f"WORKING INSTANCES: {', '.join(winners)}")
        print(f"→ use {winners[0]!r} as the default for NITTER_INSTANCE")
        return 0
    else:
        print("No working Nitter instance found. X may have pressured them all down,")
        print("or this specific tweet isn't accessible. Recommend the copy-paste path.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
