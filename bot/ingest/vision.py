"""Image ingestion: OCR + description via the configured vision provider.

Returns {"ocr": str, "description": str}. The LLM call is counted against the
llm_usage ledger with purpose='vision'.
"""

from __future__ import annotations

import base64
import json
import logging
import re

import aiosqlite

from bot.config import Settings
from bot.llm import budget
from bot.llm.router import Providers

log = logging.getLogger(__name__)


VISION_PROMPT = """Describe this image for a personal commonplace book.

Return a single JSON object with two keys and no prose outside the JSON:
  "ocr":         any printed or handwritten text visible in the image, verbatim. Empty string if none.
  "description": one-to-two-sentence description of what the image shows (<=300 chars).

Rules:
- If the image is a screenshot of text (e.g. a Kindle highlight, a note, a tweet screenshot), the ocr field is the extracted text.
- Do not add commentary inside the ocr field — copy only what is actually in the image.
- Respond with ONLY the JSON.
""".strip()


def _coerce_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _normalize(obj: dict) -> dict:
    ocr = obj.get("ocr") if isinstance(obj.get("ocr"), str) else ""
    desc = obj.get("description") if isinstance(obj.get("description"), str) else ""
    return {"ocr": ocr, "description": desc}


async def ocr_and_describe(
    image_bytes: bytes,
    *,
    mime_type: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
    max_tokens: int = 1024,
) -> dict:
    """Run vision on the image; returns {"ocr": str, "description": str}."""
    from bot.llm.router import model_for_purpose
    provider = providers.pick(settings.LLM_PROVIDER_VISION, purpose="vision")
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    model = await model_for_purpose(settings, "vision", provider.name, conn)

    response = await provider.vision(
        model=model,
        image_b64=image_b64,
        mime_type=mime_type,
        prompt=VISION_PROMPT,
        max_tokens=max_tokens,
    )
    await budget.record_usage(conn, purpose="vision", response=response)
    parsed = _coerce_json(response.text) or {}
    return _normalize(parsed)
