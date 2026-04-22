#!/usr/bin/env python3
"""Probe whether Zyte can fetch Nitter pages past the Anubis anti-bot PoW.

Usage:
    ZYTE_API_KEY=<key> python3 scripts/zyte_nitter_probe.py "https://x.com/user/status/123"

Plain httpx against Nitter returns an Anubis JavaScript challenge page, not
the tweet. Zyte's /extract endpoint with browserHtml:true renders the page
in a real headless browser, which *should* execute the PoW and return the
final tweet content. This script proves it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import httpx


INSTANCES = [
    "nitter.net",
    "nitter.tiekoetter.com",
    "nitter.privacydev.net",
    "nitter.cz",
    "nitter.poast.org",
]

_ZYTE_URL = "https://api.zyte.com/v1/extract"
TIMEOUT = 60.0  # Zyte browser renders can take a while (PoW + render)


_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TWEET_CONTENT_RE = re.compile(
    r'<div[^>]+class=["\'][^"\']*tweet-content[^"\']*["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)


def x_to_path(url: str) -> str | None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not (host in ("x.com", "twitter.com") or host.endswith(".x.com") or host.endswith(".twitter.com")):
        return None
    return p.path or "/"


def extract_tweet_text(html: str) -> str:
    m = _OG_DESC_RE.search(html)
    if m:
        text = m.group(1)
        for a, b in (("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
            text = text.replace(a, b)
        return text.strip()
    m = _TWEET_CONTENT_RE.search(html)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ""


def probe_direct(nitter_url: str) -> dict:
    """Free httpx check — some instances may serve content without Anubis."""
    t0 = time.monotonic()
    try:
        resp = httpx.get(
            nitter_url, timeout=8.0,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            follow_redirects=True,
        )
        elapsed = time.monotonic() - t0
        html = resp.text
        is_challenge = "anubis" in html.lower() or "not a bot" in html.lower()
        text = extract_tweet_text(html) if not is_challenge else ""
        return {
            "status": resp.status_code, "elapsed_ms": int(elapsed * 1000),
            "html_len": len(html),
            "is_anubis_challenge": is_challenge,
            "tweet_text": text,
        }
    except Exception as e:
        return {
            "status": 0, "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


def probe_zyte(nitter_url: str, key: str) -> dict:
    t0 = time.monotonic()
    try:
        resp = httpx.post(
            _ZYTE_URL,
            json={"url": nitter_url, "browserHtml": True},
            auth=(key, ""),
            timeout=TIMEOUT,
        )
        elapsed = time.monotonic() - t0
        if resp.status_code != 200:
            return {
                "status": resp.status_code, "elapsed_ms": int(elapsed * 1000),
                "error": resp.text[:300],
            }
        data = resp.json()
        html = data.get("browserHtml") or ""
        is_challenge = "anubis" in html.lower() or "not a bot" in html.lower()
        text = extract_tweet_text(html)
        return {
            "status": 200, "elapsed_ms": int(elapsed * 1000),
            "html_len": len(html),
            "is_anubis_challenge": is_challenge,
            "tweet_text": text,
        }
    except Exception as e:
        return {
            "status": 0, "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


def main() -> int:
    key = os.environ.get("ZYTE_API_KEY")
    if not key:
        print("error: set ZYTE_API_KEY env var", file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print("usage: ZYTE_API_KEY=... python3 scripts/zyte_nitter_probe.py <tweet-url>", file=sys.stderr)
        return 2
    x_url = sys.argv[1]
    path = x_to_path(x_url)
    if path is None:
        print(f"error: not an x.com / twitter.com URL: {x_url}", file=sys.stderr)
        return 2

    print(f"probing path: {path}\n")
    print("Phase 1 — free httpx (no Zyte cost):")
    direct_winners: list[str] = []
    needs_zyte: list[str] = []
    for inst in INSTANCES:
        nitter_url = f"https://{inst}{path}"
        r = probe_direct(nitter_url)
        if r.get("tweet_text"):
            preview = r["tweet_text"][:200].replace("\n", " ")
            print(f"  ✓ {inst:35s} {r['status']}  {r['elapsed_ms']:>5d}ms  (direct, free)")
            print(f"    text: {preview!r}")
            direct_winners.append(inst)
        elif r.get("is_anubis_challenge"):
            print(f"  ⚠ {inst:35s} ANUBIS {r['elapsed_ms']:>5d}ms  (try Zyte)")
            needs_zyte.append(inst)
        elif r.get("status") == 200:
            print(f"  ✗ {inst:35s} {r['status']}  {r['elapsed_ms']:>5d}ms  html_len={r.get('html_len', 0)} (no text, not challenge)")
        else:
            print(f"  ✗ {inst:35s} {r.get('status', 'ERR')}  {r['elapsed_ms']:>5d}ms  {r.get('error', '')[:80]}")

    zyte_winners: list[str] = []
    if needs_zyte:
        print(f"\nPhase 2 — Zyte browserHtml for {len(needs_zyte)} Anubis-gated instance(s):")
        for inst in needs_zyte:
            nitter_url = f"https://{inst}{path}"
            r = probe_zyte(nitter_url, key)
            if r["status"] == 200 and r.get("tweet_text") and not r.get("is_anubis_challenge"):
                preview = r["tweet_text"][:200].replace("\n", " ")
                print(f"  ✓ {inst:35s} {r['status']}  {r['elapsed_ms']:>5d}ms  (via Zyte)")
                print(f"    text: {preview!r}")
                zyte_winners.append(inst)
            elif r.get("is_anubis_challenge"):
                print(f"  ✗ {inst:35s} STILL ANUBIS  {r['elapsed_ms']:>5d}ms (Zyte couldn't solve PoW)")
            elif "error" in r:
                print(f"  ✗ {inst:35s} ERR  {r['elapsed_ms']:>5d}ms  {r['error'][:80]}")
            else:
                print(f"  ✗ {inst:35s} {r['status']}  {r['elapsed_ms']:>5d}ms  html_len={r.get('html_len', 0)}")

    print()
    if direct_winners:
        print(f"DIRECT WINNERS (no Zyte needed): {', '.join(direct_winners)}")
        print(f"→ wire {direct_winners[0]!r} as NITTER_INSTANCE, plain httpx is enough")
    elif zyte_winners:
        print(f"ZYTE-ONLY WINNERS: {', '.join(zyte_winners)}")
        print(f"→ wire {zyte_winners[0]!r} as NITTER_INSTANCE, route through zyte.fetch_html_via_zyte")
    else:
        print("No instance works (direct or Zyte). Accept the copy-paste workflow.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
