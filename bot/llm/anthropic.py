"""Anthropic Claude adapter. Marks each system block with ephemeral
`cache_control` so prompt prefixes are cached across calls.
"""

from __future__ import annotations

from typing import Any

from bot.llm.base import LlmResponse, Message, Purpose


class AnthropicProvider:
    name = "anthropic"

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
        system: list[dict[str, Any]] = [
            {"type": "text", "text": b, "cache_control": {"type": "ephemeral"}}
            for b in system_blocks
            if b
        ]
        anth_messages = [
            {"role": m.role, "content": m.content} for m in messages
        ]
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system if system else [],
            messages=anth_messages,
        )
        # Extract plain text from the first text block
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        text = "".join(text_parts)

        usage = resp.usage
        return LlmResponse(
            text=text,
            model=model,
            provider=self.name,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            stop_reason=getattr(resp, "stop_reason", None),
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
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        usage = resp.usage
        return LlmResponse(
            text="".join(text_parts),
            model=model, provider=self.name,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            stop_reason=getattr(resp, "stop_reason", None),
        )
