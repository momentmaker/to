from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from bot import webhook as webhook_module
from bot.config import Settings
from bot.handlers import is_owner


def _fake_update(user_id: int):
    u = MagicMock()
    u.effective_user = MagicMock()
    u.effective_user.id = user_id
    return u


def test_owner_guard_rejects_other_chat_ids():
    settings = Settings(TELEGRAM_OWNER_ID=42)
    assert is_owner(_fake_update(42), settings) is True
    assert is_owner(_fake_update(43), settings) is False


def test_owner_guard_rejects_when_owner_unset():
    settings = Settings(TELEGRAM_OWNER_ID=0)
    assert is_owner(_fake_update(42), settings) is False


def test_webhook_rejects_missing_secret_token():
    settings = Settings(TELEGRAM_WEBHOOK_SECRET="shhh")
    bot_app = MagicMock()
    bot_app.process_update = AsyncMock()
    bot_app.bot = MagicMock()
    webhook_module.init_webhook(bot_app, settings)

    client = TestClient(webhook_module.app)
    r = client.post("/webhook", json={})
    assert r.status_code == 403
    bot_app.process_update.assert_not_awaited()


def test_webhook_rejects_wrong_secret_token():
    settings = Settings(TELEGRAM_WEBHOOK_SECRET="shhh")
    bot_app = MagicMock()
    bot_app.process_update = AsyncMock()
    bot_app.bot = MagicMock()
    webhook_module.init_webhook(bot_app, settings)

    client = TestClient(webhook_module.app)
    r = client.post("/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert r.status_code == 403
    bot_app.process_update.assert_not_awaited()


def test_webhook_health_returns_ok():
    client = TestClient(webhook_module.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_start_handler_responds_with_greeting(conn):
    from unittest.mock import AsyncMock
    from bot.handlers import start_handler
    from bot.persona import GREETING
    from bot.config import Settings

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await start_handler(update, context)
    update.message.reply_text.assert_awaited_once_with(GREETING)


@pytest.mark.asyncio
async def test_start_handler_ignored_for_non_owner(conn):
    from unittest.mock import AsyncMock
    from bot.handlers import start_handler
    from bot.config import Settings

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 99
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await start_handler(update, context)
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_message_handler_stores_capture_and_acks(conn):
    from unittest.mock import AsyncMock
    from bot import db as db_mod
    from bot.handlers import text_message_handler
    from bot.persona import ACK_TEXT
    from bot.config import Settings

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "a line i overheard"
    update.message.message_id = 1234
    update.message.forward_origin = None
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await text_message_handler(update, context)

    total = await db_mod.count_captures(conn)
    assert total == 1
    update.message.reply_text.assert_awaited_once_with(ACK_TEXT)


@pytest.mark.asyncio
async def test_text_message_handler_ignores_non_private_chats(conn):
    from unittest.mock import AsyncMock
    from bot import db as db_mod
    from bot.handlers import text_message_handler
    from bot.config import Settings

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "hello group"
    update.message.chat = MagicMock()
    update.message.chat.type = "group"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await text_message_handler(update, context)

    assert await db_mod.count_captures(conn) == 0
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_handler_reports_corpus_and_week_counts(conn):
    from unittest.mock import AsyncMock
    from bot import db as db_mod
    from bot.handlers import status_handler
    from bot.config import Settings
    from datetime import date

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    await db_mod.insert_capture(
        conn, kind="text", raw="first", dob=date(1990, 1, 1), tz_name="UTC"
    )
    await db_mod.insert_capture(
        conn, kind="text", raw="second", dob=date(1990, 1, 1), tz_name="UTC"
    )

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await status_handler(update, context)

    update.message.reply_text.assert_awaited_once()
    body = update.message.reply_text.await_args.args[0]
    assert body.startswith("corpus: 2\n")
    assert "this week" in body
    # Foundational config visible for quick verification
    assert "dob: 1990-01-01" in body
    assert "tz: UTC" in body
    assert "digest cron:" in body


@pytest.mark.asyncio
async def test_status_handler_ignored_for_non_owner(conn):
    from unittest.mock import AsyncMock
    from bot.handlers import status_handler
    from bot.config import Settings

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 99
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await status_handler(update, context)
    update.message.reply_text.assert_not_awaited()


def test_create_bot_app_fails_fast_on_missing_dob():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="")
    with pytest.raises(ValueError):
        asyncio.run(create_bot_app(settings))


def test_create_bot_app_fails_fast_on_missing_owner():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=0, DOB="1990-01-01")
    with pytest.raises(RuntimeError, match="TELEGRAM_OWNER_ID"):
        asyncio.run(create_bot_app(settings))


def test_create_bot_app_fails_fast_on_missing_token():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_OWNER_ID=42, DOB="1990-01-01")
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        asyncio.run(create_bot_app(settings))


def test_create_bot_app_fails_fast_when_no_llm_keys():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY / OPENAI_API_KEY"):
        asyncio.run(create_bot_app(settings))


