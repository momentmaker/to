"""OpenAI GPT adapter. Concatenates system_blocks into a single `system`
message — OpenAI auto-caches prefixes ≥1024 tokens on gpt-4o / gpt-4.1 family.
"""

from __future__ import annotations

from typing import Any

from bot.llm.base import LlmResponse, Message, Purpose


class OpenAIProvider:
    name = "openai"

    def __init__(self, client: Any):
        self._client = client

    async def chat(
        self,
        *,
        model: str,
        purpose: Purpose,
        system_blocks: list[str],
        messages: list[Message],
        max_tokens: int = 1024,
    ) -> LlmResponse:
        oai_messages: list[dict[str, Any]] = []
        system_text = "\n\n".join(b for b in system_blocks if b)
        if system_text:
            oai_messages.append({"role": "system", "content": system_text})
        for m in messages:
            oai_messages.append({"role": m.role, "content": m.content})

        resp = await self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=oai_messages,
        )

        text = resp.choices[0].message.content or ""
        usage = resp.usage
        cache_read = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0
        # OpenAI's prompt_tokens INCLUDES cached_tokens. Subtract so
        # input_tokens/cache_read_tokens/cache_write_tokens stay disjoint —
        # matching Anthropic's native semantics and letting the shared cost
        # formula work correctly.
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        fresh_input = max(prompt_tokens - cache_read, 0)

        return LlmResponse(
            text=text,
            model=model,
            provider=self.name,
            input_tokens=fresh_input,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,  # OpenAI doesn't separately bill cache writes
            stop_reason=getattr(resp.choices[0], "finish_reason", None),
        )

    async def vision(
        self,
        *,
        model: str,
        image_b64: str,
        mime_type: str,
        prompt: str,
        max_tokens: int = 512,
    ) -> LlmResponse:
        resp = await self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                ],
            }],
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        cache_read = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        fresh_input = max(prompt_tokens - cache_read, 0)
        return LlmResponse(
            text=text, model=model, provider=self.name,
            input_tokens=fresh_input,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=cache_read, cache_write_tokens=0,
            stop_reason=getattr(resp.choices[0], "finish_reason", None),
        )
