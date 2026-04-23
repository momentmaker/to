"""YouTube ingestion — title + description + captions.

Three parallel fetches per URL, all best-effort and independent:
  - oEmbed (title + channel name) — public, stable, rarely blocked
  - watch-page HTML scrape (og:description) — same source Telegram's link
    preview uses; less aggressively gated than the transcript endpoint
  - transcript via youtube-transcript-api — the ambitious one, prone to
    IP-blocking on VPS datacenter IPs

No API key required for any of these.

`fetch()` returns a YouTubeContent populated with whatever succeeded,
never None (except when the URL isn't a YouTube URL at all). Transcript
failures are classified into FAIL_* strings and carried on
`transcript_error` — the router decides whether to surface that as
scrape_error based on whether any other metadata came back.
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
_WATCH_URL = "https://www.youtube.com/watch"
_OEMBED_TIMEOUT = 10.0
_WATCH_TIMEOUT = 10.0
# YouTube video IDs are always exactly 11 chars from [A-Za-z0-9_-].
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Transcript languages to try in order. English first; any-English second.
# If the video has only, say, Japanese captions, we'll fall through and
# the api will raise NoTranscriptFound — we handle that gracefully.
_TRANSCRIPT_LANGS: tuple[str, ...] = ("en", "en-US", "en-GB")
# Cap description length — YouTube allows up to 5000 chars, but a
# commonplace capture doesn't need the full pinned comment thread or
# affiliate-link wall.
_DESCRIPTION_MAX_CHARS = 2000
_WATCH_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass
class YouTubeContent:
    url: str
    video_id: str
    title: str | None        # from oEmbed (may be None on rate-limit / fail)
    author: str | None       # channel name from oEmbed
    description: str | None  # from the watch page's og:description
    text: str                # concatenated caption text, "" if transcripts failed
    language_code: str | None  # None when we have no transcript
    is_auto_generated: bool | None  # None when we have no transcript
    transcript_error: str | None   # FAIL_* string when the transcript part failed


# Distinct failure reasons surfaced back to the user via scrape_error. Lets
# you tell an infrastructure problem (we're IP-banned) from a video problem
# (captions disabled) from a language problem (no English captions), which
# drives very different remediations.
FAIL_IP_BLOCKED = (
    "ip_blocked: YouTube is blocking requests from this server's IP. "
    "VPS datacenter IPs (Coolify, Fly, Hetzner, etc.) get flagged. "
    "needs a proxy or residential IP — see README."
)
FAIL_TRANSCRIPTS_DISABLED = (
    "transcripts_disabled: the uploader turned off captions for this video."
)
FAIL_NO_TRANSCRIPT = (
    "no_transcript: the video has no captions in any language we tried."
)
FAIL_VIDEO_UNAVAILABLE = (
    "video_unavailable: private, deleted, region-locked, or age-restricted."
)
FAIL_UNKNOWN = (
    "transcript unavailable (unknown reason). try again later, or paste a "
    "transcript manually."
)


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


def _unescape_html(text: str) -> str:
    for a, b in (("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"),
                 ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(a, b)
    return text


async def _fetch_description(
    video_id: str, client: httpx.AsyncClient,
) -> str | None:
    """Scrape og:description from the watch page HTML. Telegram's link
    preview does the same thing — the YouTube consumer HTML serves these
    meta tags without the aggressive rate-limiting the transcript endpoint
    gets. Returns None on any failure (blocked page, 429, parse miss)."""
    try:
        resp = await client.get(
            _WATCH_URL,
            params={"v": video_id},
            headers={"User-Agent": _WATCH_UA, "Accept": "text/html"},
            timeout=_WATCH_TIMEOUT,
            follow_redirects=True,
        )
    except Exception as e:
        log.debug("youtube watch page fetch failed for %s: %s", video_id, e)
        return None
    if resp.status_code != 200 or not resp.text:
        return None
    html = resp.text
    m = _OG_DESC_RE.search(html) or _META_DESC_RE.search(html)
    if not m:
        return None
    text = _unescape_html(m.group(1)).strip()
    if not text:
        return None
    if len(text) > _DESCRIPTION_MAX_CHARS:
        text = text[:_DESCRIPTION_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def _fetch_transcript_sync(
    video_id: str, languages: Iterable[str] = _TRANSCRIPT_LANGS,
) -> tuple[str, str, bool] | str:
    """Blocking transcript fetch. Runs in a threadpool from the async caller.

    Returns either:
      - (text, language_code, is_auto) on success, or
      - a FAIL_* string describing the failure mode (for scrape_error).

    Runs two passes: first tries the preferred language list, then falls
    back to any available caption if the first pass saw `NoTranscriptFound`.
    Japanese/Chinese/other-language videos get captured that way instead
    of being rejected for not having English captions.
    """
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        YouTubeTranscriptApiException,
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
        IpBlocked,
        RequestBlocked,
    )

    api = YouTubeTranscriptApi()

    def _extract(fetched) -> tuple[str, str, bool]:
        segments = [seg.text.strip() for seg in fetched.snippets if seg.text.strip()]
        return " ".join(segments).strip(), fetched.language_code, fetched.is_generated

    # Pass 1: preferred languages.
    try:
        fetched = api.fetch(video_id, languages=list(languages))
    except (IpBlocked, RequestBlocked) as e:
        log.warning("youtube IP blocked for %s: %s", video_id, type(e).__name__)
        return FAIL_IP_BLOCKED
    except TranscriptsDisabled:
        log.info("youtube transcripts disabled for %s", video_id)
        return FAIL_TRANSCRIPTS_DISABLED
    except VideoUnavailable:
        log.info("youtube video unavailable for %s", video_id)
        return FAIL_VIDEO_UNAVAILABLE
    except NoTranscriptFound:
        # Fall through to pass 2: any available language.
        fetched = None
    except YouTubeTranscriptApiException as e:
        log.info("youtube transcript unavailable for %s: %s", video_id, type(e).__name__)
        return FAIL_UNKNOWN
    except Exception as e:
        log.warning(
            "youtube transcript unexpected error for %s: %s: %s",
            video_id, type(e).__name__, e,
        )
        return FAIL_UNKNOWN

    # Pass 2: try any language the video has (only entered if pass 1 saw
    # NoTranscriptFound).
    if fetched is None:
        try:
            transcript_list = api.list(video_id)
            transcript = transcript_list.find_transcript(
                [t.language_code for t in transcript_list]
            )
            fetched = transcript.fetch()
        except (IpBlocked, RequestBlocked):
            return FAIL_IP_BLOCKED
        except YouTubeTranscriptApiException as e:
            log.info("youtube no-lang fallback failed for %s: %s", video_id, type(e).__name__)
            return FAIL_NO_TRANSCRIPT
        except Exception as e:
            log.warning(
                "youtube no-lang fallback unexpected error for %s: %s: %s",
                video_id, type(e).__name__, e,
            )
            return FAIL_NO_TRANSCRIPT

    text, language_code, is_generated = _extract(fetched)
    if not text:
        return FAIL_NO_TRANSCRIPT
    return text, language_code, is_generated


async def fetch(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> YouTubeContent | None:
    """Best-effort fetch of a YouTube URL's metadata + captions.

    Returns None only when the URL isn't a YouTube URL at all. In every
    other case — transcripts disabled, IP blocked, private video, no
    captions — returns a YouTubeContent populated with whatever we COULD
    get (title / author / description). That way a YouTube capture
    always has more content than a bare URL, matching what Telegram's
    own link preview shows.

    The `transcript_error` field carries the FAIL_* string when the
    transcript part specifically failed, so the caller can surface that
    in scrape_error while still using the metadata for content.
    """
    video_id = extract_video_id(url)
    if video_id is None:
        return None

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_OEMBED_TIMEOUT)
    try:
        # All three fetches run in parallel — the transcript call is the
        # slowest and most likely to fail, but oEmbed + description almost
        # always succeed (different endpoint, less aggressively rate-limited).
        oembed_task = _fetch_oembed(url, client)
        description_task = _fetch_description(video_id, client)
        transcript_task = asyncio.to_thread(_fetch_transcript_sync, video_id)

        (title, author), description, transcript = await asyncio.gather(
            oembed_task, description_task, transcript_task,
        )

        if isinstance(transcript, str):
            # Transcript failed with a classified reason; keep metadata.
            return YouTubeContent(
                url=url, video_id=video_id,
                title=title, author=author, description=description,
                text="", language_code=None, is_auto_generated=None,
                transcript_error=transcript,
            )
        text, language_code, is_auto = transcript
        return YouTubeContent(
            url=url, video_id=video_id,
            title=title, author=author, description=description,
            text=text, language_code=language_code, is_auto_generated=is_auto,
            transcript_error=None,
        )
    finally:
        if owned:
            await client.aclose()
