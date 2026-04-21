from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bot import reflection


@pytest.mark.asyncio
async def test_set_and_get_pending_roundtrips(conn):
    await reflection.set_pending(conn, local_date="2026-04-21", tz_name="UTC")
    pending = await reflection.get_pending(conn)
    assert pending is not None
    assert pending.local_date == "2026-04-21"
    assert pending.deadline > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_deadline_is_next_local_midnight(conn):
    await reflection.set_pending(conn, local_date="2026-04-21", tz_name="UTC")
    pending = await reflection.get_pending(conn)
    assert pending is not None
    # Deadline must be strictly in the future but before 24h from now.
    now = datetime.now(timezone.utc)
    assert pending.deadline > now
    assert pending.deadline <= now + timedelta(hours=24)


@pytest.mark.asyncio
async def test_consume_if_live_returns_and_clears(conn):
    await reflection.set_pending(conn, local_date="2026-04-21", tz_name="UTC")
    got = await reflection.consume_if_live(conn)
    assert got == "2026-04-21"
    # Cleared
    assert await reflection.consume_if_live(conn) is None


@pytest.mark.asyncio
async def test_consume_if_live_expired_returns_none_and_clears(conn):
    past = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('pending_reflection', ?, ?)",
        (json.dumps({"local_date": "2000-01-01", "deadline": past}), "2000-01-01T00:00:00Z"),
    )
    await conn.commit()
    assert await reflection.consume_if_live(conn) is None
    assert await reflection.get_pending(conn) is None


@pytest.mark.asyncio
async def test_corrupt_row_self_heals(conn):
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('pending_reflection', ?, ?)",
        ("not-json", "2000-01-01T00:00:00Z"),
    )
    await conn.commit()
    assert await reflection.get_pending(conn) is None
    async with conn.execute("SELECT COUNT(*) FROM kv WHERE key = 'pending_reflection'") as cur:
        row = await cur.fetchone()
    assert row[0] == 0
