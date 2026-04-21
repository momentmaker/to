from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Settings
from bot.ingest import vision as vision_mod
from bot.ingest import voice as voice_mod
from bot.llm import whisper
from bot.llm.anthropic import AnthropicProvider
from bot.llm.base import LlmResponse
from bot.llm.openai import OpenAIProvider
from bot.llm.router import Providers


# ---- Whisper --------------------------------------------------------------

@pytest.mark.asyncio
async def test_whisper_transcribe_reads_bytes_and_returns_text():
    fake_client = MagicMock()
    fake_client.audio = MagicMock()
    fake_client.audio.transcriptions = MagicMock()
    fake_client.audio.transcriptions.create = AsyncMock(
        return_value=SimpleNamespace(text="  the line I heard at the bar  ")
    )
    result = await whisper.transcribe(b"ogg-bytes", filename="voice.ogg", client=fake_client)
    assert result.text == "the line I heard at the bar"
    assert result.model == "whisper-1"

    call_kwargs = fake_client.audio.transcriptions.create.await_args.kwargs
    assert call_kwargs["model"] == "whisper-1"
    # file should be a file-like with .name set for MIME sniffing
    file_obj = call_kwargs["file"]
    assert getattr(file_obj, "name", None) == "voice.ogg"


@pytest.mark.asyncio
async def test_whisper_requires_client_or_settings_with_key():
    settings = Settings(TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
                        OPENAI_API_KEY="")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await whisper.transcribe(b"x", settings=settings)


@pytest.mark.asyncio
async def test_whisper_skips_api_call_on_empty_audio():
    fake_client = MagicMock()
    fake_client.audio = MagicMock()
    fake_client.audio.transcriptions = MagicMock()
    fake_client.audio.transcriptions.create = AsyncMock()

    result = await whisper.transcribe(b"", filename="v.ogg", client=fake_client)
    assert result.text == ""
    fake_client.audio.transcriptions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcribe_voice_bytes_wraps_whisper(conn, monkeypatch):
    async def _fake(audio, *, filename, client=None, settings=None):
        return whisper.Transcription(text="hello world", model="whisper-1")

    monkeypatch.setattr(whisper, "transcribe", _fake)
    settings = Settings(TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
                        OPENAI_API_KEY="k")
    text = await voice_mod.transcribe_voice_bytes(b"x", settings=settings)
    assert text == "hello world"


# ---- Vision ---------------------------------------------------------------

def _anth_vision_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=30,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
        stop_reason="end_turn",
    )


def _oai_vision_response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=30,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


def _vision_settings(provider: str = "anthropic"):
    return Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k",
        LLM_PROVIDER_VISION=provider,
    )


@pytest.mark.asyncio
async def test_anthropic_vision_returns_text_and_usage():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_anth_vision_response('{"ocr": "hi", "description": "a sign"}')
    )
    provider = AnthropicProvider(client)
    resp = await provider.vision(
        model="claude-sonnet-4-6", image_b64="QUJD",
        mime_type="image/jpeg", prompt="describe",
    )
    assert '"ocr"' in resp.text
    kwargs = client.messages.create.await_args.kwargs
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[1]["type"] == "text"


@pytest.mark.asyncio
async def test_openai_vision_uses_data_url():
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_oai_vision_response('{"ocr": "", "description": "a cat"}')
    )
    provider = OpenAIProvider(client)
    await provider.vision(
        model="gpt-4.1-mini", image_b64="QUJD",
        mime_type="image/png", prompt="describe",
    )
    kwargs = client.chat.completions.create.await_args.kwargs
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,QUJD")


@pytest.mark.asyncio
async def test_ocr_and_describe_routes_to_anthropic_and_parses_json(conn):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_anth_vision_response('{"ocr": "the quote", "description": "a screenshot"}')
    )
    providers = Providers(AnthropicProvider(client), None)
    result = await vision_mod.ocr_and_describe(
        b"image-bytes", mime_type="image/jpeg",
        settings=_vision_settings("anthropic"), providers=providers, conn=conn,
    )
    assert result == {"ocr": "the quote", "description": "a screenshot"}

    # usage row written with purpose='vision'
    async with conn.execute("SELECT purpose FROM llm_usage") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["purpose"] == "vision"


