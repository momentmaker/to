from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from bot.config import Settings
from bot.llm import budget
from bot.llm.base import LlmResponse
from bot.llm.router import model_for_purpose


def _settings(cap: float = 30.0, **kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        LLM_MONTHLY_USD_CAP=cap,
    )
    base.update(kw)
    return Settings(**base)


async def _insert_usage(conn, *, cost: float, purpose: str = "ingest",
                        provider: str = "anthropic", model: str = "claude-sonnet-4-6",
                        year_month: str | None = None):
    if year_month is None:
        year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    at = f"{year_month}-15T12:00:00Z"
    await conn.execute(
        """INSERT INTO llm_usage (at, provider, model, purpose,
                                  input_tokens, cache_read_tokens, cache_write_tokens,
                                  output_tokens, cost_usd)
           VALUES (?, ?, ?, ?, 100, 0, 0, 100, ?)""",
        (at, provider, model, purpose, cost),
    )
    await conn.commit()


# ---- should_degrade ------------------------------------------------------

@pytest.mark.asyncio
async def test_should_degrade_false_under_cap(conn):
    await _insert_usage(conn, cost=10.0)
    settings = _settings(cap=30.0)
    assert await budget.should_degrade(conn, settings=settings, purpose="ingest") is False


@pytest.mark.asyncio
async def test_should_degrade_true_above_cap(conn):
    await _insert_usage(conn, cost=35.0)
    settings = _settings(cap=30.0)
    assert await budget.should_degrade(conn, settings=settings, purpose="ingest") is True


@pytest.mark.asyncio
async def test_should_degrade_preserves_digest_even_above_cap(conn):
    await _insert_usage(conn, cost=100.0)
    settings = _settings(cap=30.0)
    assert await budget.should_degrade(conn, settings=settings, purpose="digest") is False


@pytest.mark.asyncio
async def test_should_degrade_disabled_when_cap_zero(conn):
    await _insert_usage(conn, cost=100.0)
    settings = _settings(cap=0.0)
    assert await budget.should_degrade(conn, settings=settings, purpose="ingest") is False


# ---- model_for_purpose ---------------------------------------------------

@pytest.mark.asyncio
async def test_model_for_purpose_normal_routing(conn):
    settings = _settings()
    assert await model_for_purpose(settings, "ingest", "anthropic", conn) == settings.CLAUDE_MODEL_INGEST
    assert await model_for_purpose(settings, "digest", "anthropic", conn) == settings.CLAUDE_MODEL_DIGEST
    assert await model_for_purpose(settings, "ingest", "openai",    conn) == settings.OPENAI_MODEL_INGEST
    assert await model_for_purpose(settings, "digest", "openai",    conn) == settings.OPENAI_MODEL_DIGEST


@pytest.mark.asyncio
async def test_model_for_purpose_degrades_non_digest_above_cap(conn):
    await _insert_usage(conn, cost=50.0)
    settings = _settings(cap=30.0)

    # Digest preserved even above cap
    assert await model_for_purpose(settings, "digest", "anthropic", conn) == settings.CLAUDE_MODEL_DIGEST

    # Everything else switches to *_CHEAP
    for purpose in ("ingest", "daily", "why", "oracle", "tweet", "vision"):
        got = await model_for_purpose(settings, purpose, "anthropic", conn)
        assert got == settings.CLAUDE_MODEL_CHEAP, f"{purpose} should degrade"

    for purpose in ("ingest", "oracle", "tweet"):
        got = await model_for_purpose(settings, purpose, "openai", conn)
        assert got == settings.OPENAI_MODEL_CHEAP, f"{purpose} should degrade"


# ---- check_and_warn_cap --------------------------------------------------

@pytest.mark.asyncio
async def test_check_and_warn_no_alert_under_threshold(conn):
    await _insert_usage(conn, cost=10.0)
    settings = _settings(cap=30.0)
    with patch("bot.notify.send_alert", AsyncMock()) as alert:
        await budget.check_and_warn_cap(conn, settings=settings)
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_warn_fires_at_ninety_percent(conn):
    await _insert_usage(conn, cost=28.0)  # 93% of 30
    settings = _settings(cap=30.0)
    with patch("bot.notify.send_alert", AsyncMock()) as alert:
        await budget.check_and_warn_cap(conn, settings=settings)
    alert.assert_awaited_once()
    kwargs = alert.await_args
    assert "LLM spend" in kwargs.args[0]


@pytest.mark.asyncio
async def test_check_and_warn_only_fires_once_per_month(conn):
    await _insert_usage(conn, cost=28.0)
    settings = _settings(cap=30.0)
    with patch("bot.notify.send_alert", AsyncMock()) as alert:
        await budget.check_and_warn_cap(conn, settings=settings)
        await budget.check_and_warn_cap(conn, settings=settings)
        await budget.check_and_warn_cap(conn, settings=settings)
    assert alert.await_count == 1


@pytest.mark.asyncio
async def test_check_and_warn_is_atomic_under_concurrent_calls(conn):
    """Regression: previously SELECT-then-INSERT had a TOCTOU race —
    concurrent callers could both see "no warning row", both send alert,
    then one INSERT wins. With INSERT ... ON CONFLICT DO NOTHING RETURNING,
    only one caller claims the row and fires the alert.
    """
    import asyncio
    await _insert_usage(conn, cost=28.0)
    settings = _settings(cap=30.0)
    with patch("bot.notify.send_alert", AsyncMock()) as alert:
        # Kick off three concurrent warn checks against the same connection
        await asyncio.gather(
            budget.check_and_warn_cap(conn, settings=settings),
            budget.check_and_warn_cap(conn, settings=settings),
            budget.check_and_warn_cap(conn, settings=settings),
        )
    assert alert.await_count == 1


@pytest.mark.asyncio
async def test_check_and_warn_disabled_when_cap_zero(conn):
    await _insert_usage(conn, cost=10_000.0)
    settings = _settings(cap=0.0)
    with patch("bot.notify.send_alert", AsyncMock()) as alert:
        await budget.check_and_warn_cap(conn, settings=settings)
    alert.assert_not_awaited()


# ---- aggregates ----------------------------------------------------------

@pytest.mark.asyncio
async def test_month_to_date_by_provider(conn):
    await _insert_usage(conn, cost=1.0, provider="anthropic")
    await _insert_usage(conn, cost=2.0, provider="anthropic")
    await _insert_usage(conn, cost=3.0, provider="openai")
    by_provider = await budget.month_to_date_by_provider(conn)
    assert by_provider == {"anthropic": 3.0, "openai": 3.0}


@pytest.mark.asyncio
async def test_cache_hit_ratio_handles_zero_usage(conn):
    assert await budget.cache_hit_ratio(conn) == 0.0


@pytest.mark.asyncio
async def test_cache_hit_ratio_computes_fraction(conn):
    at = datetime.now(timezone.utc).strftime("%Y-%m") + "-01T12:00:00Z"
    await conn.execute(
        """INSERT INTO llm_usage (at, provider, model, purpose,
                                  input_tokens, cache_read_tokens, cache_write_tokens,
                                  output_tokens, cost_usd)
           VALUES (?, 'anthropic', 'claude', 'ingest', 100, 300, 50, 20, 0.001)""",
        (at,),
    )
    await conn.commit()
    ratio = await budget.cache_hit_ratio(conn)
    # 300 cached / (100 fresh + 300 read + 50 write) = 300/450 = 0.666...
    assert ratio == pytest.approx(300 / 450)
