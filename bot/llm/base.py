"""Provider protocol + response type for LLM calls.

Both Anthropic and OpenAI adapters implement Provider. Callers use
`bot.llm.router.call_llm(purpose=...)` which picks provider + model from env.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

log = logging.getLogger(__name__)

_warned_unknown_models: set[str] = set()

Purpose = Literal["ingest", "daily", "why", "digest", "oracle", "tweet", "vision"]
Role = Literal["user", "assistant"]


@dataclass
class LlmResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None


@dataclass
class Message:
    role: Role
    content: str


class Provider(Protocol):
    name: str

    async def chat(
        self,
        *,
        model: str,
        purpose: Purpose,
        system_blocks: list[str],
        messages: list[Message],
        max_tokens: int = 1024,
    ) -> LlmResponse: ...


# Approximate per-million-token pricing (USD). Verify against current vendor
# pricing periodically; this is used only for the soft budget ledger, not
# billing. Caching discounts modeled as: cache_read ≈ 0.1× input,
# cache_write ≈ 1.25× input (Anthropic ephemeral-cache behavior).
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-sonnet-4-6":         (3.0,  15.0),
    "claude-haiku-4-5-20251001": (0.8,  4.0),
    "gpt-4.1":      (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
}


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate call cost.

    Convention: the three token buckets (`input_tokens`, `cache_read_tokens`,
    `cache_write_tokens`) are **disjoint** — `input_tokens` is the fresh,
    non-cached portion only. Anthropic reports this directly. The OpenAI
    adapter normalizes by subtracting `prompt_tokens_details.cached_tokens`
    from `prompt_tokens` before storing.
    """
    prices = PRICING.get(model)
    if prices is None:
        # Warn once per unknown model so the budget ledger doesn't silently
        # under-report forever if a user sets an unfamiliar model id.
        if model not in _warned_unknown_models:
            _warned_unknown_models.add(model)
            log.warning(
                "no pricing for model=%r; cost will be recorded as $0. "
                "add it to bot.llm.base.PRICING to enable the budget guard.",
                model,
            )
        return 0.0
    inp_per_m, out_per_m = prices
    cost = input_tokens * inp_per_m / 1_000_000
    cost += cache_read_tokens * inp_per_m * 0.1 / 1_000_000
    cost += cache_write_tokens * inp_per_m * 1.25 / 1_000_000
    cost += output_tokens * out_per_m / 1_000_000
    return round(cost, 6)