def test_create_bot_app_fails_fast_on_invalid_provider_name():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        LLM_PROVIDER_INGEST="claude",  # typo: should be "anthropic"
    )
    with pytest.raises(RuntimeError, match="LLM_PROVIDER_INGEST"):
        asyncio.run(create_bot_app(settings))


def test_create_bot_app_fails_fast_on_invalid_timezone():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42,
        DOB="1990-01-01", TIMEZONE="Not/AZone",
    )
    with pytest.raises(RuntimeError, match="TIMEZONE"):
        asyncio.run(create_bot_app(settings))


def test_create_bot_app_fails_fast_on_empty_timezone():
    import asyncio
    from bot.bot_app import create_bot_app
    from bot.config import Settings

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42,
        DOB="1990-01-01", TIMEZONE="",
    )
    with pytest.raises(RuntimeError, match="TIMEZONE"):
        asyncio.run(create_bot_app(settings))


def test_webhook_acks_unrecognized_update_without_crashing():
    from unittest.mock import AsyncMock, patch
    from bot import webhook as webhook_module
    from bot.config import Settings

    settings = Settings(TELEGRAM_WEBHOOK_SECRET="")
    bot_app = MagicMock()
    bot_app.process_update = AsyncMock()
    bot_app.bot = MagicMock()
    webhook_module.init_webhook(bot_app, settings)

    client = TestClient(webhook_module.app)
    with patch("telegram.Update.de_json", return_value=None):
        r = client.post("/webhook", json={"update_id": 1, "mystery_field": {}})
    assert r.status_code == 200
    bot_app.process_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_message_handler_routes_urls_through_scraper(conn):
    from unittest.mock import AsyncMock, patch
    from bot.handlers import text_message_handler
    from bot.config import Settings
    from bot.ingest.router import UrlScrapeResult

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "https://example.com/article check this"
    update.message.message_id = 9001
    update.message.forward_origin = None
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    fake_scrape = UrlScrapeResult(
        source="article",
        payload={"title": "An Example Piece", "text": "body", "method": "readability"},
        content="An Example Piece\n\nbody",
    )
    with patch("bot.handlers.scrape_url", AsyncMock(return_value=fake_scrape)):
        await text_message_handler(update, context)

    import json as _json
    async with conn.execute(
        "SELECT kind, source, url, payload FROM captures WHERE telegram_msg_id = ?", (9001,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["kind"] == "url"
    assert row["source"] == "article"
    assert row["url"] == "https://example.com/article"
    payload = _json.loads(row["payload"])
    assert payload["scrape"]["source"] == "article"
    assert payload["scrape"]["title"] == "An Example Piece"
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_message_handler_skips_llm_when_scrape_fails_and_content_is_just_url(conn):
    """Scrape failure leaves processing_content == raw URL. Don't waste tokens
    running ingest over a bare URL string.
    """
    from unittest.mock import AsyncMock, patch
    from bot.handlers import text_message_handler
    from bot.config import Settings
    from bot.ingest.router import UrlScrapeResult
    from bot.llm.base import LlmResponse
    from bot.llm.router import Providers
    import asyncio as _asyncio

    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k",
    )

    class _SpyProv:
        name = "anthropic"
        called = False
        async def chat(self, **kwargs):
            _SpyProv.called = True
            return LlmResponse(
                text='{"title":"","tags":[],"quotes":[],"summary":""}',
                model="m", provider="anthropic", input_tokens=1, output_tokens=1,
            )
    providers = Providers(_SpyProv(), None)

    failed_scrape = UrlScrapeResult(
        source="article", payload={},
        content="https://example.com/dead",  # just the URL
        error="DNS fail",
    )

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "https://example.com/dead"
    update.message.message_id = 9600
    update.message.forward_origin = None
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    update.message.chat.id = 99
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    with patch("bot.handlers.scrape_url", AsyncMock(return_value=failed_scrape)):
        await text_message_handler(update, context)

    # Drain any scheduled background tasks (the why prompt, but NOT ingest)
    pending = [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]
    for t in pending:
        try:
            await t
        except Exception:
            pass

    # Ingest LLM should NOT have been called — content was just the URL.
    # The why LLM still runs (it has its own prompt); we filter by checking the
    # chat call's system_blocks or by verifying capture.processed is not set.
    async with conn.execute(
        "SELECT status, processed FROM captures WHERE telegram_msg_id = ?", (9600,)
    ) as cur:
        row = await cur.fetchone()
    # Status stays 'pending' because we skipped processing
    assert row["status"] == "pending"
    assert row["processed"] is None


@pytest.mark.asyncio
async def test_text_message_handler_pushes_to_github_when_llm_skipped(conn):
    """Regression: when the scrape fails and LLM processing is skipped, the
    capture still needs to reach the GitHub repo — otherwise it sits in
    SQLite until nightly_sync, which is hours away.
    """
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, patch
    from bot.handlers import text_message_handler
    from bot.config import Settings
    from bot.ingest.router import UrlScrapeResult
    from bot.llm.router import Providers

    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k",
        GITHUB_TOKEN="ghp_x", GITHUB_REPO="u/r", GITHUB_BRANCH="main",
    )

    class _Prov:
        name = "anthropic"
        async def chat(self, **kwargs):
            from bot.llm.base import LlmResponse
            return LlmResponse(
                text='one short line.', model="m", provider="anthropic",
                input_tokens=1, output_tokens=1,
            )
    providers = Providers(_Prov(), None)

    failed_scrape = UrlScrapeResult(
        source="article", payload={},
        content="https://example.com/dead", error="DNS fail",
    )

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "https://example.com/dead"
    update.message.message_id = 9601
    update.message.forward_origin = None
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.chat.id = 99
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    push_mock = AsyncMock(return_value=True)
    with patch("bot.handlers.scrape_url", AsyncMock(return_value=failed_scrape)), \
         patch("bot.github_sync.push_capture", push_mock):
        await text_message_handler(update, context)
        # Drain background tasks so the push completes
        pending = [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]
        for t in pending:
            try:
                await t
            except Exception:
                pass

    # push_capture should have fired for the failed-scrape capture, even
    # though LLM processing was skipped.
    push_mock.assert_awaited()


