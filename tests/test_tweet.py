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


# ---- parse_digest_md -----------------------------------------------------

def test_parse_digest_md_happy_path():
    md = (
        "# 2026-W17\n"
        "\n"
        "**☲**  _a week of small ignitions_\n"
        "\n"
        "paragraph one\n"
        "\n"
        "paragraph two\n"
    )
    out = tweet_mod.parse_digest_md(md)
    assert out == {
        "iso_week": "2026-W17",
        "mark": "☲",
        "whisper": "a week of small ignitions",
        "essay": "paragraph one\n\nparagraph two",
    }


def test_parse_digest_md_returns_none_on_empty_or_short():
    assert tweet_mod.parse_digest_md("") is None
    assert tweet_mod.parse_digest_md(None) is None  # type: ignore[arg-type]
    assert tweet_mod.parse_digest_md("# 2026-W17\n") is None


def test_parse_digest_md_returns_none_on_missing_week_header():
    md = "not a header\n\n**x**  _y_\n\nessay\n"
    assert tweet_mod.parse_digest_md(md) is None


def test_parse_digest_md_returns_none_on_missing_mark_whisper_line():
    md = "# 2026-W17\n\njust prose\n\nessay\n"
    assert tweet_mod.parse_digest_md(md) is None


def test_parse_digest_md_tolerates_multiple_blanks_before_mark():
    md = "# 2026-W17\n\n\n\n**a**  _b_\n\nessay line\n"
    out = tweet_mod.parse_digest_md(md)
    assert out is not None
    assert out["mark"] == "a"
    assert out["essay"] == "essay line"


# ---- /tweetweekly handler ------------------------------------------------

def _tw_settings(**kw):
    return _fully_configured(
        DOB="1990-01-01", TIMEZONE="UTC",
        GITHUB_TOKEN="ghp", GITHUB_REPO="u/r", GITHUB_BRANCH="main",
        **kw,
    )


def _tw_update_context(*, settings, conn, providers, args=None):
    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}
    context.args = args or []
    return update, context


@pytest.mark.asyncio
async def test_tweetweekly_rejects_when_weekly_disabled(conn):
    from bot.handlers import tweetweekly_handler

    settings = _settings(DOB="1990-01-01", TIMEZONE="UTC")  # X_WEEKLY_ENABLED=False
    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=Providers(None, None),
    )
    await tweetweekly_handler(update, context)
    update.message.reply_text.assert_awaited_once()
    msg = update.message.reply_text.await_args.args[0]
    assert "X_WEEKLY_ENABLED" in msg


@pytest.mark.asyncio
async def test_tweetweekly_rejects_when_github_not_configured(conn):
    from bot.handlers import tweetweekly_handler

    settings = _fully_configured(DOB="1990-01-01", TIMEZONE="UTC")  # no GITHUB_*
    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=Providers(_SeqProv([]), None),
    )
    await tweetweekly_handler(update, context)
    msg = update.message.reply_text.await_args.args[0]
    assert "github" in msg.lower()


@pytest.mark.asyncio
async def test_tweetweekly_rejects_bad_week_arg(conn, monkeypatch):
    from bot import github_sync
    from bot.handlers import tweetweekly_handler

    settings = _tw_settings()
    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=Providers(_SeqProv([]), None),
        args=["not-a-week"],
    )
    # fetch_file should never be called
    async def _fetch_shouldnt_run(**kwargs):
        raise AssertionError("fetch_file should not be called on bad arg")
    monkeypatch.setattr(github_sync, "fetch_file", _fetch_shouldnt_run)

    await tweetweekly_handler(update, context)
    msg = update.message.reply_text.await_args.args[0]
    assert "usage" in msg.lower()


@pytest.mark.asyncio
async def test_tweetweekly_handles_missing_digest(conn, monkeypatch):
    from bot import github_sync
    from bot.handlers import tweetweekly_handler

    settings = _tw_settings()
    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=Providers(_SeqProv([]), None),
        args=["2026-w17"],
    )

    async def _fetch_none(**kwargs):
        return None
    monkeypatch.setattr(github_sync, "fetch_file", _fetch_none)

    await tweetweekly_handler(update, context)
    # last reply_text call gets the user-facing error
    last = update.message.reply_text.await_args.args[0]
    assert "no digest found" in last.lower()
    assert "2026-w17/digest.md" in last


