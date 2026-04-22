"""Tests for bot.ingest.nitter: URL rewriting, text extraction, instance
rotation, and Zyte fallback on Anubis challenges."""

from __future__ import annotations

import httpx
import pytest

from bot.ingest import nitter


# ---- URL rewriting --------------------------------------------------------

def test_rewrite_to_nitter_converts_x_domain():
    out = nitter._rewrite_to_nitter("https://x.com/user/status/123", "nitter.example")
    assert out == "https://nitter.example/user/status/123"


def test_rewrite_to_nitter_converts_twitter_domain():
    out = nitter._rewrite_to_nitter("https://twitter.com/user/status/123", "nitter.example")
    assert out == "https://nitter.example/user/status/123"


def test_rewrite_to_nitter_returns_none_for_non_x_url():
    assert nitter._rewrite_to_nitter("https://example.com/page", "nitter.example") is None


# ---- Text extraction ------------------------------------------------------

def test_extract_prefers_og_description():
    html = """
    <meta property="og:description" content="the clean tweet body">
    <div class="tweet-content">the raw text with <b>html</b></div>
    """
    assert nitter._extract_text(html) == "the clean tweet body"


def test_extract_falls_back_to_tweet_content_div():
    html = '<div class="tweet-content media-body">raw body</div>'
    assert nitter._extract_text(html) == "raw body"


def test_extract_unescapes_html_entities():
    html = '<meta property="og:description" content="it&#39;s a &quot;test&quot;">'
    assert nitter._extract_text(html) == 'it\'s a "test"'


def test_extract_returns_empty_on_missing():
    assert nitter._extract_text("<html><body>nothing useful</body></html>") == ""


def test_extract_author_pulls_from_og_title():
    html = '<meta property="og:title" content="notthreadguy (@notthreadguy) / Twitter">'
    assert nitter._extract_author(html) == "notthreadguy (@notthreadguy) / Twitter"


def test_extract_author_returns_none_when_missing():
    assert nitter._extract_author("<html><body>no og:title</body></html>") is None


def test_extract_author_unescapes_entities():
    html = '<meta property="og:title" content="Ben &amp; Jerry (@bj)">'
    assert nitter._extract_author(html) == "Ben & Jerry (@bj)"


def test_is_anubis_challenge_detected():
    anubis_html = """
    <title>Making sure you're not a bot!</title>
    <script id="anubis_challenge">{}</script>
    """
    assert nitter._is_anubis_challenge(anubis_html)
    assert not nitter._is_anubis_challenge("<html><body>real content</body></html>")


# ---- fetch_tweet (integration) --------------------------------------------

_REAL_TWEET_HTML = """
<html><head>
<meta property="og:title" content="notthreadguy (@notthreadguy) on Twitter">
<meta property="og:description" content="a real tweet body">
</head><body><div class="tweet-content">a real tweet body</div></body></html>
"""


@pytest.mark.asyncio
async def test_fetch_tweet_direct_success_no_zyte_called():
    """Direct httpx returns good content — Zyte should not be called."""
    zyte_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if "api.zyte.com" in str(request.url):
            zyte_calls.append(str(request.url))
            return httpx.Response(200, json={"browserHtml": "should not be used"})
        # Nitter-style response
        return httpx.Response(200, text=_REAL_TWEET_HTML)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tweet = await nitter.fetch_tweet(
            "https://x.com/u/status/1",
            instances=["nitter.first"],
            zyte_api_key="K",
            client=client,
        )
    assert tweet is not None
    assert tweet.text == "a real tweet body"
    assert tweet.via == "direct"
    assert zyte_calls == []


@pytest.mark.asyncio
async def test_fetch_tweet_falls_back_to_zyte_on_anubis():
    """Direct hits an Anubis page — Zyte is called and returns real content."""
    nitter_calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal nitter_calls
        if "api.zyte.com" in str(request.url):
            return httpx.Response(200, json={"browserHtml": _REAL_TWEET_HTML})
        nitter_calls += 1
        return httpx.Response(
            200,
            text='<title>Making sure you\'re not a bot!</title><script id="anubis_challenge"/>',
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tweet = await nitter.fetch_tweet(
            "https://x.com/u/status/1",
            instances=["nitter.first"],
            zyte_api_key="K",
            client=client,
        )
    assert tweet is not None
    assert tweet.text == "a real tweet body"
    assert tweet.via.startswith("zyte:")


@pytest.mark.asyncio
async def test_fetch_tweet_rotates_through_instances():
    """First instance fails entirely (connection refused); second works direct."""
    seen_hosts: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen_hosts.append(host)
        if host == "nitter.down":
            raise httpx.ConnectError("refused")
        if host == "nitter.up":
            return httpx.Response(200, text=_REAL_TWEET_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tweet = await nitter.fetch_tweet(
            "https://x.com/u/status/1",
            instances=["nitter.down", "nitter.up"],
            zyte_api_key="",  # not needed; direct works
            client=client,
        )
    assert tweet is not None
    assert tweet.text == "a real tweet body"
    assert "nitter.down" in seen_hosts
    assert "nitter.up" in seen_hosts


@pytest.mark.asyncio
async def test_fetch_tweet_accepts_comma_separated_string():
    """Settings surface instances as a comma-separated env var string."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_REAL_TWEET_HTML)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tweet = await nitter.fetch_tweet(
            "https://x.com/u/status/1",
            instances="nitter.a, nitter.b , nitter.c",
            zyte_api_key="",
            client=client,
        )
    assert tweet is not None


@pytest.mark.asyncio
async def test_fetch_tweet_returns_none_when_all_fail():
    """All instances Anubis-gated AND Zyte also returns Anubis — caller
    degrades to bare-URL capture."""
    anubis = '<title>not a bot</title><script id="anubis_challenge"/>'

    def _handler(request: httpx.Request) -> httpx.Response:
        if "api.zyte.com" in str(request.url):
            return httpx.Response(200, json={"browserHtml": anubis})
        return httpx.Response(200, text=anubis)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        tweet = await nitter.fetch_tweet(
            "https://x.com/u/status/1",
            instances=["nitter.one", "nitter.two"],
            zyte_api_key="K",
            client=client,
        )
    assert tweet is None


@pytest.mark.asyncio
async def test_fetch_tweet_returns_none_for_non_x_url():
    async with httpx.AsyncClient() as client:
        tweet = await nitter.fetch_tweet(
            "https://example.com/page",
            instances=["nitter.tiekoetter.com"],
            zyte_api_key="K",
            client=client,
        )
    assert tweet is None
