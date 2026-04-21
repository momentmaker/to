"""Voice note ingestion: Telegram voice/audio → Whisper → transcript."""

from __future__ import annotations

import logging

from bot.config import Settings
from bot.llm import whisper

log = logging.getLogger(__name__)


async def transcribe_voice_bytes(
    audio_bytes: bytes, *, filename: str = "voice.ogg", settings: Settings,
) -> str:
    """Return the transcript text for an audio blob. Raises on Whisper failure."""
    result = await whisper.transcribe(audio_bytes, filename=filename, settings=settings)
    return result.text
