"""Tests for HN / Zyte / Exa scrapers and the scrape_url dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bot.config import Settings
from bot.ingest import exa, hn, zyte
from bot.ingest.generic import ExtractedArticle
from bot.ingest.router import scrape_url


# ---- HN -------------------------------------------------------------------

def test_hn_extract_item_id_from_various_url_shapes():
    assert hn.extract_item_id("https://news.ycombinator.com/item?id=12345") == 12345
    assert hn.extract_item_id("https://news.ycombinator.com/item?id=9&p=2") == 9
    assert hn.extract_item_id("https://hn.algolia.com/api/v1/items/999") == 999
    assert hn.extract_item_id("https://news.ycombinator.com/") is None


def test_hn_strip_html_replaces_paragraphs_and_tags():
    raw = "<p>first</p><p>second with <a href=\"x\">link</a></p>"
    assert "first" in hn._strip_html(raw)
    assert "second with link" in hn._strip_html(raw)
    assert "<" not in hn._strip_html(raw)


@pytest.mark.asyncio
async def test_hn_fetches_story_and_top_10_comments():
    story_json = {
        "id": 42, "title": "A Thread", "url": "https://ex.com",
        "by": "pg", "score": 100,
        "kids": list(range(100, 120)),  # 20 kid ids; we should fetch only first 10
    }
    kid_responses = {i: {"id": i, "by": f"u{i}", "text": f"<p>comment {i}</p>", "time": 1} for i in range(100, 110)}

    def _transport_handler(request: httpx.Request) -> httpx.Response:
        import re as _re
        m = _re.search(r"/item/(\d+)\.json", str(request.url))
        assert m is not None
        iid = int(m.group(1))
        if iid == 42:
            return httpx.Response(200, json=story_json)
        if iid in kid_responses:
            return httpx.Response(200, json=kid_responses[iid])
        # Extra IDs beyond our first-10 slice shouldn't be requested, but if
        # they are, return something safe.
        return httpx.Response(200, json={"id": iid, "deleted": True})

    transport = httpx.MockTransport(_transport_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await hn.fetch_story(42, client=client)

    assert result is not None
    assert result.title == "A Thread"
    assert result.score == 100
    assert len(result.comments) == 10
    assert result.comments[0]["text"] == "comment 100"
    assert "comment 100" in hn.to_processing_content(result)
    payload = hn.to_payload(result)
    assert payload["story"]["title"] == "A Thread"
    assert len(payload["comments"]) == 10


@pytest.mark.asyncio
async def test_hn_fetch_story_returns_none_when_item_missing():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await hn.fetch_story(999, client=client)
    assert result is None


# ---- Zyte -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_zyte_fetch_posts_url_with_basic_auth():
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization", "")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"browserHtml": "<html><body>rendered</body></html>"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        html = await zyte.fetch_html_via_zyte("https://hard.example", api_key="K", client=client)

    assert html is not None and "rendered" in html
    assert captured["url"] == "https://api.zyte.com/v1/extract"
    assert captured["auth"].startswith("Basic ")  # BasicAuth header present
    assert "hard.example" in captured["body"]
    # httpx serializes JSON without space after colon
    assert '"browserHtml":true' in captured["body"].replace(" ", "")


@pytest.mark.asyncio
async def test_zyte_returns_none_without_api_key():
    html = await zyte.fetch_html_via_zyte("https://x", api_key="")
    assert html is None


@pytest.mark.asyncio
async def test_zyte_returns_none_on_http_error():
    def _handler(request): return httpx.Response(500)
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        html = await zyte.fetch_html_via_zyte("https://x", api_key="K", client=client)
    assert html is None


# ---- Exa ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exa_fetch_returns_text_content():
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["x_api_key"] = request.headers.get("x-api-key", "")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={
            "results": [{
                "url": "https://x.com/u/status/1",
                "title": "a short post",
                "author": "u",
                "text": "the body of the tweet",
            }]
        })

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await exa.fetch_content("https://x.com/u/status/1", api_key="K", client=client)

    assert result is not None
    assert result.text == "the body of the tweet"
    assert result.title == "a short post"
    assert captured["url"] == "https://api.exa.ai/contents"
    assert captured["x_api_key"] == "K"
    assert "x.com/u/status/1" in captured["body"]


@pytest.mark.asyncio
async def test_exa_returns_none_on_empty_results():
    def _handler(request): return httpx.Response(200, json={"results": []})
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await exa.fetch_content("https://x", api_key="K", client=client)
    assert result is None


# ---- scrape_url dispatcher ------------------------------------------------

def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        ZYTE_API_KEY="", EXA_API_KEY="",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_scrape_url_routes_hn_to_hn_scraper():
    from bot.ingest import hn as hn_mod

    fake_story = hn_mod.HnStory(
        id=5, title="ok", url=None, by="me", score=1, text="the post",
        comments=[{"id": 1, "by": "a", "text": "cmt", "time": 0}],
    )
    with patch("bot.ingest.router.hn.fetch_story", AsyncMock(return_value=fake_story)) as m:
        result = await scrape_url(
            "https://news.ycombinator.com/item?id=5",
            settings=_settings(),
        )
    m.assert_awaited_once()
    assert result.source == "hn"
    assert "the post" in result.content
    assert result.payload["story"]["id"] == 5


@pytest.mark.asyncio
async def test_scrape_url_routes_x_and_reddit_via_exa():
    fake_ec = exa.ExaContent(url="https://x.com/u/status/1", title="t", author="u", text="body")
    with patch("bot.ingest.router.exa.fetch_content", AsyncMock(return_value=fake_ec)) as m:
        result = await scrape_url(
            "https://x.com/u/status/1", settings=_settings(EXA_API_KEY="K"),
        )
    assert result.source == "x"
    assert result.content.startswith("t\n\nbody")
    m.assert_awaited_once()

    with patch("bot.ingest.router.exa.fetch_content", AsyncMock(return_value=fake_ec)) as m:
        result = await scrape_url(
            "https://reddit.com/r/foo", settings=_settings(EXA_API_KEY="K"),
        )
    assert result.source == "reddit"
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_scrape_url_x_without_exa_key_returns_error():
    result = await scrape_url("https://x.com/u/status/1", settings=_settings())
    assert result.source == "x"
    assert result.error and "EXA_API_KEY" in result.error
    assert result.content == "https://x.com/u/status/1"


@pytest.mark.asyncio
async def test_scrape_url_retries_with_zyte_when_generic_returns_raw():
    raw = ExtractedArticle(title=None, text="<js-only-shell>", method="raw")
    fixed = ExtractedArticle(title="Full", text="Rendered body text", method="readability")

    with patch("bot.ingest.router.generic.extract_article", AsyncMock(return_value=raw)) as mg, \
         patch("bot.ingest.router.zyte.extract_with_zyte", AsyncMock(return_value=fixed)) as mz:
        result = await scrape_url(
            "https://hard.example/a", settings=_settings(ZYTE_API_KEY="K"),
        )
    mg.assert_awaited_once()
    mz.assert_awaited_once()
    assert result.source == "article"
    assert result.payload["method"] == "readability"
    assert "Rendered body" in result.content


@pytest.mark.asyncio
async def test_scrape_url_generic_success_skips_zyte():
    ok = ExtractedArticle(title="T", text="body", method="trafilatura")
    with patch("bot.ingest.router.generic.extract_article", AsyncMock(return_value=ok)), \
         patch("bot.ingest.router.zyte.extract_with_zyte", AsyncMock(return_value=None)) as mz:
        result = await scrape_url("https://example.com/a", settings=_settings(ZYTE_API_KEY="K"))
    mz.assert_not_awaited()
    assert result.payload["method"] == "trafilatura"
