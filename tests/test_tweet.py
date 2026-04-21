from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import tweet as tweet_mod
from bot.config import Settings
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        X_DAILY_ENABLED=False, X_WEEKLY_ENABLED=False,
        X_CONSUMER_KEY="", X_CONSUMER_SECRET="",
        X_ACCESS_TOKEN="", X_ACCESS_TOKEN_SECRET="",
    )
    base.update(kw)
    return Settings(**base)


def _fully_configured(**kw):
    return _settings(
        X_DAILY_ENABLED=True, X_WEEKLY_ENABLED=True,
        X_CONSUMER_KEY="ck", X_CONSUMER_SECRET="cs",
        X_ACCESS_TOKEN="at", X_ACCESS_TOKEN_SECRET="ats",
        **kw,
    )


# ---- configuration gates -------------------------------------------------

def test_is_configured_for_daily_requires_enabled_and_oauth():
    # Enabled but no OAuth
    s = _settings(X_DAILY_ENABLED=True)
    assert not tweet_mod.is_configured_for_daily(s)
    # OAuth but not enabled
    s = _settings(
        X_CONSUMER_KEY="ck", X_CONSUMER_SECRET="cs",
        X_ACCESS_TOKEN="at", X_ACCESS_TOKEN_SECRET="ats",
    )
    assert not tweet_mod.is_configured_for_daily(s)
    # Both
    assert tweet_mod.is_configured_for_daily(_fully_configured())


def test_is_configured_for_weekly_requires_enabled_and_oauth():
    s = _settings(X_WEEKLY_ENABLED=True)
    assert not tweet_mod.is_configured_for_weekly(s)
    assert tweet_mod.is_configured_for_weekly(_fully_configured())


# ---- truncation ----------------------------------------------------------

def test_tweet_truncation_respects_260():
    short = "a short line."
    assert tweet_mod.truncate_tweet(short) == short

    long = "x" * 500
    out = tweet_mod.truncate_tweet(long)
    import grapheme
    assert grapheme.length(out) == 260


def test_tweet_truncation_preserves_single_graphemes():
    """ZWJ sequences and emoji shouldn't get sliced mid-grapheme."""
    emoji = "👨‍👩‍👧"  # family ZWJ = 1 grapheme, multiple code points
    text = emoji * 300
    out = tweet_mod.truncate_tweet(text)
    import grapheme
    assert grapheme.length(out) == 260
    # No partial grapheme at the end
    assert out.endswith(emoji)


def test_coerce_tweet_text_from_various_shapes():
    assert tweet_mod._coerce_tweet_text('{"tweet": "hello"}') == "hello"
    # Code-fenced
    assert tweet_mod._coerce_tweet_text('```json\n{"tweet": "hi"}\n```') == "hi"
    # Nested braces / prose
    out = tweet_mod._coerce_tweet_text('sure, here: {"tweet": "ok"} done')
    assert out == "ok"
    # Malformed — falls through to truncated raw
    assert tweet_mod._coerce_tweet_text("not json") == "not json"


# ---- LLM generation ------------------------------------------------------

class _SeqProv:
    name = "anthropic"
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []
    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        text = self._responses.pop(0) if self._responses else ""
        return LlmResponse(
            text=text, model=kwargs["model"], provider=self.name,
            input_tokens=50, output_tokens=30,
        )


