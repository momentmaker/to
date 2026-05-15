from bot.ingest.router import UrlScrapeResult, classify_text
from bot.ingest.urls import classify_url, extract_url


def test_url_scrape_result_canonical_url_defaults_none():
    r = UrlScrapeResult(source="article", payload={}, content="body")
    assert r.canonical_url is None


def test_classify_url_recognizes_hn():
    assert classify_url("https://news.ycombinator.com/item?id=123") == "hn"
    assert classify_url("https://hn.algolia.com/api/v1/items/123") == "hn"


def test_classify_url_recognizes_reddit():
    assert classify_url("https://reddit.com/r/foo/comments/1/bar") == "reddit"
    assert classify_url("https://www.reddit.com/r/foo") == "reddit"
    assert classify_url("https://old.reddit.com/r/foo") == "reddit"


def test_classify_url_recognizes_x_and_twitter():
    assert classify_url("https://x.com/handle/status/1") == "x"
    assert classify_url("https://twitter.com/handle/status/1") == "x"
    assert classify_url("https://mobile.twitter.com/handle") == "x"


def test_classify_url_defaults_to_generic():
    assert classify_url("https://example.com/article") == "generic"
    assert classify_url("https://blog.acme.io/post") == "generic"


def test_extract_url_finds_first_http_url():
    assert extract_url("check this out https://example.com/a cool!") == "https://example.com/a"
    assert extract_url("no url here") is None
    assert extract_url("") is None


def test_extract_url_strips_trailing_punctuation():
    assert extract_url("see https://example.com/path.") == "https://example.com/path"
    assert extract_url("see https://example.com/path,") == "https://example.com/path"
    assert extract_url("see https://example.com/path)") == "https://example.com/path"


def test_classify_text_text_vs_url():
    assert classify_text("just a plain line i heard") == ("text", None)
    k, u = classify_text("https://example.com/a is worth a read")
    assert k == "url" and u == "https://example.com/a"


# --- Characterization: generic-article branch of scrape_url (U2 refactor guard) ---

import types
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.ingest.router import scrape_url

_GEN_URL = "https://blog.example.com/post"


def _article(title, text, method):
    return types.SimpleNamespace(title=title, text=text, method=method)


async def test_scrape_url_generic_clean_extraction():
    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")
    art = _article("A Title", "the body", "readability")
    with patch("bot.ingest.router.generic.extract_article", AsyncMock(return_value=art)):
        r = await scrape_url(_GEN_URL, settings=settings)
    assert r.source == "article"
    assert r.payload == {"title": "A Title", "text": "the body", "method": "readability"}
    assert r.content == "A Title\n\nthe body"
    assert r.error is None
    assert r.canonical_url is None


async def test_scrape_url_generic_total_failure_no_zyte():
    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")
    with patch(
        "bot.ingest.router.generic.extract_article",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        r = await scrape_url(_GEN_URL, settings=settings)
    assert r.source == "article"
    assert r.payload == {}
    assert r.content == _GEN_URL
    assert r.error == "boom"


async def test_scrape_url_generic_raw_retries_via_zyte():
    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC", ZYTE_API_KEY="zk"
    )
    raw = _article(None, "thin", "raw")
    good = _article("Zyte Title", "zyte body", "trafilatura")
    with patch(
        "bot.ingest.router.generic.extract_article", AsyncMock(return_value=raw)
    ), patch(
        "bot.ingest.router.zyte.extract_with_zyte", AsyncMock(return_value=good)
    ):
        r = await scrape_url(_GEN_URL, settings=settings)
    assert r.payload["method"] == "trafilatura"
    assert r.payload["title"] == "Zyte Title"
    assert r.content == "Zyte Title\n\nzyte body"
    assert r.error is None