@pytest.mark.asyncio
async def test_text_message_handler_records_scrape_error_without_crashing(conn):
    from unittest.mock import AsyncMock, patch
    from bot.handlers import text_message_handler
    from bot.config import Settings
    from bot.ingest.router import UrlScrapeResult

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "https://example.com/nope"
    update.message.message_id = 9002
    update.message.forward_origin = None
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    failed_scrape = UrlScrapeResult(
        source="article", payload={}, content="https://example.com/nope",
        error="DNS fail",
    )
    with patch("bot.handlers.scrape_url", AsyncMock(return_value=failed_scrape)):
        await text_message_handler(update, context)

    import json as _json
    async with conn.execute(
        "SELECT payload FROM captures WHERE telegram_msg_id = ?", (9002,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    payload = _json.loads(row["payload"])
    assert payload["scrape_error"] == "DNS fail"
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_message_handler_runs_llm_processing_in_background(conn):
    from unittest.mock import AsyncMock, patch
    from bot import db as db_mod
    from bot.handlers import text_message_handler
    from bot.config import Settings
    from bot.llm.base import LlmResponse
    from bot.llm.router import Providers
    import asyncio as _asyncio

    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
    )

    class _FakeProv:
        name = "anthropic"
        async def chat(self, **kwargs):
            return LlmResponse(
                text='{"title": "T", "tags": ["a"], "quotes": [], "summary": "s"}',
                model="claude-sonnet-4-6", provider="anthropic",
                input_tokens=10, output_tokens=5,
            )

    providers = Providers(_FakeProv(), None)

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = MagicMock()
    update.message.text = "a quiet line"
    update.message.message_id = 9003
    update.message.forward_origin = None
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    await text_message_handler(update, context)

    # Wait for background task to complete — it's the only pending task in the loop.
    pending = [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]
    for t in pending:
        await t

    async with conn.execute(
        "SELECT status, processed FROM captures WHERE telegram_msg_id = ?", (9003,)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "processed"
    assert '"title": "T"' in row["processed"]


@pytest.mark.asyncio
async def test_duplicate_telegram_webhook_delivery_stores_only_once(conn):
    from unittest.mock import AsyncMock
    from bot import db as db_mod
    from bot.handlers import text_message_handler
    from bot.config import Settings

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    def _update_for_msg(msg_id: int):
        u = MagicMock()
        u.effective_user = MagicMock()
        u.effective_user.id = 42
        u.message = MagicMock()
        u.message.text = "a single line"
        u.message.message_id = msg_id
        u.message.forward_origin = None
        u.message.chat = MagicMock()
        u.message.chat.type = "private"
        u.message.reply_text = AsyncMock()
        return u

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    first = _update_for_msg(555)
    second = _update_for_msg(555)

    await text_message_handler(first, context)
    await text_message_handler(second, context)

    assert await db_mod.count_captures(conn) == 1
    first.message.reply_text.assert_awaited_once()
    second.message.reply_text.assert_not_awaited()


def test_webhook_accepts_correct_secret_token():
    from unittest.mock import AsyncMock, patch
    from bot import webhook as webhook_module
    from bot.config import Settings

    settings = Settings(TELEGRAM_WEBHOOK_SECRET="shhh")
    bot_app = MagicMock()
    bot_app.process_update = AsyncMock()
    bot_app.bot = MagicMock()
    webhook_module.init_webhook(bot_app, settings)

    client = TestClient(webhook_module.app)
    with patch("telegram.Update.de_json", return_value=MagicMock()):
        r = client.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "shhh"},
        )
    assert r.status_code == 200
    bot_app.process_update.assert_awaited_once()