@pytest.mark.asyncio
async def test_generate_daily_tweet_returns_trimmed_text(conn):
    prov = _SeqProv(['{"tweet": "the impediment to action advances action"}'])
    providers = Providers(prov, None)
    text = await tweet_mod.generate_daily_tweet(
        fragments_text="- hello world", reflection="my reflection",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert text == "the impediment to action advances action"


@pytest.mark.asyncio
async def test_generate_daily_tweet_handles_llm_failure(conn):
    class _Broken:
        name = "anthropic"
        async def chat(self, **kwargs): raise RuntimeError("down")
    providers = Providers(_Broken(), None)
    text = await tweet_mod.generate_daily_tweet(
        fragments_text="x", reflection="y",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert text == ""


@pytest.mark.asyncio
async def test_generate_weekly_tweet_returns_trimmed_text(conn):
    prov = _SeqProv(['{"tweet": "☲ a week of small ignitions"}'])
    providers = Providers(prov, None)
    text = await tweet_mod.generate_weekly_tweet(
        mark="☲", whisper="a week of small ignitions", essay="long essay",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert "ignitions" in text


@pytest.mark.asyncio
async def test_tweet_uses_no_orchurator_voice_block(conn):
    """Tweets are in the user's voice, not orchurator's — the VOICE_ORCHURATOR
    persona spec block must NOT be prepended to tweet-generation calls.
    (The SYSTEM_TWEET_* prompts themselves reference "orchurator" by name to
    instruct the LLM — that's expected; what we're asserting against is the
    actual persona-definition block.)
    """
    from bot.persona import VOICE_ORCHURATOR
    prov = _SeqProv([
        '{"tweet": "daily content"}',
        '{"tweet": "weekly content"}',
    ])
    providers = Providers(prov, None)

    await tweet_mod.generate_daily_tweet(
        fragments_text="x", reflection="y",
        settings=_settings(), providers=providers, conn=conn,
    )
    await tweet_mod.generate_weekly_tweet(
        mark="a", whisper="b", essay="c",
        settings=_settings(), providers=providers, conn=conn,
    )
    # Pin against a distinctive phrase from VOICE_ORCHURATOR that would NOT
    # appear in any incidental "orchurator" mention in the tweet prompts.
    persona_marker = "part child, part fool, part sage"
    assert persona_marker in VOICE_ORCHURATOR  # sanity check
    for call in prov.calls:
        joined = "\n\n".join(call["system_blocks"])
        assert persona_marker not in joined, \
            "tweet generation must not include the VOICE_ORCHURATOR persona block"


# ---- post_tweet ----------------------------------------------------------

@pytest.mark.asyncio
async def test_post_tweet_skips_when_oauth_missing():
    result = await tweet_mod.post_tweet("hi", settings=_settings())
    assert result is None


@pytest.mark.asyncio
async def test_post_tweet_skips_on_empty_text():
    result = await tweet_mod.post_tweet("", settings=_fully_configured())
    assert result is None


@pytest.mark.asyncio
async def test_post_tweet_creates_via_tweepy():
    fake_client = MagicMock()
    fake_client.create_tweet = AsyncMock(
        return_value=MagicMock(data={"id": "123456789"})
    )
    fake_module = MagicMock()
    fake_module.AsyncClient = MagicMock(return_value=fake_client)

    with patch.dict("sys.modules", {"tweepy.asynchronous": fake_module}):
        result = await tweet_mod.post_tweet(
            "hello world", settings=_fully_configured(),
        )
    assert result is not None
    assert result.id == "123456789"
    assert result.url == "https://x.com/i/web/status/123456789"
    fake_client.create_tweet.assert_awaited_once_with(text="hello world")


@pytest.mark.asyncio
async def test_post_tweet_handles_tweepy_exception():
    fake_client = MagicMock()
    fake_client.create_tweet = AsyncMock(side_effect=RuntimeError("rate limit"))
    fake_module = MagicMock()
    fake_module.AsyncClient = MagicMock(return_value=fake_client)

    with patch.dict("sys.modules", {"tweepy.asynchronous": fake_module}):
        result = await tweet_mod.post_tweet(
            "hi", settings=_fully_configured(),
        )
    assert result is None


# ---- handler integration -------------------------------------------------

@pytest.mark.asyncio
async def test_daily_tweet_fires_after_reflection(conn, monkeypatch):
    """Regression + integration: reflection reply with X_DAILY_ENABLED
    triggers a background tweet, which updates `daily.tweet_text`.
    """
    import asyncio as _asyncio
    from bot import db as db_mod
    from bot import reflection as reflection_mod
    from bot.handlers import text_message_handler

    settings = _fully_configured(DOB="1990-01-01", TIMEZONE="UTC")
    today = "2026-04-21"

    await conn.execute(
        "INSERT INTO daily (local_date, prompt, prompted_at) VALUES (?, ?, ?)",
        (today, "what caught you?", "2026-04-21T21:30:00Z"),
    )
    await conn.commit()
    await reflection_mod.set_pending(conn, local_date=today, tz_name="UTC")

    # Stub the tweet pipeline so the test isn't making real HTTP calls.
    async def _fake_gen(*, fragments_text, reflection, settings, providers, conn):
        return "a tweet from the reflection"
    async def _fake_post(text, *, settings):
        return tweet_mod.TweetResult(id="9", url="https://x.com/i/web/status/9")

    monkeypatch.setattr(tweet_mod, "generate_daily_tweet", _fake_gen)
    monkeypatch.setattr(tweet_mod, "post_tweet", _fake_post)

    class _Prov:
        name = "anthropic"
        async def chat(self, **kwargs):
            return LlmResponse(
                text='{"title":"","tags":[],"quotes":[],"summary":""}',
                model="m", provider="anthropic", input_tokens=1, output_tokens=1,
            )
    providers = Providers(_Prov(), None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "the way light held the room"
    update.message.message_id = 555
    update.message.forward_origin = None
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.chat.id = 99
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    await text_message_handler(update, context)
    # Drain background tasks
    pending = [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]
    for t in pending:
        try:
            await t
        except Exception:
            pass

    async with conn.execute(
        "SELECT tweet_text FROM daily WHERE local_date = ?", (today,)
    ) as cur:
        row = await cur.fetchone()
    assert row["tweet_text"] == "a tweet from the reflection"


@pytest.mark.asyncio
async def test_tweet_disabled_skips_api_call(conn, monkeypatch):
    """With X_DAILY_ENABLED=false, no tweet task should be scheduled."""
    from bot import reflection as reflection_mod
    from bot.handlers import text_message_handler

    settings = _settings(DOB="1990-01-01", TIMEZONE="UTC")  # X_DAILY_ENABLED=False
    today = "2026-04-21"
    await conn.execute(
        "INSERT INTO daily (local_date, prompt, prompted_at) VALUES (?, ?, ?)",
        (today, "q", "2026-04-21T21:30:00Z"),
    )
    await conn.commit()
    await reflection_mod.set_pending(conn, local_date=today, tz_name="UTC")

    gen_calls = [0]
    async def _gen_spy(**kwargs):
        gen_calls[0] += 1
        return ""
    monkeypatch.setattr(tweet_mod, "generate_daily_tweet", _gen_spy)

    class _Prov:
        name = "anthropic"
        async def chat(self, **kwargs):
            return LlmResponse(text="{}", model="m", provider="anthropic",
                               input_tokens=1, output_tokens=1)
    providers = Providers(_Prov(), None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "a reflection"
    update.message.message_id = 777
    update.message.forward_origin = None
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.chat.id = 99
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    await text_message_handler(update, context)
    import asyncio as _asyncio
    for t in [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]:
        try:
            await t
        except Exception:
            pass

    assert gen_calls[0] == 0
    async with conn.execute(
        "SELECT tweet_text FROM daily WHERE local_date = ?", (today,)
    ) as cur:
        row = await cur.fetchone()
    assert row["tweet_text"] is None
