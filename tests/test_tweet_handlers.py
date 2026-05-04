import pytest
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from bot import handlers, tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


def _update(text, owner_id=1):
    u = MagicMock()
    u.effective_user.id = owner_id
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def _ctx(*, conn, settings):
    c = MagicMock()
    c.bot_data = {
        "conn": conn, "db": conn,
        "settings": settings,
        "providers": MagicMock(),
    }
    return c


@pytest.mark.asyncio
async def test_post_handler_posts_and_writes_ledger(monkeypatch):
    settings = fake_settings(
        TELEGRAM_OWNER_ID=1,
        X_CONSUMER_KEY="a", X_CONSUMER_SECRET="b",
        X_ACCESS_TOKEN="c", X_ACCESS_TOKEN_SECRET="d",
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def fake_post_tweet(text, *, settings):
            from bot.tweet import TweetResult
            return TweetResult(
                id="1789", url="https://x.com/i/web/status/1789",
            )
        monkeypatch.setattr(
            "bot.handlers.tweet_mod.post_tweet", fake_post_tweet,
        )

        async def fake_push(**_):
            pass
        monkeypatch.setattr(
            "bot.handlers.tweet_daily.push_ledger_to_repo", fake_push,
        )

        update = _update("/post")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.post_handler(update, ctx)

        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        assert await tweet_daily.get_pending(conn) is None
        update.message.reply_text.assert_awaited()
        assert "1789" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_post_handler_no_pending_replies_idle():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _update("/post")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.post_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "no draft" in update.message.reply_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_post_handler_post_failure_clears_pending(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def fake_post_tweet(text, *, settings):
            return None  # post fails

        monkeypatch.setattr(
            "bot.handlers.tweet_mod.post_tweet", fake_post_tweet,
        )

        update = _update("/post")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.post_handler(update, ctx)

        # Pending consumed (cleared) even though post failed.
        assert await tweet_daily.get_pending(conn) is None
        # No tweet row written.
        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            assert (await cur.fetchone())[0] == 0
        update.message.reply_text.assert_awaited()
        msg = update.message.reply_text.call_args.args[0]
        assert "post failed" in msg.lower()
