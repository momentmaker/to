"""Provider selection + unified call_llm that records usage.

Callers pass `purpose` (ingest/daily/why/digest/oracle/tweet/vision) and this
module picks provider + model from settings, calls it, and writes an
llm_usage row.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from bot.config import Settings
from bot.llm import budget
from bot.llm.anthropic import AnthropicProvider
from bot.llm.base import LlmResponse, Message, Provider, Purpose
from bot.llm.openai import OpenAIProvider

log = logging.getLogger(__name__)

# Purposes for which we've already warned about a silent provider fallback.
# Warning once per (purpose, requested) pair keeps logs readable.
_warned_fallbacks: set[tuple[str, str]] = set()


_PROVIDER_ATTR: dict[Purpose, str] = {
    "ingest":  "LLM_PROVIDER_INGEST",
    "daily":   "LLM_PROVIDER_DAILY",
    "why":     "LLM_PROVIDER_WHY",
    "digest":  "LLM_PROVIDER_DIGEST",
    "oracle":  "LLM_PROVIDER_ORACLE",
    "tweet":   "LLM_PROVIDER_TWEET",
    "vision":  "LLM_PROVIDER_VISION",
}


class Providers:
    """Holds initialized Provider instances. Kept in app.bot_data["providers"]."""

    def __init__(self, anthropic: Provider | None, openai: Provider | None):
        self.anthropic = anthropic
        self.openai = openai

    def pick(self, name: str, *, purpose: str = "") -> Provider:
        if name == "anthropic" and self.anthropic is not None:
            return self.anthropic
        if name == "openai" and self.openai is not None:
            return self.openai
        # Fallback to whichever is configured, with a one-time warning so the
        # user notices that their per-purpose env setting is being ignored.
        fallback = self.anthropic or self.openai
        if fallback is not None:
            key = (purpose or "<unspecified>", name)
            if key not in _warned_fallbacks:
                _warned_fallbacks.add(key)
                log.warning(
                    "requested provider %r for purpose=%s is not configured; "
                    "falling back to %s. set the corresponding API key or update "
                    "the LLM_PROVIDER_* env var to silence this warning.",
                    name, purpose or "?", fallback.name,
                )
            return fallback
        raise RuntimeError("no LLM provider configured")


def build_providers(settings: Settings, *, anthropic_client=None, openai_client=None) -> Providers:
    """Construct Providers; real clients are lazy-imported so tests can pass stubs."""
    anth: Provider | None = None
    oai: Provider | None = None

    if settings.ANTHROPIC_API_KEY and anthropic_client is None:
        from anthropic import AsyncAnthropic
        anthropic_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    if anthropic_client is not None:
        anth = AnthropicProvider(anthropic_client)

    if settings.OPENAI_API_KEY and openai_client is None:
        from openai import AsyncOpenAI
        openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    if openai_client is not None:
        oai = OpenAIProvider(openai_client)

    if anth is None and oai is None:
        raise RuntimeError(
            "at least one of ANTHROPIC_API_KEY / OPENAI_API_KEY must be set"
        )
    return Providers(anth, oai)


async def model_for_purpose(
    settings: Settings, purpose: Purpose, provider_name: str,
    conn: aiosqlite.Connection,
) -> str:
    """Pick the model name for this call, applying budget-driven degrade.

    Rules:
    - Digest always uses the heavy model (weekly headline feature, never degrade).
    - Every other purpose checks `should_degrade`: above the soft cap, fall
      back to the `*_CHEAP` model for that provider.
    """
    degrade = await budget.should_degrade(conn, settings=settings, purpose=purpose)
    if provider_name == "anthropic":
        if purpose == "digest":
            return settings.CLAUDE_MODEL_DIGEST
        if degrade:
            return settings.CLAUDE_MODEL_CHEAP
        return settings.CLAUDE_MODEL_INGEST
    # openai
    if purpose == "digest":
        return settings.OPENAI_MODEL_DIGEST
    if degrade:
        return settings.OPENAI_MODEL_CHEAP
    return settings.OPENAI_MODEL_INGEST


async def call_llm(
    *,
    purpose: Purpose,
    system_blocks: list[str],
    messages: list[Message],
    max_tokens: int = 1024,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> LlmResponse:
    requested = getattr(settings, _PROVIDER_ATTR[purpose])
    provider = providers.pick(requested, purpose=purpose)
    model = await model_for_purpose(settings, purpose, provider.name, conn)
    resp = await provider.chat(
        model=model,
        purpose=purpose,
        system_blocks=system_blocks,
        messages=messages,
        max_tokens=max_tokens,
    )
    await budget.record_usage(conn, purpose=purpose, response=resp)
    # Fire-and-forget warn check. Never block the caller for alert I/O.
    try:
        await budget.check_and_warn_cap(conn, settings=settings)
    except Exception:
        log.exception("call_llm: budget warn check failed")
    return resp
