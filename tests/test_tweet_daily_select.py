import json

import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


async def _add_capture(
    conn, *, raw, kind="text", local_date="2026-05-01", payload=None,
):
    await conn.execute(
        """
        INSERT INTO captures (kind, raw, payload, created_at, local_date,
                              iso_week_key, fz_week_idx, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'processed')
        """,
        (kind, raw, json.dumps(payload or {}),
         f"{local_date}T12:00:00Z", local_date, "2026-W18", 1900),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_empty_pool_when_nothing_flagged():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="hi", payload={})
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert rows == []


@pytest.mark.asyncio
async def test_pool_includes_only_tweetable():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="not flagged", payload={})
        await _add_capture(conn, raw="yes please", payload={"tweetable": True})
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert len(rows) == 1
        assert rows[0]["raw"] == "yes please"


@pytest.mark.asyncio
async def test_pool_excludes_why_and_highlight():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(
            conn, raw="why", kind="why", payload={"tweetable": True},
        )
        await _add_capture(
            conn, raw="hl", kind="highlight", payload={"tweetable": True},
        )
        await _add_capture(
            conn, raw="text", kind="text", payload={"tweetable": True},
        )
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert [r["raw"] for r in rows] == ["text"]


@pytest.mark.asyncio
async def test_pool_excludes_already_tweeted():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="a", payload={"tweetable": True})
        await _add_capture(conn, raw="b", payload={"tweetable": True})
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date,
                                capture_ids, text, draft_count)
            VALUES ('t1', '2026-05-01T01:00:00Z', '2026-05-01', '[1]',
                    'tweet', 1)
            """
        )
        await conn.commit()
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert [r["raw"] for r in rows] == ["b"]


@pytest.mark.asyncio
async def test_pool_window_falls_back_to_full_corpus():
    """If the 14-day window has fewer than 2 candidates, expand to full corpus."""
    settings = fake_settings(TWEET_POOL_DAYS=14)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(
            conn, raw="recent", local_date="2026-05-01",
            payload={"tweetable": True},
        )
        await _add_capture(
            conn, raw="ancient", local_date="2024-01-01",
            payload={"tweetable": True},
        )
        rows = await tweet_daily.pick_eligible_pool(
            conn, settings=settings, today_iso="2026-05-03",
        )
        # Recent-only count (1) < threshold of 2 → expand.
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_pool_window_does_not_expand_when_recent_sufficient():
    settings = fake_settings(TWEET_POOL_DAYS=14)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        for raw in ("a", "b", "c"):
            await _add_capture(
                conn, raw=raw, local_date="2026-05-01",
                payload={"tweetable": True},
            )
        await _add_capture(
            conn, raw="ancient", local_date="2024-01-01",
            payload={"tweetable": True},
        )
        rows = await tweet_daily.pick_eligible_pool(
            conn, settings=settings, today_iso="2026-05-03",
        )
        # 3 recent — threshold met, ancient excluded.
        assert {r["raw"] for r in rows} == {"a", "b", "c"}
