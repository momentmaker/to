from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Settings
from bot.llm import budget
from bot.llm.base import LlmResponse, Message
from bot.llm.router import Providers, build_providers, call_llm


class _FakeProvider:
    def __init__(self, name: str, resp: LlmResponse):
        self.name = name
        self._resp = resp
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp


async def test_record_usage_inserts_and_sums(conn):
    resp = LlmResponse(
        text="", model="claude-sonnet-4-6", provider="anthropic",
        input_tokens=1000, output_tokens=500, cache_read_tokens=0, cache_write_tokens=0,
    )
    cost = await budget.record_usage(conn, purpose="ingest", response=resp)
    assert cost > 0

    async with conn.execute("SELECT provider, model, purpose, cost_usd FROM llm_usage") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["purpose"] == "ingest"

    total = await budget.month_to_date_usd(conn)
    assert total == pytest.approx(cost)


async def test_call_llm_picks_requested_provider_and_records_usage(conn):
    anth = _FakeProvider("anthropic", LlmResponse(
        text="A", model="claude-sonnet-4-6", provider="anthropic",
        input_tokens=100, output_tokens=50,
    ))
    oai = _FakeProvider("openai", LlmResponse(
        text="O", model="gpt-4.1-mini", provider="openai",
        input_tokens=100, output_tokens=50,
    ))
    providers = Providers(anth, oai)

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k",
        LLM_PROVIDER_INGEST="openai",
    )
    resp = await call_llm(
        purpose="ingest",
        system_blocks=["s"],
        messages=[Message(role="user", content="x")],
        settings=settings, providers=providers, conn=conn,
    )
    assert resp.provider == "openai"
    assert oai.calls and not anth.calls
    # Usage row written
    async with conn.execute("SELECT provider FROM llm_usage") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["provider"] == "openai"


async def test_call_llm_falls_back_when_requested_provider_unavailable(conn):
    anth = _FakeProvider("anthropic", LlmResponse(
        text="A", model="claude-sonnet-4-6", provider="anthropic",
        input_tokens=1, output_tokens=1,
    ))
    providers = Providers(anth, None)  # only anthropic configured

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        LLM_PROVIDER_INGEST="openai",
    )
    resp = await call_llm(
        purpose="ingest",
        system_blocks=[],
        messages=[Message(role="user", content="x")],
        settings=settings, providers=providers, conn=conn,
    )
    assert resp.provider == "anthropic"


def test_build_providers_raises_when_no_keys():
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY / OPENAI_API_KEY"):
        build_providers(settings)


def test_build_providers_with_injected_clients():
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
    )
    providers = build_providers(
        settings,
        anthropic_client=MagicMock(),
        openai_client=MagicMock(),
    )
    assert providers.anthropic is not None
    assert providers.openai is not None


def test_providers_pick_raises_when_none_configured():
    providers = Providers(None, None)
    with pytest.raises(RuntimeError, match="no LLM provider"):
        providers.pick("anthropic")


def test_providers_pick_warns_once_on_fallback(caplog):
    import bot.llm.router as router_module
    router_module._warned_fallbacks.clear()  # reset between tests

    anth = _FakeProvider("anthropic", LlmResponse(
        text="", model="x", provider="anthropic", input_tokens=1, output_tokens=1,
    ))
    providers = Providers(anth, None)

    import logging
    with caplog.at_level(logging.WARNING, logger="bot.llm.router"):
        providers.pick("openai", purpose="tweet")
        providers.pick("openai", purpose="tweet")  # second call should not re-warn
        providers.pick("openai", purpose="daily")  # different purpose, should warn

    warnings = [r for r in caplog.records if "falling back" in r.message]
    assert len(warnings) == 2  # (tweet, openai) + (daily, openai), not 3
