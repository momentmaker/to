from datetime import date, datetime, timezone

import pytest

from bot import db
from bot.week import fz_week_idx, iso_week_key


pytestmark = pytest.mark.asyncio


async def _tables(conn):
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' OR type='virtual' ORDER BY name"
    ) as cur:
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def test_schema_init_creates_all_tables_and_fts(conn):
    tables = await _tables(conn)
    for t in ("captures", "media", "daily", "weekly", "kv", "llm_usage", "captures_fts"):
        assert t in tables, f"missing table {t}"


async def test_fts_triggers_keep_index_in_sync(conn, dob, tz_name):
    cid = await db.insert_capture(
        conn,
        kind="text",
        raw="a tiny ignition in the corpus",
        source="telegram",
        dob=dob,
        tz_name=tz_name,
    )
    assert cid > 0

    async with conn.execute(
        "SELECT rowid FROM captures_fts WHERE captures_fts MATCH ?", ("ignition",)
    ) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [cid]

    await conn.execute("UPDATE captures SET raw = ? WHERE id = ?", ("a silent corpus", cid))
    await conn.commit()

    async with conn.execute(
        "SELECT rowid FROM captures_fts WHERE captures_fts MATCH ?", ("ignition",)
    ) as cur:
        assert (await cur.fetchall()) == []
    async with conn.execute(
        "SELECT rowid FROM captures_fts WHERE captures_fts MATCH ?", ("silent",)
    ) as cur:
        assert [r[0] for r in await cur.fetchall()] == [cid]

    await conn.execute("DELETE FROM captures WHERE id = ?", (cid,))
    await conn.commit()
    async with conn.execute(
        "SELECT rowid FROM captures_fts WHERE captures_fts MATCH ?", ("silent",)
    ) as cur:
        assert (await cur.fetchall()) == []


async def test_insert_capture_sets_week_index_from_dob(conn, tz_name):
    dob = date(1990, 1, 1)
    created = datetime(1990, 1, 15, 12, 0, tzinfo=timezone.utc)
    cid = await db.insert_capture(
        conn,
        kind="text",
        raw="hello",
        dob=dob,
        tz_name=tz_name,
        created_at=created,
    )
    async with conn.execute(
        "SELECT fz_week_idx, iso_week_key, local_date FROM captures WHERE id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()

    assert row["fz_week_idx"] == 2
    assert row["iso_week_key"] == iso_week_key(created.date())
    assert row["local_date"] == "1990-01-15"


async def test_fz_week_math_dob_is_week_zero():
    dob = date(1990, 1, 1)
    assert fz_week_idx(dob, dob) == 0
    assert fz_week_idx(date(1990, 1, 7), dob) == 0
    assert fz_week_idx(date(1990, 1, 8), dob) == 1


async def test_count_captures_this_week_includes_today_only_current_week(conn, dob, tz_name):
    now = datetime.now(timezone.utc)
    past = datetime(dob.year + 1, 6, 1, 12, 0, tzinfo=timezone.utc)

    await db.insert_capture(conn, kind="text", raw="today", dob=dob, tz_name=tz_name, created_at=now)
    await db.insert_capture(conn, kind="text", raw="old",  dob=dob, tz_name=tz_name, created_at=past)

    c = await db.count_captures_this_week(conn, dob=dob, tz_name=tz_name)
    assert c == 1


async def test_insert_capture_deduplicates_by_telegram_msg_id(conn, dob, tz_name):
    first = await db.insert_capture(
        conn, kind="text", source="telegram", raw="first",
        telegram_msg_id=777, dob=dob, tz_name=tz_name,
    )
    assert first is not None

    second = await db.insert_capture(
        conn, kind="text", source="telegram", raw="first-retry",
        telegram_msg_id=777, dob=dob, tz_name=tz_name,
    )
    assert second is None

    assert await db.count_captures(conn) == 1


async def test_insert_capture_allows_nulls_and_distinct_sources_with_same_id(conn, dob, tz_name):
    a = await db.insert_capture(
        conn, kind="text", source="telegram", raw="a",
        telegram_msg_id=1, dob=dob, tz_name=tz_name,
    )
    b = await db.insert_capture(
        conn, kind="text", source="email", raw="b",
        telegram_msg_id=1, dob=dob, tz_name=tz_name,
    )
    # captures without a telegram_msg_id are never duplicates
    c = await db.insert_capture(conn, kind="text", raw="c1", dob=dob, tz_name=tz_name)
    d = await db.insert_capture(conn, kind="text", raw="c2", dob=dob, tz_name=tz_name)
    assert a is not None and b is not None and c is not None and d is not None
    assert await db.count_captures(conn) == 4


async def test_user_version_is_at_current_migration_after_init(conn):
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == len(db.MIGRATIONS)


async def test_init_schema_is_idempotent(conn, dob, tz_name):
    # Re-running migrations on an already-initialized DB must not raise or
    # re-run migrations past user_version.
    await db.init_schema(conn)
    cid = await db.insert_capture(conn, kind="text", raw="post-reinit", dob=dob, tz_name=tz_name)
    assert cid is not None