@pytest.mark.asyncio
async def test_tweetweekly_handles_unparseable_digest(conn, monkeypatch):
    from bot import github_sync
    from bot.handlers import tweetweekly_handler

    settings = _tw_settings()
    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=Providers(_SeqProv([]), None),
        args=["2026-w17"],
    )

    async def _fetch_junk(**kwargs):
        return ("random unparseable text\nno header\n", "sha")
    monkeypatch.setattr(github_sync, "fetch_file", _fetch_junk)

    await tweetweekly_handler(update, context)
    last = update.message.reply_text.await_args.args[0]
    assert "didn't parse" in last.lower()


@pytest.mark.asyncio
async def test_tweetweekly_happy_path_posts_and_backfills_current_week(
    conn, monkeypatch,
):
    """Current-week digest → tweet drafted, posted, and weekly row upserted."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from bot import github_sync
    from bot.handlers import tweetweekly_handler
    from bot.week import fz_week_idx, iso_week_key, local_date_for

    settings = _tw_settings()

    # Compute "current week" from the bot's perspective
    today = local_date_for(_dt.now(tz=_tz.utc), "UTC")
    current_iso = iso_week_key(today)  # e.g. "2026-W17"
    current_dir = current_iso.replace("W", "w")

    digest_md = (
        f"# {current_iso}\n"
        f"\n"
        f"**☲**  _the week hummed_\n"
        f"\n"
        f"the essay body\n"
    )

    fetch_calls = []
    async def _fetch(**kwargs):
        fetch_calls.append(kwargs["path"])
        return (digest_md, "sha-1")
    monkeypatch.setattr(github_sync, "fetch_file", _fetch)

    prov = _SeqProv(['{"tweet": "short and true"}'])
    providers = Providers(prov, None)

    async def _fake_post(text, *, settings):
        return tweet_mod.TweetResult(id="9", url="https://x.com/i/web/status/9")
    monkeypatch.setattr(tweet_mod, "post_tweet", _fake_post)

    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=providers,
    )
    await tweetweekly_handler(update, context)

    assert fetch_calls == [f"{current_dir}/digest.md"]
    # Final reply includes the tweet URL
    last = update.message.reply_text.await_args.args[0]
    assert "https://x.com/i/web/status/9" in last

    # Weekly row backfilled for current week
    from datetime import date as _d
    dob = _d(1990, 1, 1)
    fz = fz_week_idx(today, dob)
    async with conn.execute(
        "SELECT mark, whisper, essay, tweet_text, tweet_posted_at FROM weekly "
        "WHERE fz_week_idx = ?", (fz,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["mark"] == "☲"
    assert row["whisper"] == "the week hummed"
    assert "the essay body" in row["essay"]
    assert row["tweet_text"] == "short and true"
    assert row["tweet_posted_at"] is not None


@pytest.mark.asyncio
async def test_tweetweekly_past_week_does_not_backfill_weekly_row(
    conn, monkeypatch,
):
    """Posting for a non-current week should skip the backfill (no fz_week_idx
    derivable safely).
    """
    from bot import github_sync
    from bot.handlers import tweetweekly_handler

    settings = _tw_settings()

    digest_md = (
        "# 2020-W01\n"
        "\n"
        "**a**  _b_\n"
        "\n"
        "c\n"
    )

    async def _fetch(**kwargs):
        return (digest_md, "sha")
    monkeypatch.setattr(github_sync, "fetch_file", _fetch)

    prov = _SeqProv(['{"tweet": "ok"}'])
    providers = Providers(prov, None)

    async def _fake_post(text, *, settings):
        return tweet_mod.TweetResult(id="1", url="https://x.com/i/web/status/1")
    monkeypatch.setattr(tweet_mod, "post_tweet", _fake_post)

    update, context = _tw_update_context(
        settings=settings, conn=conn, providers=providers,
        args=["2020-w01"],
    )
    await tweetweekly_handler(update, context)

    async with conn.execute("SELECT COUNT(*) FROM weekly") as cur:
        (count,) = await cur.fetchone()
    assert count == 0
