"""Tests for bot.ingest.youtube: URL parsing, transcript fetching,
oEmbed metadata, and router integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.ingest import youtube
from bot.ingest.urls import classify_url


# ---- URL classification --------------------------------------------------

def test_classify_youtube_watch_url():
    assert classify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"


def test_classify_youtu_be_short_url():
    assert classify_url("https://youtu.be/dQw4w9WgXcQ") == "youtube"


def test_classify_youtube_mobile():
    assert classify_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"


def test_classify_non_youtube_stays_generic():
    assert classify_url("https://example.com/page") == "generic"


# ---- extract_video_id ----------------------------------------------------

def test_extract_id_from_watch_url():
    assert youtube.extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_from_watch_url_with_extra_params():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&feature=share"
    assert youtube.extract_video_id(url) == "dQw4w9WgXcQ"


def test_extract_id_from_short_url():
    assert youtube.extract_video_id(
        "https://youtu.be/dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_from_short_url_with_query():
    assert youtube.extract_video_id(
        "https://youtu.be/dQw4w9WgXcQ?t=30"
    ) == "dQw4w9WgXcQ"


def test_extract_id_from_shorts_url():
    assert youtube.extract_video_id(
        "https://www.youtube.com/shorts/dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_from_embed_url():
    assert youtube.extract_video_id(
        "https://www.youtube.com/embed/dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_from_live_url():
    assert youtube.extract_video_id(
        "https://www.youtube.com/live/dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_returns_none_for_non_youtube():
    assert youtube.extract_video_id("https://example.com/page") is None


def test_extract_id_returns_none_when_v_missing():
    assert youtube.extract_video_id("https://www.youtube.com/watch") is None


def test_extract_id_returns_none_for_malformed_id():
    # ID must be exactly 11 chars from [A-Za-z0-9_-]
    assert youtube.extract_video_id(
        "https://youtu.be/abc"  # too short
    ) is None
    assert youtube.extract_video_id(
        "https://youtu.be/this-is-not-valid!!"
    ) is None


# ---- _fetch_transcript_sync (mocked) -------------------------------------

def _fake_snippet(text: str, start: float = 0.0, duration: float = 2.0):
    s = MagicMock()
    s.text = text
    s.start = start
    s.duration = duration
    return s


def _fake_fetched(snippets: list, language_code: str = "en", is_generated: bool = False):
    f = MagicMock()
    f.snippets = snippets
    f.language_code = language_code
    f.is_generated = is_generated
    return f


def test_sync_fetch_concatenates_snippets(monkeypatch):
    fake = _fake_fetched([
        _fake_snippet("  line one  "),
        _fake_snippet("line two"),
        _fake_snippet("   "),   # empty-after-strip, skipped
        _fake_snippet("line three"),
    ])
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            return fake
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result is not None
    text, lang, is_auto = result
    assert text == "line one line two line three"
    assert lang == "en"
    assert is_auto is False


def test_sync_fetch_returns_none_on_transcripts_disabled(monkeypatch):
    from youtube_transcript_api._errors import TranscriptsDisabled

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise TranscriptsDisabled(vid)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    assert youtube._fetch_transcript_sync("abc12345678") is None


def test_sync_fetch_returns_none_on_no_transcript_found(monkeypatch):
    from youtube_transcript_api._errors import NoTranscriptFound

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise NoTranscriptFound(vid, list(languages), None)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    assert youtube._fetch_transcript_sync("abc12345678") is None


def test_sync_fetch_swallows_unexpected_errors(monkeypatch):
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise RuntimeError("connection reset")
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    assert youtube._fetch_transcript_sync("abc12345678") is None


def test_sync_fetch_returns_none_on_empty_transcript(monkeypatch):
    """All whitespace-only snippets should collapse to empty → None."""
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            return _fake_fetched([_fake_snippet("   "), _fake_snippet("\t")])
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    assert youtube._fetch_transcript_sync("abc12345678") is None


# ---- fetch_transcript (integration) --------------------------------------

@pytest.mark.asyncio
async def test_fetch_transcript_happy_path(monkeypatch):
    """End-to-end: valid URL → oEmbed metadata + captions → YouTubeContent."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if "oembed" in str(request.url):
            return httpx.Response(200, json={
                "title": "Never Gonna Give You Up",
                "author_name": "Rick Astley",
            })
        return httpx.Response(404)

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            return _fake_fetched([
                _fake_snippet("we're no strangers to love"),
                _fake_snippet("you know the rules and so do i"),
            ], language_code="en", is_generated=True)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await youtube.fetch_transcript(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            client=client,
        )
    assert out is not None
    assert out.video_id == "dQw4w9WgXcQ"
    assert out.title == "Never Gonna Give You Up"
    assert out.author == "Rick Astley"
    assert "strangers to love" in out.text
    assert out.language_code == "en"
    assert out.is_auto_generated is True


@pytest.mark.asyncio
async def test_fetch_transcript_returns_none_when_captions_fail(monkeypatch):
    """oEmbed succeeds, captions fail → function returns None (caller
    handles the bare-URL fallback)."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "t", "author_name": "a"})

    from youtube_transcript_api._errors import TranscriptsDisabled
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise TranscriptsDisabled(vid)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await youtube.fetch_transcript(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            client=client,
        )
    assert out is None


@pytest.mark.asyncio
async def test_fetch_transcript_survives_oembed_failure(monkeypatch):
    """oEmbed 500s but captions still succeed → YouTubeContent with
    title=None, author=None. We value the captions over the metadata."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            return _fake_fetched([_fake_snippet("a line")])
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await youtube.fetch_transcript(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            client=client,
        )
    assert out is not None
    assert out.title is None
    assert out.author is None
    assert out.text == "a line"


@pytest.mark.asyncio
async def test_fetch_transcript_returns_none_for_non_youtube():
    async with httpx.AsyncClient() as client:
        out = await youtube.fetch_transcript(
            "https://example.com/page", client=client,
        )
    assert out is None


# ---- router integration --------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_url_routes_youtube_to_transcript():
    from bot.config import Settings
    from bot.ingest.router import scrape_url
    fake = youtube.YouTubeContent(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        author="Rick Astley",
        text="we're no strangers to love",
        language_code="en",
        is_auto_generated=False,
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k",
    )
    with patch(
        "bot.ingest.router.youtube.fetch_transcript",
        AsyncMock(return_value=fake),
    ) as m:
        result = await scrape_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings=settings,
        )
    m.assert_awaited_once()
    assert result.source == "youtube"
    assert result.error is None
    assert "Never Gonna Give You Up" in result.content
    assert "Rick Astley" in result.content
    assert "strangers to love" in result.content
    assert result.payload["video_id"] == "dQw4w9WgXcQ"
    assert result.payload["title"] == "Never Gonna Give You Up"
    assert result.payload["is_auto_generated"] is False


@pytest.mark.asyncio
async def test_scrape_url_youtube_degrades_when_transcript_fails():
    from bot.config import Settings
    from bot.ingest.router import scrape_url
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k",
    )
    with patch(
        "bot.ingest.router.youtube.fetch_transcript",
        AsyncMock(return_value=None),
    ):
        result = await scrape_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings=settings,
        )
    assert result.source == "youtube"
    assert result.content == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert result.error is not None
    assert "transcript" in result.error.lower()
