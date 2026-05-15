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
from bot.ingest import hn as hn_mod
from bot.ingest.router import scrape_url

_GEN_URL = "https://blog.example.com/post"
_HN_URL = "https://news.ycombinator.com/item?id=42"


def _settings(**kw):
    return Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC", **kw)


def _article(title, text, method):
    return types.SimpleNamespace(title=title, text=text, method=method)


def _story(url, title="HN Title", text=None, comments=None):
    return hn_mod.HnStory(
        id=42, title=title, url=url, by="alice", score=100, text=text,
        comments=comments if comments is not None
        else [{"id": 1, "by": "bob", "text": "great point", "time": 0}],
    )


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


# --- U3: HN branch decision tree (R1-R7) ---


async def test_hn_self_post_unchanged():
    # AE2: no outbound url → canonical None, full HN content, unchanged.
    with patch(
        "bot.ingest.router.hn.fetch_story",
        AsyncMock(return_value=_story(url=None, text="Ask HN body")),
    ):
        r = await scrape_url(_HN_URL, settings=_settings())
    assert r.source == "hn"
    assert r.canonical_url is None
    assert "HN Title" in r.content and "Ask HN body" in r.content
    assert "story" in r.payload and "comments" in r.payload
    assert "title" not in r.payload  # no top-level article fields


async def test_hn_routable_target_not_scraped():
    # AE3: outbound url is a tweet → canonical = tweet, no deep scrape.
    ext = AsyncMock()
    with patch(
        "bot.ingest.router.hn.fetch_story",
        AsyncMock(return_value=_story(url="https://x.com/a/status/1")),
    ), patch("bot.ingest.router._extract_article", ext):
        r = await scrape_url(_HN_URL, settings=_settings())
    ext.assert_not_called()
    assert r.source == "hn"
    assert r.canonical_url == "https://x.com/a/status/1"
    assert "[link: https://x.com/a/status/1]" in r.content
    assert "story" in r.payload and "title" not in r.payload


async def test_hn_routable_youtube_and_self_hn_link():
    for target in (
        "https://youtu.be/abcdef",
        "https://news.ycombinator.com/item?id=99",
    ):
        with patch(
            "bot.ingest.router.hn.fetch_story",
            AsyncMock(return_value=_story(url=target)),
        ), patch("bot.ingest.router._extract_article", AsyncMock()) as ext:
            r = await scrape_url(_HN_URL, settings=_settings())
        ext.assert_not_called()
        assert r.canonical_url == target
        assert r.source == "hn"


async def test_hn_generic_article_scraped_as_primary():
    # AE1: generic outbound → article body is the capture; discourse nested;
    # article title/text top-level for digest/tweet shape-readers (R5).
    art = _article("Real Title", "real body", "readability")
    with patch(
        "bot.ingest.router.hn.fetch_story",
        AsyncMock(return_value=_story(url="https://blog.example.com/x")),
    ), patch(
        "bot.ingest.router._extract_article", AsyncMock(return_value=(art, None))
    ):
        r = await scrape_url(_HN_URL, settings=_settings())
    assert r.source == "hn"
    assert r.canonical_url == "https://blog.example.com/x"
    assert r.content == "Real Title\n\nreal body"
    assert r.payload["story"]["title"] == "HN Title"
    assert r.payload["comments"][0]["by"] == "bob"
    assert r.payload["title"] == "Real Title"
    assert r.payload["text"] == "real body"
    assert r.error is None


async def test_hn_article_failure_falls_back_to_discourse():
    # AE4: extraction returns nothing → canonical stays article url, content
    # is the HN discussion, scrape_error notes discourse retained.
    with patch(
        "bot.ingest.router.hn.fetch_story",
        AsyncMock(return_value=_story(url="https://blog.example.com/x")),
    ), patch(
        "bot.ingest.router._extract_article", AsyncMock(return_value=(None, "boom"))
    ):
        r = await scrape_url(_HN_URL, settings=_settings())
    assert r.canonical_url == "https://blog.example.com/x"
    assert "HN Title" in r.content
    assert r.error == "article extraction failed; HN discussion retained"
    assert "story" in r.payload and "title" not in r.payload


async def test_hn_article_helper_raise_degrades_not_propagates():
    # R6: an unforeseen raise from the helper must not break the capture.
    with patch(
        "bot.ingest.router.hn.fetch_story",
        AsyncMock(return_value=_story(url="https://blog.example.com/x")),
    ), patch(
        "bot.ingest.router._extract_article",
        AsyncMock(side_effect=RuntimeError("zyte exploded")),
    ):
        r = await scrape_url(_HN_URL, settings=_settings())
    assert r.canonical_url == "https://blog.example.com/x"
    assert r.error == "article extraction failed; HN discussion retained"
    assert "story" in r.payload


async def test_hn_fetch_failure_unchanged():
    # AE5: HN fetch itself fails → bare-url contract, unchanged.
    with patch("bot.ingest.router.hn.fetch_story", AsyncMock(return_value=None)):
        r = await scrape_url(_HN_URL, settings=_settings())
    assert r.source == "hn" and r.payload == {} and r.content == _HN_URL
    assert r.error == "hn fetch failed"


async def test_hn_empty_comment_list_payload_well_formed():
    with patch(
        "bot.ingest.router.hn.fetch_story",
        AsyncMock(return_value=_story(url=None, comments=[])),
    ):
        r = await scrape_url(_HN_URL, settings=_settings())
    assert r.payload["comments"] == []
    assert "story" in r.payload
