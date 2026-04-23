"""YouTube transcript ingestion.

Pulls captions (auto-generated or manual) via youtube-transcript-api, plus
title+author via YouTube's free oEmbed endpoint. Feeds the existing ingest
pipeline so you get {title, tags, quotes, summary} derived from the video
the same way articles do.

No API key required for either call. youtube-transcript-api hits the same
unofficial endpoint the YouTube player uses; oEmbed is a public, stable
URL (https://www.youtube.com/oembed?url=...&format=json).

Failure modes handled:
  - private / deleted / age-restricted videos → None (bare-URL fallback)
  - no captions (rare, usually auto-caps exist) → None
  - YouTube temporarily rate-limiting our IP → None
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)


_OEMBED_URL = "https://www.youtube.com/oembed"
_OEMBED_TIMEOUT = 10.0
# YouTube video IDs are always exactly 11 chars from [A-Za-z0-9_-].
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Transcript languages to try in order. English first; any-English second.
# If the video has only, say, Japanese captions, we'll fall through and
# the api will raise NoTranscriptFound — we handle that gracefully.
_TRANSCRIPT_LANGS: tuple[str, ...] = ("en", "en-US", "en-GB")


@dataclass
class YouTubeContent:
    url: str
    video_id: str
    title: str | None       # from oEmbed (may be None on rate-limit / fail)
    author: str | None      # channel name from oEmbed
    text: str               # concatenated caption text
    language_code: str      # e.g. "en", "en-US"
    is_auto_generated: bool


def _is_youtube_host(host: str) -> bool:
    host = host.lower()
    return (
        host in ("youtube.com", "youtu.be")
        or host.endswith(".youtube.com")
    )


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char video ID out of the common YouTube URL forms.

    Handles:
      https://www.youtube.com/watch?v=ID
      https://youtu.be/ID
      https://youtube.com/shorts/ID
      https://youtube.com/embed/ID
      https://youtube.com/live/ID
      https://m.youtube.com/watch?v=ID
      (with or without additional query params / trailing slashes)
    """
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    if not _is_youtube_host(host):
        return None

    # youtu.be/ID
    if host == "youtu.be":
        candidate = (p.path or "").lstrip("/").split("/", 1)[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    # youtube.com/watch?v=ID
    if (p.path or "").rstrip("/") in ("", "/watch"):
        q = parse_qs(p.query or "")
        v = (q.get("v") or [""])[0]
        return v if _VIDEO_ID_RE.match(v) else None

    # /shorts/ID, /embed/ID, /live/ID, /v/ID
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if (p.path or "").startswith(prefix):
            candidate = p.path[len(prefix):].split("/", 1)[0]
            return candidate if _VIDEO_ID_RE.match(candidate) else None

    return None


async def _fetch_oembed(
    url: str, client: httpx.AsyncClient,
) -> tuple[str | None, str | None]:
    """Return (title, author_name) from YouTube's oEmbed endpoint. Both
    fields can be None if the call fails — the caller should still proceed
    with the transcript extraction."""
    try:
        resp = await client.get(
            _OEMBED_URL,
            params={"url": url, "format": "json"},
            timeout=_OEMBED_TIMEOUT,
        )
    except Exception as e:
        log.debug("youtube oembed failed for %s: %s", url, e)
        return None, None
    if resp.status_code != 200:
        return None, None
    try:
        data = resp.json()
    except Exception:
        return None, None
    return data.get("title"), data.get("author_name")


def _fetch_transcript_sync(
    video_id: str, languages: Iterable[str] = _TRANSCRIPT_LANGS,
) -> tuple[str, str, bool] | None:
    """Blocking transcript fetch. Returns (text, language_code, is_auto) or
    None on any failure. Runs in a threadpool from the async caller."""
    from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    except YouTubeTranscriptApiException as e:
        log.info("youtube transcript unavailable for %s: %s", video_id, type(e).__name__)
        return None
    except Exception as e:
        # Network issues, library bugs, etc. Don't let these bubble up to
        # the Telegram handler.
        log.warning(
            "youtube transcript unexpected error for %s: %s: %s",
            video_id, type(e).__name__, e,
        )
        return None

    segments = [seg.text.strip() for seg in fetched.snippets if seg.text.strip()]
    text = " ".join(segments).strip()
    if not text:
        return None
    return text, fetched.language_code, fetched.is_generated


async def fetch_transcript(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> YouTubeContent | None:
    """Async fetch of captions + title for a YouTube URL. Returns None on
    any failure (private video, no captions, rate-limited, etc.) — caller
    should degrade to a bare-URL capture."""
    video_id = extract_video_id(url)
    if video_id is None:
        return None

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_OEMBED_TIMEOUT)
    try:
        oembed_task = _fetch_oembed(url, client)
        # youtube-transcript-api is synchronous; run it off the event loop
        # so we don't block other captures' handlers.
        transcript_task = asyncio.to_thread(_fetch_transcript_sync, video_id)
        (title, author), transcript = await asyncio.gather(oembed_task, transcript_task)

        if transcript is None:
            return None
        text, language_code, is_auto = transcript
        return YouTubeContent(
            url=url,
            video_id=video_id,
            title=title,
            author=author,
            text=text,
            language_code=language_code,
            is_auto_generated=is_auto,
        )
    finally:
        if owned:
            await client.aclose()
