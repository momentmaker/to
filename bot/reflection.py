"""Daily-reflection state machine.

Flow:
1. At DAILY_PROMPT_LOCAL_TIME the scheduler runs daily_prompt_job, which
   LLM-generates a bespoke question, writes a `daily` row, and calls
   `set_pending(local_date)` here to mark the owner's next text/voice as a
   reflection reply.
2. The handler consumes pending state on the next text or voice message,
   stores it as `kind='reflection'`, and updates `daily.reflection_capture_id`.
3. `/skip` clears the pending state.

The deadline is open-ended within the day: `set_pending` uses the next local
midnight (per the user's TIMEZONE) so a late-night reflection still lands
correctly, but tomorrow's first message doesn't accidentally link to
yesterday's prompt.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

import aiosqlite

log = logging.getLogger(__name__)

_KV_KEY = "pending_reflection"


class PendingReflection(NamedTuple):
    local_date: str          # YYYY-MM-DD in the user's TZ
    deadline: datetime       # UTC


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _next_local_midnight(tz_name: str) -> datetime:
    """Next local midnight as an aware UTC datetime."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    tomorrow = now_local.date() + timedelta(days=1)
    midnight_local = datetime.combine(tomorrow, time(0, 0), tzinfo=tz)
    return midnight_local.astimezone(timezone.utc)


async def set_pending(
    conn: aiosqlite.Connection, *, local_date: str, tz_name: str,
) -> None:
    deadline = _next_local_midnight(tz_name)
    value = json.dumps({
        "local_date": local_date,
        "deadline": deadline.isoformat().replace("+00:00", "Z"),
    })
    now_iso = _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
    await conn.execute(
        """
        INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (_KV_KEY, value, now_iso),
    )
    await conn.commit()


async def get_pending(conn: aiosqlite.Connection) -> PendingReflection | None:
    async with conn.execute("SELECT value FROM kv WHERE key = ?", (_KV_KEY,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        deadline = datetime.fromisoformat(data["deadline"].replace("Z", "+00:00"))
        return PendingReflection(local_date=str(data["local_date"]), deadline=deadline)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("corrupt pending_reflection row, clearing: %s", e)
        await clear_pending(conn)
        return None


async def clear_pending(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM kv WHERE key = ?", (_KV_KEY,))
    await conn.commit()


async def consume_if_live(conn: aiosqlite.Connection) -> str | None:
    """Atomically consume the pending-reflection row.

    Uses `DELETE ... RETURNING` so concurrent handler tasks can't both claim
    the same pending state — only one task gets the row, others see None.
    Returns local_date if live, None if the row was absent, expired, or corrupt.
    """
    async with conn.execute(
        "DELETE FROM kv WHERE key = ? RETURNING value", (_KV_KEY,),
    ) as cur:
        row = await cur.fetchone()
    await conn.commit()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        deadline = datetime.fromisoformat(data["deadline"].replace("Z", "+00:00"))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("corrupt pending_reflection row consumed, dropping: %s", e)
        return None
    if deadline <= _utcnow():
        return None
    return str(data["local_date"])
