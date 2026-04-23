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


def test_extract_id_from_legacy_v_url():
    """The old /v/ID embed form still shows up in the wild."""
    assert youtube.extract_video_id(
        "https://www.youtube.com/v/dQw4w9WgXcQ"
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


def test_sync_fetch_returns_reason_on_transcripts_disabled(monkeypatch):
    from youtube_transcript_api import TranscriptsDisabled

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise TranscriptsDisabled(vid)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result == youtube.FAIL_TRANSCRIPTS_DISABLED


def test_sync_fetch_returns_reason_on_video_unavailable(monkeypatch):
    from youtube_transcript_api import VideoUnavailable

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise VideoUnavailable(vid)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result == youtube.FAIL_VIDEO_UNAVAILABLE


def test_sync_fetch_returns_ip_blocked_reason(monkeypatch):
    """The important one: when YouTube blocks the server's IP, give the
    user a distinct message so they know it's an infrastructure problem,
    not a per-video one."""
    from youtube_transcript_api import IpBlocked

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise IpBlocked(vid)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result == youtube.FAIL_IP_BLOCKED
    assert "ip_blocked" in result.lower()
    assert "proxy" in result.lower() or "residential" in result.lower()


def test_sync_fetch_falls_back_to_any_language(monkeypatch):
    """If English isn't available, fall back to the first available
    caption language — better to capture a Japanese video's Japanese
    captions than give up entirely."""
    from youtube_transcript_api import NoTranscriptFound

    call_count = {"fetch": 0}
    ja_fetched = _fake_fetched(
        [_fake_snippet("日本語の字幕")], language_code="ja", is_generated=False,
    )

    class _FakeTranscript:
        language_code = "ja"
        def fetch(self): return ja_fetched

    class _FakeTranscriptList(list):
        def find_transcript(self, langs):
            return _FakeTranscript()

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            call_count["fetch"] += 1
            raise NoTranscriptFound(vid, list(languages), None)
        def list(self, vid):
            return _FakeTranscriptList([_FakeTranscript()])
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert isinstance(result, tuple)
    text, lang, _ = result
    assert lang == "ja"
    assert "日本語" in text
    # First-pass attempted English; fallback then ran.
    assert call_count["fetch"] == 1


def test_sync_fetch_returns_no_transcript_when_fallback_also_fails(monkeypatch):
    from youtube_transcript_api import NoTranscriptFound

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise NoTranscriptFound(vid, list(languages), None)
        def list(self, vid):
            raise NoTranscriptFound(vid, [], None)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result == youtube.FAIL_NO_TRANSCRIPT


def test_sync_fetch_returns_reason_on_unexpected_error(monkeypatch):
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise RuntimeError("connection reset")
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result == youtube.FAIL_UNKNOWN


def test_sync_fetch_returns_reason_on_empty_transcript(monkeypatch):
    """All whitespace-only snippets should collapse to empty → no_transcript."""
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            return _fake_fetched([_fake_snippet("   "), _fake_snippet("\t")])
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    result = youtube._fetch_transcript_sync("abc12345678")
    assert result == youtube.FAIL_NO_TRANSCRIPT


# ---- fetch_transcript (integration) --------------------------------------

_WATCH_HTML = """
<html><head>
<meta property="og:description" content="Patrick sits down with Dylan to explore AI token dynamics.">
</head><body></body></html>
"""


@pytest.mark.asyncio
async def test_fetch_happy_path_returns_full_metadata_and_transcript(monkeypatch):
    """End-to-end: valid URL → oEmbed (title+author) + watch page scrape
    (description) + captions → fully populated YouTubeContent."""
    def _handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "oembed" in u:
            return httpx.Response(200, json={
                "title": "Never Gonna Give You Up",
                "author_name": "Rick Astley",
            })
        if "/watch" in u:
            return httpx.Response(200, text=_WATCH_HTML)
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
        out = await youtube.fetch(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            client=client,
        )
    assert out is not None
    assert out.video_id == "dQw4w9WgXcQ"
    assert out.title == "Never Gonna Give You Up"
    assert out.author == "Rick Astley"
    assert out.description and "dynamics" in out.description
    assert "strangers to love" in out.text
    assert out.language_code == "en"
    assert out.is_auto_generated is True
    assert out.transcript_error is None


@pytest.mark.asyncio
async def test_fetch_returns_metadata_when_captions_fail(monkeypatch):
    """The critical case: transcript fetch fails (IP blocked), but oEmbed
    and description still succeed. YouTubeContent should come back with
    usable metadata and transcript_error set — NOT None."""
    def _handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "oembed" in u:
            return httpx.Response(200, json={"title": "t", "author_name": "a"})
        if "/watch" in u:
            return httpx.Response(200, text=_WATCH_HTML)
        return httpx.Response(404)

    from youtube_transcript_api import IpBlocked
    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            raise IpBlocked(vid)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await youtube.fetch(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            client=client,
        )
    assert out is not None  # NOT None — metadata is still useful
    assert out.title == "t"
    assert out.author == "a"
    assert out.description and "dynamics" in out.description
    assert out.text == ""
    assert out.language_code is None
    assert out.transcript_error == youtube.FAIL_IP_BLOCKED


@pytest.mark.asyncio
async def test_fetch_survives_oembed_failure(monkeypatch):
    """oEmbed 500s but captions still succeed → YouTubeContent with
    title=None, author=None, but transcript intact."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    class _FakeApi:
        def __init__(self): pass
        def fetch(self, vid, languages):
            return _fake_fetched([_fake_snippet("a line")])
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await youtube.fetch(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            client=client,
        )
    assert out is not None
    assert out.title is None
    assert out.author is None
    assert out.description is None
    assert out.text == "a line"


@pytest.mark.asyncio
async def test_fetch_returns_none_for_non_youtube():
    async with httpx.AsyncClient() as client:
        out = await youtube.fetch("https://example.com/page", client=client)
    assert out is None


# ---- description-scrape unit tests ----------------------------------------

@pytest.mark.asyncio
async def test_fetch_description_parses_og_tag():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_WATCH_HTML)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        desc = await youtube._fetch_description("dQw4w9WgXcQ", client)
    assert desc and "Patrick" in desc and "Dylan" in desc


@pytest.mark.asyncio
async def test_fetch_description_returns_none_on_http_error():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)  # rate-limited
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        desc = await youtube._fetch_description("dQw4w9WgXcQ", client)
    assert desc is None


@pytest.mark.asyncio
async def test_fetch_description_truncates_very_long_bodies():
    long_html = (
        '<meta property="og:description" content="'
        + ("spam link affiliate " * 500)
        + '">'
    )
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=long_html)
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        desc = await youtube._fetch_description("dQw4w9WgXcQ", client)
    assert desc is not None
    assert len(desc) <= youtube._DESCRIPTION_MAX_CHARS + 1  # +1 for "…"
    assert desc.endswith("…")


# ---- router integration --------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_url_routes_youtube_with_full_payload():
    from bot.config import Settings
    from bot.ingest.router import scrape_url
    fake = youtube.YouTubeContent(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        author="Rick Astley",
        description="The classic.",
        text="we're no strangers to love",
        language_code="en",
        is_auto_generated=False,
        transcript_error=None,
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k",
    )
    with patch(
        "bot.ingest.router.youtube.fetch",
        AsyncMock(return_value=fake),
    ) as m:
        result = await scrape_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings=settings,
        )
    m.assert_awaited_once()
    assert result.source == "youtube"
    assert result.error is None
    # Content has title, author, description, and transcript — in that order.
    for expected in ("Never Gonna Give You Up", "Rick Astley", "classic", "strangers to love"):
        assert expected in result.content
    assert result.payload["video_id"] == "dQw4w9WgXcQ"
    assert result.payload["title"] == "Never Gonna Give You Up"
    assert result.payload["description"] == "The classic."
    assert result.payload["is_auto_generated"] is False
    # Happy path: transcript_error field is present but None.
    assert result.payload["transcript_error"] is None


@pytest.mark.asyncio
async def test_scrape_url_youtube_uses_metadata_when_transcript_blocked():
    """Key regression for the IP-blocked case: even when transcripts fail,
    we still get a useful capture from title+description. No scrape_error
    should be surfaced (the capture isn't 'thin', it has real content)."""
    from bot.config import Settings
    from bot.ingest.router import scrape_url
    fake = youtube.YouTubeContent(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title="The Supply and Demand of AI Tokens",
        author="Invest Like the Best",
        description="Patrick sits down with Dylan Patel to explore AI token dynamics.",
        text="",
        language_code=None,
        is_auto_generated=None,
        transcript_error=youtube.FAIL_IP_BLOCKED,
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k",
    )
    with patch(
        "bot.ingest.router.youtube.fetch",
        AsyncMock(return_value=fake),
    ):
        result = await scrape_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings=settings,
        )
    assert result.source == "youtube"
    # No scrape_error — we HAVE useful metadata, the capture is fine.
    assert result.error is None
    for expected in ("Supply and Demand", "Invest Like the Best", "Dylan Patel"):
        assert expected in result.content
    assert result.payload["title"] == "The Supply and Demand of AI Tokens"
    assert result.payload["description"] is not None
    # Transcript error IS preserved in payload for diagnostics, just not
    # surfaced as scrape_error since we have metadata.
    assert result.payload.get("text") == ""
    assert result.payload.get("transcript_error") == youtube.FAIL_IP_BLOCKED


@pytest.mark.asyncio
async def test_scrape_url_youtube_surfaces_error_only_when_truly_empty():
    """If transcript fails AND we got no title AND no description (total
    blackout), THEN surface the classified error in scrape_error."""
    from bot.config import Settings
    from bot.ingest.router import scrape_url
    fake = youtube.YouTubeContent(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title=None, author=None, description=None,
        text="", language_code=None, is_auto_generated=None,
        transcript_error=youtube.FAIL_IP_BLOCKED,
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k",
    )
    with patch(
        "bot.ingest.router.youtube.fetch",
        AsyncMock(return_value=fake),
    ):
        result = await scrape_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings=settings,
        )
    assert result.source == "youtube"
    assert result.error == youtube.FAIL_IP_BLOCKED
