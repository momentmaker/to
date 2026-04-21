"""X (Twitter) posting for daily reflections + weekly digests.

Opt-in via `X_{DAILY|WEEKLY}_ENABLED=true` AND all four OAuth1 creds set.
Never posts without explicit enablement — a silent deploy that starts
tweeting is worse than one that doesn't tweet at all.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import aiosqlite

from bot.config import Settings
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.prompts import SYSTEM_TWEET_DAILY, SYSTEM_TWEET_WEEKLY

log = logging.getLogger(__name__)


_TWEET_MAX = 260


def is_configured_for_daily(settings: Settings) -> bool:
    return settings.X_DAILY_ENABLED and _oauth_configured(settings)


def is_configured_for_weekly(settings: Settings) -> bool:
    return settings.X_WEEKLY_ENABLED and _oauth_configured(settings)


def _oauth_configured(settings: Settings) -> bool:
    return all([
        settings.X_CONSUMER_KEY,
        settings.X_CONSUMER_SECRET,
        settings.X_ACCESS_TOKEN,
        settings.X_ACCESS_TOKEN_SECRET,
    ])


# ---- text preparation ----------------------------------------------------

def _coerce_tweet_text(raw: str) -> str:
    """Extract the tweet text from a JSON-wrapped LLM response."""
    if not raw:
        return ""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            return cleaned.strip()[:_TWEET_MAX]
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return cleaned.strip()[:_TWEET_MAX]
    if isinstance(obj, dict):
        text = obj.get("tweet")
        if isinstance(text, str):
            return text.strip()
    return ""


def truncate_tweet(text: str, *, limit: int = _TWEET_MAX) -> str:
    """Cap tweet length in graphemes (not bytes). Adds no ellipsis — we'd
    rather show a clean truncation than eat characters for `…`.
    """
    import grapheme
    stripped = (text or "").strip()
    if grapheme.length(stripped) <= limit:
        return stripped
    kept: list[str] = []
    for i, g in enumerate(grapheme.graphemes(stripped)):
        if i >= limit:
            break
        kept.append(g)
    return "".join(kept).rstrip()


# ---- LLM-driven tweet generation -----------------------------------------

async def generate_daily_tweet(
    *,
    fragments_text: str,
    reflection: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> str:
    """Produce a ≤260-char tweet from the day's captures + reflection.
    Returns empty string on failure (caller should skip posting).
    """
    user_content = (
        f"Today's fragments:\n{fragments_text}\n\nReflection:\n{reflection}"
    )
    try:
        response = await call_llm(
            purpose="tweet",
            system_blocks=[SYSTEM_TWEET_DAILY],
            messages=[Message(role="user", content=user_content)],
            max_tokens=200,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        log.exception("tweet: daily generation failed")
        return ""
    return truncate_tweet(_coerce_tweet_text(response.text))


_DIGEST_WEEK_RE = re.compile(r"^#\s+(\S+)")
_DIGEST_MARK_WHISPER_RE = re.compile(r"^\*\*(.+?)\*\*\s+_(.+?)_\s*$")


def parse_digest_md(text: str) -> dict | None:
    """Parse the digest.md format produced by both the bot and the local CLI:

        # 2026-W17

        **☲**  _a week of small ignitions_

        <essay>

    Returns {iso_week, mark, whisper, essay} or None if the format is off.
    """
    if not text:
        return None
    lines = text.splitlines()
    if len(lines) < 3:
        return None
    m_week = _DIGEST_WEEK_RE.match(lines[0])
    if not m_week:
        return None
    # Find the mark/whisper line, skipping blanks.
    idx = 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return None
    m_mw = _DIGEST_MARK_WHISPER_RE.match(lines[idx])
    if not m_mw:
        return None
    essay = "\n".join(lines[idx + 1:]).strip()
    return {
        "iso_week": m_week.group(1),
        "mark": m_mw.group(1),
        "whisper": m_mw.group(2),
        "essay": essay,
    }


async def generate_weekly_tweet(
    *,
    mark: str,
    whisper: str,
    essay: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> str:
    user_content = (
        f"Mark: {mark}\nWhisper: {whisper}\n\nEssay:\n{essay}"
    )
    try:
        response = await call_llm(
            purpose="tweet",
            system_blocks=[SYSTEM_TWEET_WEEKLY],
            messages=[Message(role="user", content=user_content)],
            max_tokens=200,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        log.exception("tweet: weekly generation failed")
        return ""
    return truncate_tweet(_coerce_tweet_text(response.text))


# ---- posting -------------------------------------------------------------

@dataclass
class TweetResult:
    id: str
    url: str


async def post_tweet(text: str, *, settings: Settings) -> TweetResult | None:
    """Post `text` to X. Returns TweetResult on success, None on failure or
    when OAuth isn't configured.
    """
    if not _oauth_configured(settings):
        log.debug("tweet: OAuth not configured, skipping post")
        return None
    if not text:
        return None
    trimmed = truncate_tweet(text)
    try:
        from tweepy.asynchronous import AsyncClient  # type: ignore
    except ImportError:
        log.warning("tweet: tweepy not installed, skipping post")
        return None
    client = AsyncClient(
        consumer_key=settings.X_CONSUMER_KEY,
        consumer_secret=settings.X_CONSUMER_SECRET,
        access_token=settings.X_ACCESS_TOKEN,
        access_token_secret=settings.X_ACCESS_TOKEN_SECRET,
        wait_on_rate_limit=False,
    )
    try:
        response = await client.create_tweet(text=trimmed)
    except Exception:
        log.exception("tweet: create_tweet failed")
        return None
    data = getattr(response, "data", None) or {}
    tid = str(data.get("id") or "")
    if not tid:
        log.warning("tweet: create_tweet returned no id")
        return None
    return TweetResult(id=tid, url=f"https://x.com/i/web/status/{tid}")
