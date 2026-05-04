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


import json


@pytest.mark.asyncio
async def test_next_handler_increments_and_dms_new_draft(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1, TWEET_NEXT_CAP=5)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        for raw in ["new alpha line worth keeping",
                    "new beta line worth keeping"]:
            await conn.execute(
                """
                INSERT INTO captures (kind, raw, payload, created_at,
                                      local_date, iso_week_key, fz_week_idx,
                                      status)
                VALUES ('text', ?, ?, ?, ?, ?, ?, 'done')
                """,
                (raw, json.dumps({"tweetable": True}),
                 "2026-05-01T12:00:00Z", "2026-05-01", "2026-W18", 1900),
            )
        await conn.commit()
        # Pending references nonexistent captures (placeholders), so the
        # /next handler's "fresh" filter keeps the real proposal.
        await tweet_daily.set_pending(
            conn,
            draft_text="d1", capture_ids=[99, 100],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def fake_call(*, purpose, **kwargs):
            class R:
                pass
            r = R()
            r.text = (
                json.dumps([{"theme": "u", "capture_ids": [1, 2],
                             "rationale": ""}])
                if purpose == "ingest"
                else json.dumps({"stitch": "you noticed twice."})
            )
            return r

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        update = _update("/next")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.next_handler(update, ctx)

        p = await tweet_daily.get_pending(conn)
        assert p.draft_count == 2
        update.message.reply_text.assert_awaited()
        assert "draft 2/5" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_next_handler_blocks_at_cap():
    settings = fake_settings(TELEGRAM_OWNER_ID=1, TWEET_NEXT_CAP=5)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="d", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        # Bump draft_count to cap
        await conn.execute(
            "UPDATE kv SET value = json_set(value, '$.draft_count', 5) "
            "WHERE key = ?", (tweet_daily._KV_KEY,),
        )
        await conn.commit()

        update = _update("/next")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.next_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        msg = update.message.reply_text.call_args.args[0].lower()
        assert "exhausted" in msg


@pytest.mark.asyncio
async def test_next_handler_no_pending_replies_idle():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _update("/next")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.next_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "no draft" in update.message.reply_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_edit_handler_posts_user_text_and_marks_edited(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="orig", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def fake_post_tweet(text, *, settings):
            from bot.tweet import TweetResult
            return TweetResult(id="2", url="https://x.com/i/web/status/2")

        monkeypatch.setattr(
            "bot.handlers.tweet_mod.post_tweet", fake_post_tweet,
        )

        async def no_push(**_):
            pass
        monkeypatch.setattr(
            "bot.handlers.tweet_daily.push_ledger_to_repo", no_push,
        )

        update = _update("/edit my own version")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)

        async with conn.execute("SELECT text, edited FROM tweets") as cur:
            row = await cur.fetchone()
        assert row["text"] == "my own version"
        assert row["edited"] == 1


@pytest.mark.asyncio
async def test_edit_handler_rejects_over_280():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="orig", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        long_text = "/edit " + "x" * 281
        update = _update(long_text)
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)
        # Pending preserved, no tweet row.
        assert await tweet_daily.get_pending(conn) is not None
        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_edit_handler_no_pending_replies_idle():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _update("/edit hello world")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "no draft" in update.message.reply_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_edit_handler_no_text_replies_usage():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _update("/edit")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "usage" in update.message.reply_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_skip_clears_pending_tweet_draft():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="d", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        update = _update("/skip")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.skip_handler(update, ctx)
        assert await tweet_daily.get_pending(conn) is None
