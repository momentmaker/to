"""Capture-time "why?" follow-up.

Flow:
1. User saves a link → URL capture stored → bot replies "kept."
2. Bot fires an orchurator-voiced one-sentence "why?" question referencing
   the scraped title, and writes a `pending_why` row to `kv` with
   `{parent_id, deadline}`.
3. Next plain-text message from the owner within WHY_WINDOW_MINUTES is
   re-routed: stored as `kind='why'` with `parent_id` set to the link's
   capture id, and the pending state is cleared.
4. After the window, the pending state is cleared and the reply files as a
   normal text capture.
5. `/skip` clears the pending state without storing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import aiosqlite

from bot.config import Settings
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_WHY

log = logging.getLogger(__name__)

_KV_KEY = "pending_why"


class PendingWhy(NamedTuple):
    parent_id: int
    deadline: datetime  # UTC


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def set_pending(
    conn: aiosqlite.Connection, *, parent_id: int, window_minutes: int
) -> None:
    deadline = _utcnow() + timedelta(minutes=max(window_minutes, 1))
    value = json.dumps({
        "parent_id": parent_id,
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


async def get_pending(conn: aiosqlite.Connection) -> PendingWhy | None:
    async with conn.execute("SELECT value FROM kv WHERE key = ?", (_KV_KEY,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        deadline = datetime.fromisoformat(data["deadline"].replace("Z", "+00:00"))
        return PendingWhy(parent_id=int(data["parent_id"]), deadline=deadline)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("corrupt pending_why row, clearing: %s", e)
        await clear_pending(conn)
        return None


async def clear_pending(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM kv WHERE key = ?", (_KV_KEY,))
    await conn.commit()


async def consume_if_live(conn: aiosqlite.Connection) -> int | None:
    """If a pending-why exists and hasn't expired, clear it and return the
    parent_id. If expired, clear it and return None. If none, return None.
    """
    pending = await get_pending(conn)
    if pending is None:
        return None
    if pending.deadline <= _utcnow():
        await clear_pending(conn)
        return None
    await clear_pending(conn)
    return pending.parent_id


async def ask_why_question(
    *,
    url: str,
    title: str | None,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> str:
    """LLM generates the orchurator-voiced question. Never raises — falls back
    to a static question if the LLM call fails."""
    user_content = f"Title: {title or '(none)'}\nURL: {url}"
    try:
        response = await call_llm(
            purpose="why",
            system_blocks=[VOICE_ORCHURATOR, SYSTEM_WHY],
            messages=[Message(role="user", content=user_content)],
            max_tokens=80,
            settings=settings, providers=providers, conn=conn,
        )
        q = (response.text or "").strip()
        return q or "why this one?"
    except Exception:
        log.exception("ask_why LLM call failed")
        return "why this one?"
