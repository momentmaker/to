from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import process
from bot.config import Settings
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


pytestmark = pytest.mark.asyncio


class _StaticProvider:
    def __init__(self, name: str, text: str):
        self.name = name
        self._text = text
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LlmResponse(
            text=self._text, model=kwargs["model"], provider=self.name,
            input_tokens=50, output_tokens=30,
        )


def _settings(provider_ingest: str = "anthropic"):
    return Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k",
        LLM_PROVIDER_INGEST=provider_ingest,
    )


async def test_process_capture_parses_json(conn):
    llm_json = (
        '{"title": "A Small Ignition", '
        '"tags": ["stoic", "action"], '
        '"quotes": ["the impediment to action advances action"], '
        '"summary": "A fragment on moving despite obstacles."}'
    )
    provider = _StaticProvider("anthropic", llm_json)
    providers = Providers(provider, None)

    result = await process.process_capture(
        content="the impediment to action advances action",
        settings=_settings("anthropic"),
        providers=providers,
        conn=conn,
    )
    assert result["title"] == "A Small Ignition"
    assert result["tags"] == ["stoic", "action"]
    assert result["quotes"] == ["the impediment to action advances action"]
    assert result["summary"].startswith("A fragment")


async def test_process_capture_tolerates_code_fenced_json(conn):
    llm_out = "```json\n" + '{"title": "T", "tags": ["a"], "quotes": [], "summary": "s"}' + "\n```"
    provider = _StaticProvider("anthropic", llm_out)
    providers = Providers(provider, None)

    result = await process.process_capture(
        content="x", settings=_settings(), providers=providers, conn=conn,
    )
    assert result == {"title": "T", "tags": ["a"], "quotes": [], "summary": "s"}


async def test_process_capture_normalizes_bad_shapes(conn):
    # Missing keys, duplicate tags, non-string values
    llm_out = '{"tags": ["Stoic", "stoic", "action", 42], "quotes": null}'
    provider = _StaticProvider("anthropic", llm_out)
    providers = Providers(provider, None)

    result = await process.process_capture(
        content="x", settings=_settings(), providers=providers, conn=conn,
    )
    assert result["title"] == ""
    assert result["summary"] == ""
    assert result["quotes"] == []
    # lowercased, deduped, "42" kept as string
    assert result["tags"] == ["stoic", "action", "42"]


async def test_process_capture_falls_back_when_llm_returns_non_json(conn):
    provider = _StaticProvider("anthropic", "sorry, I cannot comply")
    providers = Providers(provider, None)

    result = await process.process_capture(
        content="x", settings=_settings(), providers=providers, conn=conn,
    )
    assert result == {"title": "", "tags": [], "quotes": [], "summary": ""}


async def test_process_capture_works_with_openai_provider(conn):
    llm_json = '{"title": "T", "tags": ["a"], "quotes": [], "summary": "s"}'
    provider = _StaticProvider("openai", llm_json)
    providers = Providers(None, provider)

    result = await process.process_capture(
        content="x",
        settings=_settings("openai"),
        providers=providers,
        conn=conn,
    )
    assert result["title"] == "T"
    # ingest purpose routed to openai
    assert provider.calls[0]["model"]  # some openai model


async def test_mark_processed_and_failed(conn):
    from bot import db as db_mod
    from datetime import date

    cid = await db_mod.insert_capture(
        conn, kind="text", raw="x", dob=date(1990, 1, 1), tz_name="UTC",
    )
    assert cid is not None

    await process.mark_processed(conn, capture_id=cid, processed={"title": "t"})
    async with conn.execute("SELECT status, processed FROM captures WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "processed"
    assert '"title"' in row["processed"]

    # Then fail it
    await process.mark_failed(conn, capture_id=cid, error="boom")
    async with conn.execute("SELECT status, error FROM captures WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "boom"
