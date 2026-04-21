"""LLM usage ledger. Stage 2 only records; Stage 7 enforces cap."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from bot.llm.base import LlmResponse, estimate_cost_usd


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
