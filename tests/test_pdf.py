"""Tests for PDF ingestion: extraction, classification, handler routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Settings
from bot.ingest import pdf as pdf_mod
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


# ---- pure logic ----------------------------------------------------------

def test_estimate_tokens_uses_4_chars_per_token():
    assert pdf_mod.estimate_tokens("") == 0
    assert pdf_mod.estimate_tokens("x" * 4) == 1
    assert pdf_mod.estimate_tokens("x" * 4000) == 1000


def test_classify_tiny_under_threshold():
    tier, reason = pdf_mod.classify(page_count=3, token_estimate=500)
    assert tier == "tiny"
    assert reason is None


def test_classify_medium_between_thresholds():
    tier, reason = pdf_mod.classify(page_count=8, token_estimate=10_000)
    assert tier == "medium"
    assert reason is None


def test_classify_large_over_token_threshold():
    tier, reason = pdf_mod.classify(page_count=10, token_estimate=25_000)
    assert tier == "large"
    assert reason is not None
    assert "25000" in reason or "tokens" in reason


def test_classify_large_over_page_threshold():
    tier, reason = pdf_mod.classify(page_count=100, token_estimate=1_000)
    assert tier == "large"
    assert reason is not None
    assert "pages" in reason


# ---- extract_pdf_bytes ---------------------------------------------------

class _FakePage:
    def __init__(self, text: str):
        self._text = text
    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, pages_text: list[str], *, encrypted: bool = False):
        self.pages = [_FakePage(t) for t in pages_text]
        self.is_encrypted = encrypted


def test_extract_happy_path_tiny(monkeypatch):
    monkeypatch.setattr(
        pdf_mod, "__name__", "bot.ingest.pdf"
    )  # stability; no-op but documents intent
    from pypdf import PdfReader as _Real
    fake = _FakeReader(["chapter one", "chapter two"])
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    out = pdf_mod.extract_pdf_bytes(b"%PDF-1.4 stub")
    assert "chapter one" in out.text
    assert "chapter two" in out.text
    assert out.page_count == 2
    assert out.tier == "tiny"
    assert out.rejected_reason is None


def test_extract_rejects_encrypted(monkeypatch):
    fake = _FakeReader([], encrypted=True)
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    out = pdf_mod.extract_pdf_bytes(b"%PDF-1.4 stub")
    assert out.tier == "large"
    assert out.rejected_reason is not None
    assert "password" in out.rejected_reason.lower()


def test_extract_rejects_too_many_pages(monkeypatch):
    fake = _FakeReader(["x"] * (pdf_mod.MAX_PAGES + 5))
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    out = pdf_mod.extract_pdf_bytes(b"%PDF-1.4 stub")
    assert out.tier == "large"
    assert out.page_count == pdf_mod.MAX_PAGES + 5
    assert out.rejected_reason is not None
    assert "pages" in out.rejected_reason


def test_extract_rejects_too_many_tokens(monkeypatch):
    # One page with a huge body — past the token threshold.
    big_text = "x" * (pdf_mod.LARGE_TOKENS * 4 + 100)  # ~LARGE_TOKENS+25 tokens
    fake = _FakeReader([big_text])
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    out = pdf_mod.extract_pdf_bytes(b"%PDF-1.4 stub")
    assert out.tier == "large"
    assert out.token_estimate > pdf_mod.LARGE_TOKENS
    assert out.rejected_reason is not None


def test_extract_handles_malformed_pdf(monkeypatch):
    """pypdf raises PdfReadError on corrupt bytes; we swallow and reject."""
    from pypdf.errors import PdfReadError
    def _raising(*_a, **_kw):
        raise PdfReadError("not a pdf")
    monkeypatch.setattr("pypdf.PdfReader", _raising)

    out = pdf_mod.extract_pdf_bytes(b"garbage")
    assert out.tier == "large"
    assert out.rejected_reason is not None
    assert "couldn't read" in out.rejected_reason.lower()


def test_extract_skips_failing_pages(monkeypatch):
    """If one page fails extraction, keep the others rather than bailing."""
    class _BrokenPage:
        def extract_text(self):
            raise RuntimeError("bad page")
    class _MixedReader:
        pages = [_FakePage("good one"), _BrokenPage(), _FakePage("good two")]
        is_encrypted = False
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: _MixedReader())

    out = pdf_mod.extract_pdf_bytes(b"%PDF-1.4 stub")
    assert out.tier == "tiny"
    assert "good one" in out.text
    assert "good two" in out.text


def test_extract_medium_tier(monkeypatch):
    # Text in the tiny < tokens <= large range.
    mid = "x" * ((pdf_mod.TINY_TOKENS + 1000) * 4)
    fake = _FakeReader([mid])
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    out = pdf_mod.extract_pdf_bytes(b"%PDF-1.4 stub")
    assert out.tier == "medium"
    assert out.rejected_reason is None
    assert out.token_estimate > pdf_mod.TINY_TOKENS
    assert out.token_estimate <= pdf_mod.LARGE_TOKENS


# ---- handler routing -----------------------------------------------------

def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
    )
    base.update(kw)
    return Settings(**base)


def _mock_message_with_pdf(*, pdf_bytes: bytes, mime: str = "application/pdf",
                           filename: str = "book.pdf", caption: str = ""):
    doc = MagicMock()
    doc.mime_type = mime
    doc.file_name = filename
    fake_file = MagicMock()
    fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(pdf_bytes))
    doc.get_file = AsyncMock(return_value=fake_file)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.document = doc
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.message_id = 777
    update.message.caption = caption or None
    update.message.forward_origin = None
    update.message.reply_text = AsyncMock()
    return update, doc


@pytest.mark.asyncio
async def test_document_handler_ignores_non_pdf_mime(conn):
    from bot.handlers import document_message_handler

    update, _doc = _mock_message_with_pdf(
        pdf_bytes=b"whatever", mime="application/zip",
    )
    context = MagicMock()
    context.bot_data = {"settings": _settings(), "db": conn, "providers": None}

    await document_message_handler(update, context)
    update.message.reply_text.assert_not_called()
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_document_handler_rejects_oversize(conn, monkeypatch):
    from bot.handlers import document_message_handler

    fake = _FakeReader(["x"] * (pdf_mod.MAX_PAGES + 10))
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    update, _doc = _mock_message_with_pdf(pdf_bytes=b"%PDF-1.4 stub")
    context = MagicMock()
    context.bot_data = {"settings": _settings(), "db": conn, "providers": None}

    await document_message_handler(update, context)
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "pages" in reply.lower()
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_document_handler_rejects_empty_text(conn, monkeypatch):
    """A scanned (image-only) PDF has no selectable text — reject with guidance."""
    from bot.handlers import document_message_handler

    fake = _FakeReader(["", "   ", ""])  # pages present but no text
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    update, _doc = _mock_message_with_pdf(pdf_bytes=b"%PDF-1.4 stub")
    context = MagicMock()
    context.bot_data = {"settings": _settings(), "db": conn, "providers": None}

    await document_message_handler(update, context)
    reply = update.message.reply_text.await_args.args[0]
    assert "scan" in reply.lower() or "photo" in reply.lower()
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_document_handler_happy_path_tiny(conn, monkeypatch):
    import asyncio as _asyncio
    from bot import handlers
    from bot.handlers import document_message_handler

    fake = _FakeReader(["the small ignition", "one more line"])
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **kw: fake)

    # Stub out the background processing so the test doesn't need a real LLM.
    async def _noop_process(**kwargs): return None
    monkeypatch.setattr(handlers, "_process_in_background", _noop_process)

    class _Prov:
        name = "anthropic"
        async def chat(self, **kwargs):
            return LlmResponse(
                text='{"title":"","tags":[],"quotes":[],"summary":""}',
                model="m", provider="anthropic", input_tokens=1, output_tokens=1,
            )
    providers = Providers(_Prov(), None)

    update, _doc = _mock_message_with_pdf(
        pdf_bytes=b"%PDF-1.4 stub", filename="notes.pdf", caption="this week's reading",
    )
    context = MagicMock()
    context.bot_data = {"settings": _settings(), "db": conn, "providers": providers}

    await document_message_handler(update, context)
    # drain background tasks
    for t in [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]:
        try:
            await t
        except Exception:
            pass

    async with conn.execute(
        "SELECT kind, raw, payload FROM captures ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["kind"] == "pdf"
    assert "the small ignition" in row["raw"]
    assert "one more line" in row["raw"]
    import json
    payload = json.loads(row["payload"])
    assert payload["filename"] == "notes.pdf"
    assert payload["page_count"] == 2
    assert payload["tier"] == "tiny"
    assert payload["caption"] == "this week's reading"

    reply = update.message.reply_text.await_args.args[0]
    assert "2p" in reply
    assert "tiny" in reply
