#!/usr/bin/env python3
"""Probe Exa for tweet content using both paths — which one actually works?

Usage:
    EXA_API_KEY=your_key python3 scripts/exa_probe.py "https://x.com/user/status/123"

Tries three approaches against the given tweet URL and prints the raw
responses + success/fail verdict for each:

  1. /contents + livecrawl "always"        (current deployed code)
  2. /contents + livecrawl "fallback"      (previous deployed code)
  3. /search + category "tweet"            (proposed next approach)

Run this locally with a real URL and EXA_API_KEY to decide which path
is worth wiring into the bot.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = "https://api.exa.ai"
TIMEOUT = 30.0


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "Content-Type": "application/json"}


def _truthy_text(results: list[dict]) -> str:
    if not results:
        return ""
    return (results[0].get("text") or "").strip()


def probe_contents(url: str, key: str, livecrawl: str) -> dict:
    resp = httpx.post(
        f"{BASE}/contents",
        json={
            "ids": [url],
            "text": True,
            "livecrawl": livecrawl,
            "livecrawlTimeout": 10000,
        },
        headers=_headers(key),
        timeout=TIMEOUT,
    )
    return {
        "status": resp.status_code,
        "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
    }


def probe_search_tweet_category(url: str, key: str) -> dict:
    # Use the tweet URL itself as the query, pinned to X/Twitter domains.
    resp = httpx.post(
        f"{BASE}/search",
        json={
            "query": url,
            "category": "tweet",
            "includeDomains": ["x.com", "twitter.com"],
            "numResults": 5,
            "contents": {"text": True, "livecrawl": "always", "livecrawlTimeout": 10000},
        },
        headers=_headers(key),
        timeout=TIMEOUT,
    )
    return {
        "status": resp.status_code,
        "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
    }


def summarize(label: str, result: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"status: {result['status']}")
    body = result["body"]
    if isinstance(body, dict):
        results = body.get("results") or []
        print(f"results count: {len(results)}")
        if results:
            r0 = results[0]
            text = (r0.get("text") or "").strip()
            print(f"first result url:    {r0.get('url')!r}")
            print(f"first result title:  {r0.get('title')!r}")
            print(f"first result author: {r0.get('author')!r}")
            print(f"first result text length: {len(text)} chars")
            if text:
                preview = text[:300].replace("\n", " ")
                print(f"first 300 chars: {preview!r}")
            else:
                print("(text is empty)")
        else:
            print("(no results — empty list)")
        if "error" in body or "message" in body:
            print(f"error/message in body: {body.get('error') or body.get('message')}")
        verdict = "✓ WORKS" if _truthy_text(results) else "✗ EMPTY"
    else:
        print(f"(non-JSON response): {body[:500]}")
        verdict = "✗ NON-JSON"
    print(f"verdict: {verdict}")


def main() -> int:
    key = os.environ.get("EXA_API_KEY")
    if not key:
        print("error: set EXA_API_KEY env var", file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print("usage: EXA_API_KEY=... python3 scripts/exa_probe.py <tweet-url>", file=sys.stderr)
        return 2
    url = sys.argv[1]
    print(f"probing: {url}")

    try:
        r1 = probe_contents(url, key, "always")
        summarize("1. /contents + livecrawl=always (CURRENT DEPLOYED)", r1)
    except Exception as e:
        print(f"\n=== 1. /contents + livecrawl=always === FAILED: {type(e).__name__}: {e}")

    try:
        r2 = probe_contents(url, key, "fallback")
        summarize("2. /contents + livecrawl=fallback (PREVIOUS)", r2)
    except Exception as e:
        print(f"\n=== 2. /contents + livecrawl=fallback === FAILED: {type(e).__name__}: {e}")

    try:
        r3 = probe_search_tweet_category(url, key)
        summarize("3. /search + category='tweet' (PROPOSED)", r3)
    except Exception as e:
        print(f"\n=== 3. /search + category='tweet' === FAILED: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
