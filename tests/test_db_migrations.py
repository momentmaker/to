import pytest
import aiosqlite

from bot.db import init_schema


@pytest.mark.asyncio
async def test_tweets_table_exists_after_init():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='tweets'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None


@pytest.mark.asyncio
async def test_tweets_indexes_exist():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name IN "
            "('tweets_theme_idx', 'tweets_local_date_idx')"
        ) as cur:
            names = {r[0] for r in await cur.fetchall()}
        assert names == {"tweets_theme_idx", "tweets_local_date_idx"}


@pytest.mark.asyncio
async def test_tweets_table_accepts_full_row():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date,
                                capture_ids, theme, stitch, text,
                                draft_count, edited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("1789", "2026-05-03T14:14:00Z", "2026-05-03",
             '["a","b"]', "privacy", "you caught it.", "tweet text",
             1, 0),
        )
        await conn.commit()
        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            row = await cur.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_pragma_user_version_advances_with_each_schema_change():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        # Asserts the latest migration ran. Bump expected value when
        # appending a migration; never mutate an already-shipped one.
        assert int(row[0]) == 4


@pytest.mark.asyncio
async def test_tweets_table_has_in_reply_to_tweet_id_column():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        async with conn.execute("PRAGMA table_info(tweets)") as cur:
            columns = {r[1] for r in await cur.fetchall()}
        assert "in_reply_to_tweet_id" in columns
