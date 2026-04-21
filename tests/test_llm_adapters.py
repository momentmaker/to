from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.llm.anthropic import AnthropicProvider
from bot.llm.base import Message, estimate_cost_usd
from bot.llm.openai import OpenAIProvider


# ---- Anthropic ------------------------------------------------------------

def _anth_response(text: str, *, input_tokens=100, output_tokens=50,
                   cache_read=0, cache_write=0, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        stop_reason=stop_reason,
    )


async def test_anthropic_adapter_marks_system_blocks_ephemeral():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_anth_response("hello"))

    provider = AnthropicProvider(client)
    await provider.chat(
        model="claude-sonnet-4-6",
        purpose="ingest",
        system_blocks=["block-A", "block-B"],
        messages=[Message(role="user", content="hi")],
    )

    kwargs = client.messages.create.await_args.kwargs
    system = kwargs["system"]
    assert len(system) == 2
    for block in system:
        assert block["type"] == "text"
        assert block["cache_control"] == {"type": "ephemeral"}
    assert [b["text"] for b in system] == ["block-A", "block-B"]


async def test_anthropic_adapter_filters_empty_system_blocks():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_anth_response("hi"))

    provider = AnthropicProvider(client)
    await provider.chat(
        model="m", purpose="ingest",
        system_blocks=["", "keep", ""],
        messages=[Message(role="user", content="x")],
    )
    system = client.messages.create.await_args.kwargs["system"]
    assert [b["text"] for b in system] == ["keep"]


async def test_anthropic_adapter_records_cache_tokens():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_anth_response("ok", input_tokens=200, cache_read=150, cache_write=40)
    )
    provider = AnthropicProvider(client)
    resp = await provider.chat(
        model="claude-sonnet-4-6", purpose="ingest",
        system_blocks=["s"],
        messages=[Message(role="user", content="x")],
    )
    assert resp.cache_read_tokens == 150
    assert resp.cache_write_tokens == 40
    assert resp.input_tokens == 200
    assert resp.text == "ok"


# ---- OpenAI ---------------------------------------------------------------

def _oai_response(text: str, *, prompt_tokens=120, completion_tokens=40, cached=0,
                  finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


async def test_openai_adapter_sends_single_system_message():
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_oai_response("hi"))

    provider = OpenAIProvider(client)
    await provider.chat(
        model="gpt-4.1-mini",
        purpose="ingest",
        system_blocks=["block-A", "block-B"],
        messages=[Message(role="user", content="x")],
    )
    kwargs = client.chat.completions.create.await_args.kwargs
    msgs = kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "block-A\n\nblock-B"
    assert msgs[1] == {"role": "user", "content": "x"}


async def test_openai_records_cached_tokens_and_normalizes_input():
    """OpenAI's prompt_tokens includes cached_tokens. The adapter must subtract
    so input_tokens/cache_read_tokens stay disjoint (matches Anthropic
    semantics and prevents double-counting in the cost formula).
    """
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_oai_response("ok", prompt_tokens=500, cached=320)
    )
    provider = OpenAIProvider(client)
    resp = await provider.chat(
        model="gpt-4.1-mini", purpose="ingest",
        system_blocks=["s"],
        messages=[Message(role="user", content="x")],
    )
    # input_tokens is the FRESH portion (prompt - cached), not the raw total
    assert resp.input_tokens == 180
    assert resp.cache_read_tokens == 320
    assert resp.cache_write_tokens == 0
    assert resp.text == "ok"


async def test_openai_adapter_with_no_system_blocks_still_works():
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_oai_response("hi"))
    provider = OpenAIProvider(client)
    await provider.chat(
        model="m", purpose="ingest",
        system_blocks=[],
        messages=[Message(role="user", content="x")],
    )
    msgs = client.chat.completions.create.await_args.kwargs["messages"]
    assert msgs == [{"role": "user", "content": "x"}]


# ---- pricing --------------------------------------------------------------

def test_estimate_cost_zero_for_unknown_model():
    assert estimate_cost_usd("not-a-model", input_tokens=1000, output_tokens=500) == 0.0


def test_estimate_cost_applies_cache_discount():
    # claude-sonnet-4-6: $3/M input, $15/M output
    # Three disjoint buckets — input_tokens is fresh (non-cached) tokens only.
    full = estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=1_000_000, output_tokens=0,
    )
    all_cached = estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert full == pytest.approx(3.0)
    assert all_cached == pytest.approx(0.3)  # 10% of full rate


def test_estimate_cost_regression_disjoint_buckets_are_summed():
    """Regression: earlier implementation subtracted cache tokens from
    input_tokens, under-counting cost for any call that had BOTH fresh input
    AND cache hits — which is every Anthropic call with a cached system prompt.
    """
    # 200 fresh + 500 cache-read + 1000 cache-write at claude-sonnet-4-6 ($3/M):
    #   fresh cost        = 200   * 3       / 1M = 0.000600
    #   cache_read cost   = 500   * 3 * 0.1 / 1M = 0.000150
    #   cache_write cost  = 1000  * 3 * 1.25/ 1M = 0.003750
    #                                              ─────────
    # total ≈ 0.004500
    cost = estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=200, output_tokens=0,
        cache_read_tokens=500, cache_write_tokens=1000,
    )
    assert cost == pytest.approx(0.0045)
    # Fresh input MUST be billed — the old bug returned 0.003900 because it
    # subtracted cache from input, zeroing out the fresh-input cost.
    assert cost > 0.004
