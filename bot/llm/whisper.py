"""OpenAI Whisper transcription — audio-to-text only.

Kept separate from the chat Provider because Whisper's API shape (multipart
file upload) is different from the chat-completions flow.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from bot.config import Settings

log = logging.getLogger(__name__)

_MODEL = "whisper-1"


@dataclass
class Transcription:
    text: str
    model: str


async def transcribe(
    audio_bytes: bytes,
    *,
    filename: str = "voice.ogg",
    client=None,
    settings: Settings | None = None,
) -> Transcription:
    """Transcribe audio bytes to text. Caller provides an AsyncOpenAI client OR
    settings with OPENAI_API_KEY so we can construct one.
    """
    # Don't round-trip to the API for a zero-byte file.
    if not audio_bytes:
        return Transcription(text="", model=_MODEL)

    if client is None:
        if settings is None or not settings.OPENAI_API_KEY:
            raise RuntimeError("whisper requires OPENAI_API_KEY or an openai client")
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    buf = io.BytesIO(audio_bytes)
    buf.name = filename  # OpenAI SDK sniffs mime from the .name attribute

    resp = await client.audio.transcriptions.create(
        model=_MODEL,
        file=buf,
    )
    text = getattr(resp, "text", "") or ""
    return Transcription(text=text.strip(), model=_MODEL)
