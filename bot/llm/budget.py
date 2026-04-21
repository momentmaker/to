"""LLM usage ledger + soft cap enforcement.

The ledger (`llm_usage` table) is the source of truth for spend. Every
adapter call records a row via `record_usage`. Two derived behaviors key off
the running total:

- `should_degrade(purpose)` — True when month-to-date spend is at or above
  the user's soft cap. Non-digest purposes switch to the `*_CHEAP` model.
  Digest is preserved — the weekly anthology is the headline feature.
- `check_and_warn_cap` — sends a one-time-per-month dhyama alert when spend
  crosses 90% of the cap.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

from bot.config import Settings
from bot.llm.base import LlmResponse, estimate_cost_usd

log = logging.getLogger(__name__)


_WARN_THRESHOLD = 0.9  # fraction of cap that triggers the dhyama warn
_WARNED_KEY_PREFIX = "budget_warned_"


async def record_usage(
    conn: aiosqlite.Connection,
    *,
    purpose: str,
    response: LlmResponse,
) -> float:
    """Append an llm_usage row and return the estimated cost in USD."""
    cost = estimate_cost_usd(
        response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cache_read_tokens=response.cache_read_tokens,
        cache_write_tokens=response.cache_write_tokens,
    )
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await conn.execute(
        """
        INSERT INTO llm_usage (
            at, provider, model, purpose,
            input_tokens, cache_read_tokens, cache_write_tokens, output_tokens,
            cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso, response.provider, response.model, purpose,
            response.input_tokens, response.cache_read_tokens,
            response.cache_write_tokens, response.output_tokens,
            cost,
        ),
    )
    await conn.commit()
    return cost


async def month_to_date_usd(
    conn: aiosqlite.Connection, year_month: str | None = None
) -> float:
    """Sum cost_usd for the given YYYY-MM (default: current UTC month)."""
    if year_month is None:
        year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE substr(at,1,7) = ?",
        (year_month,),
    ) as cur:
        row = await cur.fetchone()
    return float(row[0]) if row else 0.0


async def month_to_date_by_provider(
    conn: aiosqlite.Connection, year_month: str | None = None
) -> dict[str, float]:
    """Per-provider spend for `/status`. Returns {provider: usd}."""
    if year_month is None:
        year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with conn.execute(
        """
        SELECT provider, COALESCE(SUM(cost_usd), 0)
        FROM llm_usage
        WHERE substr(at,1,7) = ?
        GROUP BY provider
        """,
        (year_month,),
    ) as cur:
        rows = await cur.fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


async def cache_hit_ratio(
    conn: aiosqlite.Connection, year_month: str | None = None,
) -> float:
    """Fraction of input tokens served from cache this month. 0.0 when no usage."""
    if year_month is None:
        year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with conn.execute(
        """
        SELECT
            COALESCE(SUM(cache_read_tokens), 0),
            COALESCE(SUM(input_tokens + cache_read_tokens + cache_write_tokens), 0)
        FROM llm_usage
        WHERE substr(at,1,7) = ?
        """,
        (year_month,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0.0
    cached = int(row[0])
    total = int(row[1])
    return (cached / total) if total > 0 else 0.0


async def should_degrade(
    conn: aiosqlite.Connection, *, settings: Settings, purpose: str,
) -> bool:
    """True when non-digest calls should fall back to the `*_CHEAP` model.

    Digest is never degraded — the weekly anthology is the headline feature
    and only fires 4x/month, so its cost impact is bounded.
    """
    if purpose == "digest":
        return False
    cap = float(settings.LLM_MONTHLY_USD_CAP)
    if cap <= 0:
        return False
    mtd = await month_to_date_usd(conn)
    return mtd >= cap


async def check_and_warn_cap(
    conn: aiosqlite.Connection, *, settings: Settings,
) -> None:
    """Emit a one-time-per-month dhyama alert when spend crosses 90% of cap.

    Re-arms at the start of each calendar month (UTC), so a long-running bot
    gets a fresh warning each month rather than only ever warning once.

    Uses `INSERT ... ON CONFLICT DO NOTHING RETURNING` to claim the
    warning atomically — two concurrent LLM calls completing above the
    threshold won't both send the dhyama alert.
    """
    cap = float(settings.LLM_MONTHLY_USD_CAP)
    if cap <= 0:
        return
    mtd = await month_to_date_usd(conn)
    if mtd < cap * _WARN_THRESHOLD:
        return

    year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    kv_key = _WARNED_KEY_PREFIX + year_month
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # Atomic claim: only the first caller wins the INSERT and thus the alert.
    async with conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO NOTHING RETURNING key",
        (kv_key, json.dumps(mtd), now_iso),
    ) as cur:
        claimed = await cur.fetchone()
    await conn.commit()
    if claimed is None:
        return  # another task already warned this month

    try:
        from bot.notify import send_alert
        await send_alert(
            f"LLM spend at ${mtd:.2f} / ${cap:.2f} cap this month. "
            f"Non-digest calls are now degraded to the *_CHEAP model.",
            severity="warning",
        )
    except Exception:
        log.exception("budget: dhyama warn failed")
