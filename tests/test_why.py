from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import why
from bot.config import Settings
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


# ---- pending-why state machine -------------------------------------------

@pytest.mark.asyncio
async def test_set_and_get_pending_roundtrips(conn):
    await why.set_pending(conn, parent_id=42, window_minutes=10)
    pending = await why.get_pending(conn)
    assert pending is not None
    assert pending.parent_id == 42
    assert pending.deadline > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_consume_if_live_returns_parent_and_clears(conn):
    await why.set_pending(conn, parent_id=7, window_minutes=10)
    parent = await why.consume_if_live(conn)
    assert parent == 7
    # Cleared: second consume returns None
    assert await why.consume_if_live(conn) is None


@pytest.mark.asyncio
async def test_consume_if_live_expired_returns_none_and_clears(conn):
    # Force-set an expired deadline
    expired = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
        ("pending_why", json.dumps({"parent_id": 5, "deadline": expired}), "2000-01-01T00:00:00Z"),
    )
    await conn.commit()
    assert await why.consume_if_live(conn) is None
    # Row was cleared
    assert await why.get_pending(conn) is None


@pytest.mark.asyncio
async def test_corrupt_pending_row_is_self_healed(conn):
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
        ("pending_why", "not-json", "2000-01-01T00:00:00Z"),
    )
    await conn.commit()
    assert await why.get_pending(conn) is None
    # Row was cleared by the self-heal
    async with conn.execute("SELECT COUNT(*) FROM kv WHERE key = 'pending_why'") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_clear_pending_is_idempotent(conn):
    await why.clear_pending(conn)
    await why.clear_pending(conn)


@pytest.mark.asyncio
async def test_consume_if_live_is_atomic_under_concurrent_tasks(conn):
    """Regression: previously `consume_if_live` did SELECT then DELETE in two
    awaits, so two concurrent tasks could both see the same live row and both
    return its parent_id. With DELETE RETURNING only one task wins.
    """
    await why.set_pending(conn, parent_id=99, window_minutes=10)

    # Run two consumers concurrently against the same connection.
    a, b = await asyncio.gather(
        why.consume_if_live(conn),
        why.consume_if_live(conn),
    )
    # Exactly one winner, one None.
    assert {a, b} == {99, None}


# ---- ask_why_question -----------------------------------------------------

class _FakeProv:
    def __init__(self, text: str):
        self.name = "anthropic"
        self._text = text
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LlmResponse(
            text=self._text, model=kwargs["model"], provider=self.name,
            input_tokens=20, output_tokens=10,
        )


def _settings():
    return Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
    )


@pytest.mark.asyncio
async def test_ask_why_question_uses_orchurator_voice_block(conn):
    prov = _FakeProv("what in the title stopped you?")
    providers = Providers(prov, None)
    q = await why.ask_why_question(
        url="https://example.com/a", title="The Impediment",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert q == "what in the title stopped you?"
    # Verify the VOICE_ORCHURATOR block is present in the system blocks
    sent_blocks = prov.calls[0]["system_blocks"]
    joined = "\n\n".join(sent_blocks)
    assert "orchurator" in joined.lower()


@pytest.mark.asyncio
async def test_ask_why_question_falls_back_on_llm_failure(conn):
    class _BrokenProv:
        name = "anthropic"
        async def chat(self, **kwargs):
            raise RuntimeError("API down")

    providers = Providers(_BrokenProv(), None)
    q = await why.ask_why_question(
        url="https://x", title=None,
        settings=_settings(), providers=providers, conn=conn,
    )
    assert q == "why this one?"


# ---- handler integration: why flow ---------------------------------------

def _owner_update(msg_id: int, text: str, *, url_in_text: bool = False):
    u = MagicMock()
    u.effective_user = MagicMock(); u.effective_user.id = 42
    u.message = MagicMock()
    u.message.text = text
    u.message.message_id = msg_id
    u.message.forward_origin = None
    u.message.chat = MagicMock()
    u.message.chat.type = "private"
    u.message.chat.id = 99
    u.message.reply_text = AsyncMock()
    return u


@pytest.mark.asyncio
async def test_plain_text_reply_within_window_is_stored_as_why(conn):
    from bot.handlers import text_message_handler
    from bot import db as db_mod

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
                        WHY_WINDOW_MINUTES=10)

    # Pretend a URL capture already exists and pending_why is live
    parent_id = await db_mod.insert_capture(
        conn, kind="url", raw="https://ex.com", url="https://ex.com",
        source="article", dob=__import__("datetime").date(1990, 1, 1), tz_name="UTC",
    )
    assert parent_id is not None
    await why.set_pending(conn, parent_id=parent_id, window_minutes=10)

    update = _owner_update(msg_id=555, text="because the title caught me")
    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await text_message_handler(update, context)

    async with conn.execute(
        "SELECT kind, parent_id, raw, status FROM captures WHERE telegram_msg_id = ?", (555,)
    ) as cur:
        row = await cur.fetchone()
    assert row["kind"] == "why"
    assert row["parent_id"] == parent_id
    assert row["raw"] == "because the title caught me"
    # Whys don't need LLM ingest — they render inline in the parent. Insert
    # with status='processed' so the process_pending sweeper skips them.
    assert row["status"] == "processed"
    # Pending cleared
    assert await why.get_pending(conn) is None


@pytest.mark.asyncio
async def test_plain_text_reply_past_window_is_stored_as_text(conn):
    from bot.handlers import text_message_handler

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    # Expired pending
    expired = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('pending_why', ?, ?)",
        (json.dumps({"parent_id": 99, "deadline": expired}), "2000-01-01T00:00:00Z"),
    )
    await conn.commit()

    update = _owner_update(msg_id=556, text="a fresh line later")
    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await text_message_handler(update, context)

    async with conn.execute(
        "SELECT kind, parent_id FROM captures WHERE telegram_msg_id = ?", (556,)
    ) as cur:
        row = await cur.fetchone()
    assert row["kind"] == "text"
    assert row["parent_id"] is None


