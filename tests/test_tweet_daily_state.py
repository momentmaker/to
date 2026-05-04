import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema


@pytest.mark.asyncio
async def test_set_and_get_pending():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        p = await tweet_daily.get_pending(conn)
        assert p is not None
        assert p.draft_text == "hi"
        assert p.capture_ids == [1, 2]
        assert p.draft_count == 1


@pytest.mark.asyncio
async def test_get_pending_returns_none_when_absent():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        assert await tweet_daily.get_pending(conn) is None


@pytest.mark.asyncio
async def test_update_for_next_increments_draft_count():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="d1", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        new_count = await tweet_daily.update_for_next(
            conn,
            draft_text="d2", capture_ids=[3, 4],
            theme="u", stitch="s2", char_count=20,
        )
        assert new_count == 2
        p = await tweet_daily.get_pending(conn)
        assert p.draft_text == "d2"
        assert p.draft_count == 2
        assert p.local_date == "2026-05-03"  # preserved


@pytest.mark.asyncio
async def test_update_for_next_returns_none_when_no_pending():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        result = await tweet_daily.update_for_next(
            conn,
            draft_text="d", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
        )
        assert result is None


@pytest.mark.asyncio
async def test_consume_for_post_returns_and_clears():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        consumed = await tweet_daily.consume_for_post(conn)
        assert consumed is not None
        assert consumed.draft_text == "hi"
        assert await tweet_daily.get_pending(conn) is None


@pytest.mark.asyncio
async def test_consume_for_post_returns_none_when_absent():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        assert await tweet_daily.consume_for_post(conn) is None


@pytest.mark.asyncio
async def test_clear_pending_when_absent_is_noop():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.clear_pending(conn)


@pytest.mark.asyncio
async def test_expire_drops_prior_day():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="old", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-02",
        )
        dropped = await tweet_daily.expire_if_stale(
            conn, today_local="2026-05-03",
        )
        assert dropped is True
        assert await tweet_daily.get_pending(conn) is None


@pytest.mark.asyncio
async def test_expire_keeps_today():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="today", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        dropped = await tweet_daily.expire_if_stale(
            conn, today_local="2026-05-03",
        )
        assert dropped is False
        assert await tweet_daily.get_pending(conn) is not None