@pytest.mark.asyncio
async def test_ocr_and_describe_falls_back_when_json_malformed(conn):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_anth_vision_response("I cannot comply with this request.")
    )
    providers = Providers(AnthropicProvider(client), None)
    result = await vision_mod.ocr_and_describe(
        b"image-bytes", mime_type="image/jpeg",
        settings=_vision_settings("anthropic"), providers=providers, conn=conn,
    )
    assert result == {"ocr": "", "description": ""}


@pytest.mark.asyncio
async def test_ocr_and_describe_routes_to_openai(conn):
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_oai_vision_response('{"ocr": "", "description": "a dog"}')
    )
    providers = Providers(None, OpenAIProvider(client))
    result = await vision_mod.ocr_and_describe(
        b"x", mime_type="image/jpeg",
        settings=_vision_settings("openai"), providers=providers, conn=conn,
    )
    assert result["description"] == "a dog"
    client.chat.completions.create.assert_awaited_once()


# ---- Handler integration --------------------------------------------------

@pytest.mark.asyncio
async def test_voice_message_handler_stores_transcript(conn, monkeypatch):
    from bot.handlers import voice_message_handler
    from bot.config import Settings

    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
        OPENAI_API_KEY="k",
    )

    async def _fake_transcribe(audio_bytes, *, filename, settings):
        return "a whispered line"
    monkeypatch.setattr(voice_mod, "transcribe_voice_bytes", _fake_transcribe)

    audio = MagicMock()
    audio.file_name = "voice.ogg"
    fake_file = MagicMock()
    fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg-bytes"))
    audio.get_file = AsyncMock(return_value=fake_file)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.voice = audio
    update.message.audio = None
    update.message.forward_origin = None
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.message_id = 1001
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await voice_message_handler(update, context)

    import json as _json
    async with conn.execute(
        "SELECT kind, raw, payload FROM captures WHERE telegram_msg_id = ?", (1001,)
    ) as cur:
        row = await cur.fetchone()
    assert row["kind"] == "voice"
    assert row["raw"] == "a whispered line"
    payload = _json.loads(row["payload"])
    assert payload["transcript"] == "a whispered line"
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_photo_message_handler_stores_ocr_and_description(conn, monkeypatch):
    from bot.handlers import photo_message_handler
    from bot.config import Settings
    from bot.llm.router import Providers

    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k",
    )

    class _DummyProv:
        name = "anthropic"
    providers = Providers(_DummyProv(), None)

    async def _fake_ocr(image_bytes, *, mime_type, settings, providers, conn, max_tokens=512):
        return {"ocr": "a Kindle highlight", "description": "a screenshot of text"}
    monkeypatch.setattr(vision_mod, "ocr_and_describe", _fake_ocr)

    small = MagicMock()
    big = MagicMock()
    fake_file = MagicMock()
    fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"jpg-bytes"))
    big.get_file = AsyncMock(return_value=fake_file)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.photo = [small, big]  # last is largest
    update.message.caption = "look at this"
    update.message.forward_origin = None
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.message_id = 2001
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    await photo_message_handler(update, context)

    import json as _json
    async with conn.execute(
        "SELECT kind, raw, payload FROM captures WHERE telegram_msg_id = ?", (2001,)
    ) as cur:
        row = await cur.fetchone()
    assert row["kind"] == "image"
    assert row["raw"] == "look at this"
    payload = _json.loads(row["payload"])
    assert payload["caption"] == "look at this"
    assert payload["vision"]["ocr"] == "a Kindle highlight"
    update.message.reply_text.assert_awaited_once()
    # largest photo was selected (last in list)
    big.get_file.assert_awaited_once()