@pytest.mark.asyncio
async def test_url_save_schedules_why_question_and_sets_pending(conn):
    from bot.handlers import text_message_handler
    from bot.ingest.router import UrlScrapeResult

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
                        WHY_WINDOW_MINUTES=10, ANTHROPIC_API_KEY="k")

    class _Prov:
        name = "anthropic"
        async def chat(self, **kwargs):
            return LlmResponse(
                text="what in this caught you?", model="claude-sonnet-4-6", provider="anthropic",
                input_tokens=10, output_tokens=5,
            )
    providers = Providers(_Prov(), None)

    fake_scrape = UrlScrapeResult(
        source="article",
        payload={"title": "The Piece", "text": "body", "method": "readability"},
        content="The Piece\n\nbody",
    )

    update = _owner_update(msg_id=9100, text="https://example.com/a")
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    with patch("bot.handlers.scrape_url", AsyncMock(return_value=fake_scrape)):
        await text_message_handler(update, context)

    # Drain background tasks (why prompt + LLM processing)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        await t

    # Bot sent the "why" question
    context.bot.send_message.assert_awaited_once()
    call = context.bot.send_message.await_args
    assert call.kwargs["chat_id"] == 99
    assert "caught you" in call.kwargs["text"]

    # Pending state is live
    pending_row = await why.get_pending(conn)
    assert pending_row is not None
    assert pending_row.parent_id is not None


@pytest.mark.asyncio
async def test_url_save_extracts_title_from_nested_hn_payload(conn):
    """HN payloads nest title under 'story', not at top level. The why-question
    generator must still receive the real title.
    """
    from bot.handlers import text_message_handler
    from bot.ingest.router import UrlScrapeResult

    settings = Settings(
        TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC",
        WHY_WINDOW_MINUTES=10, ANTHROPIC_API_KEY="k",
    )

    seen_titles: list[str | None] = []

    class _Prov:
        name = "anthropic"
        async def chat(self, **kwargs):
            user_msg = kwargs["messages"][0].content
            seen_titles.append(user_msg)
            return LlmResponse(
                text="what in the thread made you stop?",
                model="claude-sonnet-4-6", provider="anthropic",
                input_tokens=10, output_tokens=5,
            )

    providers = Providers(_Prov(), None)

    hn_scrape = UrlScrapeResult(
        source="hn",
        payload={
            "story": {"id": 5, "title": "An HN Thread", "by": "pg"},
            "comments": [],
        },
        content="An HN Thread\n\n(story body)",
    )

    update = _owner_update(msg_id=9300, text="https://news.ycombinator.com/item?id=5")
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    with patch("bot.handlers.scrape_url", AsyncMock(return_value=hn_scrape)):
        await text_message_handler(update, context)

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        await t

    # Check the LLM received the HN title, not "(none)"
    joined = "\n".join(seen_titles)
    assert "An HN Thread" in joined
    # And the URL is withheld when we have a title, so the model can't quote it.
    assert "news.ycombinator.com" not in joined


@pytest.mark.asyncio
async def test_url_save_without_providers_does_not_ask_why(conn):
    from bot.handlers import text_message_handler
    from bot.ingest.router import UrlScrapeResult

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    fake_scrape = UrlScrapeResult(
        source="article", payload={"title": "x"}, content="x",
    )
    update = _owner_update(msg_id=9101, text="https://example.com/a")
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn}  # no providers

    with patch("bot.handlers.scrape_url", AsyncMock(return_value=fake_scrape)):
        await text_message_handler(update, context)

    context.bot.send_message.assert_not_awaited()
    assert await why.get_pending(conn) is None


@pytest.mark.asyncio
async def test_skip_handler_clears_pending(conn):
    from bot.handlers import skip_handler

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    await why.set_pending(conn, parent_id=1, window_minutes=10)
    assert await why.get_pending(conn) is not None

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await skip_handler(update, context)

    assert await why.get_pending(conn) is None
    update.message.reply_text.assert_awaited_once_with("skipped.")


@pytest.mark.asyncio
async def test_second_url_while_pending_why_does_not_consume_the_why(conn):
    """If the owner sends a new URL before answering the first 'why?', the
    new URL gets its own capture; the old pending-why is left alone
    (overwritten by the new URL's own why, once scheduled).
    """
    from bot.handlers import text_message_handler
    from bot.ingest.router import UrlScrapeResult
    from bot import db as db_mod

    settings = Settings(TELEGRAM_OWNER_ID=42, DOB="1990-01-01", TIMEZONE="UTC")

    first_parent = await db_mod.insert_capture(
        conn, kind="url", raw="https://a", url="https://a",
        source="article", dob=__import__("datetime").date(1990, 1, 1), tz_name="UTC",
    )
    await why.set_pending(conn, parent_id=first_parent, window_minutes=10)

    update = _owner_update(msg_id=9200, text="https://example.com/b")
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn}  # no providers → no new why scheduled

    with patch("bot.handlers.scrape_url",
               AsyncMock(return_value=UrlScrapeResult(source="article", payload={"title": "b"}, content="b"))):
        await text_message_handler(update, context)

    # The second message was NOT treated as a why-reply (it's a URL, different kind).
    async with conn.execute("SELECT kind, parent_id FROM captures WHERE telegram_msg_id = 9200") as cur:
        row = await cur.fetchone()
    assert row["kind"] == "url"
    assert row["parent_id"] is None
    # Original pending still present (not consumed by a URL message)
    assert await why.get_pending(conn) is not None
