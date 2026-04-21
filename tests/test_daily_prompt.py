from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import db as db_mod
from bot import reflection
from bot import scheduler as sched_mod
from bot.config import Settings
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        DAILY_PROMPT_LOCAL_TIME="21:30",
    )
    base.update(kw)
    return Settings(**base)


class _SpyProv:
    name = "anthropic"
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LlmResponse(
            text=self._text, model=kwargs["model"], provider="anthropic",
            input_tokens=20, output_tokens=10,
        )


@pytest.mark.asyncio
async def test_daily_prompt_skips_if_zero_captures_today(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    providers = Providers(_SpyProv("unused"), None)

    sent = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    assert sent is False
    bot.send_message.assert_not_awaited()
    # No daily row created for a skip
    async with conn.execute("SELECT COUNT(*) FROM daily") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_daily_prompt_sends_question_and_sets_prompted_at(conn):
    # Insert a capture for today so the prompt has material to work with.
    await db_mod.insert_capture(
        conn, kind="text", raw="the impediment to action advances action",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    prov = _SpyProv("what in that line still hums?")
    providers = Providers(prov, None)

    sent = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    assert sent is True

    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 42
    assert "still hums" in call.kwargs["text"]

    async with conn.execute(
        "SELECT prompt, prompted_at FROM daily WHERE local_date = ?",
        (datetime.now(timezone.utc).date().isoformat(),),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert "still hums" in row["prompt"]
    assert row["prompted_at"]


@pytest.mark.asyncio
async def test_daily_prompt_uses_orchurator_voice_block(conn):
    await db_mod.insert_capture(
        conn, kind="text", raw="a small ignition",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    prov = _SpyProv("q?")
    providers = Providers(prov, None)

    await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    assert prov.calls, "LLM was never called"
    sent_blocks = prov.calls[0]["system_blocks"]
    # VOICE_ORCHURATOR block must be present.
    joined = "\n\n".join(sent_blocks)
    assert "orchurator" in joined.lower()


@pytest.mark.asyncio
async def test_daily_prompt_is_idempotent_per_day(conn):
    """Running twice in the same day must only send one message."""
    await db_mod.insert_capture(
        conn, kind="text", raw="x", dob=date(1990, 1, 1), tz_name="UTC",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    providers = Providers(_SpyProv("q?"), None)

    first = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    second = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    assert first is True
    assert second is False
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_daily_prompt_force_bypasses_idempotent_check(conn):
    """/reflect calls with force=True should re-fire even when the day's
    scheduled prompt has already fired."""
    await db_mod.insert_capture(
        conn, kind="text", raw="x", dob=date(1990, 1, 1), tz_name="UTC",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    providers = Providers(_SpyProv("q?"), None)

    first = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    second = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot, force=True,
    )
    assert first is True
    assert second is True
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_daily_prompt_sets_pending_reflection(conn):
    await db_mod.insert_capture(
        conn, kind="text", raw="y", dob=date(1990, 1, 1), tz_name="UTC",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    providers = Providers(_SpyProv("q?"), None)

    await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    pending = await reflection.get_pending(conn)
    assert pending is not None
    assert pending.local_date == datetime.now(timezone.utc).date().isoformat()


@pytest.mark.asyncio
async def test_daily_prompt_does_not_persist_when_send_fails(conn):
    """If bot.send_message raises, the daily row and pending_reflection
    must NOT be persisted — otherwise the owner never sees the question
    but their next message would silently become a mystery reflection.
    """
    await db_mod.insert_capture(
        conn, kind="text", raw="a line", dob=date(1990, 1, 1), tz_name="UTC",
    )

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram 502"))
    providers = Providers(_SpyProv("q?"), None)

    sent = await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    assert sent is False

    # No daily row
    async with conn.execute("SELECT COUNT(*) FROM daily") as cur:
        row = await cur.fetchone()
    assert row[0] == 0

    # No pending_reflection
    assert await reflection.get_pending(conn) is None


@pytest.mark.asyncio
async def test_daily_prompt_skips_why_captures_from_context(conn):
    # Parent capture + a why child. Only the parent should be presented to
    # the daily LLM — the why is already attached to the parent.
    parent = await db_mod.insert_capture(
        conn, kind="url", url="https://ex.com", raw="https://ex.com",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    await db_mod.insert_capture(
        conn, kind="why", raw="because the structure caught me",
        parent_id=parent, telegram_msg_id=1,
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    bot = MagicMock(); bot.send_message = AsyncMock()
    prov = _SpyProv("q?")
    providers = Providers(prov, None)

    await sched_mod.daily_prompt_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    user_msg = prov.calls[0]["messages"][0].content
    assert "because the structure caught me" not in user_msg
    assert "https://ex.com" in user_msg


# ---- reply routing -------------------------------------------------------

def _owner_update(msg_id: int, text: str):
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
async def test_next_user_message_after_prompt_is_linked_as_reflection(conn):
    from bot.handlers import text_message_handler

    settings = _settings()
    # Create a daily row for today and set pending
    today = datetime.now(timezone.utc).date().isoformat()
    await conn.execute(
        "INSERT INTO daily (local_date, prompt, prompted_at) VALUES (?, ?, ?)",
        (today, "what stopped you today?", "2026-04-21T21:30:00Z"),
    )
    await conn.commit()
    await reflection.set_pending(conn, local_date=today, tz_name="UTC")

    update = _owner_update(msg_id=9400, text="the way light held the room")
    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn}

    await text_message_handler(update, context)

    # Reflection row stored
    async with conn.execute(
        "SELECT kind, raw FROM captures WHERE telegram_msg_id = ?", (9400,)
    ) as cur:
        row = await cur.fetchone()
    assert row["kind"] == "reflection"
    assert row["raw"] == "the way light held the room"

    # daily.reflection_capture_id set
    async with conn.execute(
        "SELECT reflection_capture_id FROM daily WHERE local_date = ?", (today,),
    ) as cur:
        row = await cur.fetchone()
    assert row["reflection_capture_id"] is not None

    # Pending cleared
    assert await reflection.get_pending(conn) is None


@pytest.mark.asyncio
async def test_voice_reply_is_linked_as_reflection(conn, monkeypatch):
    """A voice note sent while pending-reflection is live must be stored as
    a reflection (with the transcript as raw), not a plain voice capture.
    """
    from bot.handlers import voice_message_handler
    from bot.ingest import voice as voice_module

    settings = _settings()
    today = datetime.now(timezone.utc).date().isoformat()
    await conn.execute(
        "INSERT INTO daily (local_date, prompt, prompted_at) VALUES (?, ?, ?)",
        (today, "what held you today?", "2026-04-21T21:30:00Z"),
    )
    await conn.commit()
    await reflection.set_pending(conn, local_date=today, tz_name="UTC")

    async def _fake_transcribe(audio_bytes, *, filename, settings):
        return "the way the afternoon settled"
    monkeypatch.setattr(voice_module, "transcribe_voice_bytes", _fake_transcribe)

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
    update.message.chat.id = 99
    update.message.message_id = 9700
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await voice_message_handler(update, context)

    async with conn.execute(
        "SELECT kind, raw FROM captures WHERE telegram_msg_id = ?", (9700,)
    ) as cur:
        row = await cur.fetchone()
    assert row["kind"] == "reflection"
    assert row["raw"] == "the way the afternoon settled"

    # daily.reflection_capture_id set
    async with conn.execute(
        "SELECT reflection_capture_id FROM daily WHERE local_date = ?", (today,)
    ) as cur:
        drow = await cur.fetchone()
    assert drow["reflection_capture_id"] is not None


@pytest.mark.asyncio
async def test_why_takes_priority_over_reflection(conn):
    """If both pending states are live (edge: URL saved right before daily
    fires), the next reply routes to the why — it's more specific."""
    from bot import why as why_mod
    from bot.handlers import text_message_handler

    settings = _settings()

    parent_id = await db_mod.insert_capture(
        conn, kind="url", url="https://ex.com", raw="https://ex.com",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    await why_mod.set_pending(conn, parent_id=parent_id, window_minutes=10)
    today = datetime.now(timezone.utc).date().isoformat()
    await reflection.set_pending(conn, local_date=today, tz_name="UTC")

    update = _owner_update(msg_id=9500, text="because the title caught me")
    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await text_message_handler(update, context)

    async with conn.execute(
        "SELECT kind, parent_id FROM captures WHERE telegram_msg_id = ?", (9500,)
    ) as cur:
        row = await cur.fetchone()
    # Went to the why, not the reflection
    assert row["kind"] == "why"
    assert row["parent_id"] == parent_id
    # Reflection pending remains (not yet consumed)
    assert await reflection.get_pending(conn) is not None


@pytest.mark.asyncio
async def test_skip_handler_clears_both_pending_states(conn):
    from bot import why as why_mod
    from bot.handlers import skip_handler

    settings = _settings()
    await why_mod.set_pending(conn, parent_id=1, window_minutes=10)
    await reflection.set_pending(conn, local_date="2026-04-21", tz_name="UTC")

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"settings": settings, "db": conn}

    await skip_handler(update, context)

    assert await why_mod.get_pending(conn) is None
    assert await reflection.get_pending(conn) is None
    update.message.reply_text.assert_awaited_once_with("skipped.")


@pytest.mark.asyncio
async def test_reflect_handler_fires_daily_prompt_job(conn):
    from bot.handlers import reflect_handler

    await db_mod.insert_capture(
        conn, kind="text", raw="a small line",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    settings = _settings()
    providers = Providers(_SpyProv("what caught you?"), None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {
        "settings": settings, "db": conn, "providers": providers,
    }

    await reflect_handler(update, context)
    context.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reflect_handler_tells_user_when_nothing_to_reflect_on(conn):
    from bot.handlers import reflect_handler

    settings = _settings()
    providers = Providers(_SpyProv("q?"), None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {
        "settings": settings, "db": conn, "providers": providers,
    }

    await reflect_handler(update, context)
    update.message.reply_text.assert_awaited_once_with(
        "nothing to reflect on yet today."
    )


# ---- scheduler structure --------------------------------------------------

def test_build_scheduler_registers_daily_prompt_when_bot_provided(conn):
    providers = Providers(_SpyProv(""), None)
    bot = MagicMock()
    scheduler = sched_mod.build_scheduler(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
    )
    ids = {j.id for j in scheduler.get_jobs()}
    assert ids == {"process_pending", "nightly_sync", "daily_prompt", "weekly_digest"}


def test_build_scheduler_skips_weekly_digest_when_disabled(conn):
    """With WEEKLY_DIGEST_ENABLED=false the cron is not registered.
    Daily prompt + housekeeping jobs are unaffected.
    """
    providers = Providers(_SpyProv(""), None)
    bot = MagicMock()
    scheduler = sched_mod.build_scheduler(
        conn=conn,
        settings=_settings(WEEKLY_DIGEST_ENABLED=False),
        providers=providers, bot=bot,
    )
    ids = {j.id for j in scheduler.get_jobs()}
    assert "weekly_digest" not in ids
    assert "daily_prompt" in ids  # other scheduled jobs still present
    assert "process_pending" in ids
    assert "nightly_sync" in ids


def test_build_scheduler_skips_daily_prompt_without_bot(conn):
    providers = Providers(_SpyProv(""), None)
    scheduler = sched_mod.build_scheduler(
        conn=conn, settings=_settings(), providers=providers,
    )
    ids = {j.id for j in scheduler.get_jobs()}
    assert "daily_prompt" not in ids


@pytest.mark.asyncio
async def test_scheduler_survives_restart(conn):
    """Rebuild the scheduler (simulating a restart) and confirm the
    daily_prompt job is registered with a future fire time."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    providers = Providers(_SpyProv(""), None)
    bot = MagicMock()

    sched1 = sched_mod.build_scheduler(
        conn=conn, settings=_settings(DAILY_PROMPT_LOCAL_TIME="23:59"),
        providers=providers, bot=bot,
    )
    sched1.start(paused=True)
    sched2 = sched_mod.build_scheduler(
        conn=conn, settings=_settings(DAILY_PROMPT_LOCAL_TIME="23:59"),
        providers=providers, bot=bot,
    )
    sched2.start(paused=True)
    try:
        job = sched2.get_job("daily_prompt")
        assert job is not None
        # Trigger re-computes next fire time on each scheduler boot.
        assert job.next_run_time is not None
    finally:
        sched1.shutdown(wait=False)
        sched2.shutdown(wait=False)


@pytest.mark.asyncio
async def test_drain_on_boot_runs_daily_prompt_when_past_scheduled_time(conn):
    """Drain fires daily_prompt only if we're past today's scheduled time.
    Using 00:00 ensures we're always past it regardless of when the test runs.
    """
    providers = Providers(_SpyProv("q?"), None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    with patch("bot.scheduler.process_pending", AsyncMock(return_value=0)) as pp, \
         patch("bot.scheduler.nightly_sync", AsyncMock(return_value=0)) as ns, \
         patch("bot.scheduler.daily_prompt_job", AsyncMock(return_value=True)) as dp:
        await sched_mod.drain_on_boot(
            conn=conn,
            settings=_settings(DAILY_PROMPT_LOCAL_TIME="00:00"),
            providers=providers, bot=bot,
        )
    pp.assert_awaited_once()
    ns.assert_awaited_once()
    dp.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_on_boot_skips_daily_prompt_before_scheduled_time(conn):
    """A daytime restart (well before the evening prompt time) must NOT fire
    the daily prompt early. The scheduler will handle it at the normal time.
    """
    providers = Providers(_SpyProv("q?"), None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    # Force the time-gate to return False regardless of when tests run.
    with patch("bot.scheduler._is_past_daily_time_today", return_value=False), \
         patch("bot.scheduler.process_pending", AsyncMock(return_value=0)), \
         patch("bot.scheduler.nightly_sync", AsyncMock(return_value=0)), \
         patch("bot.scheduler.daily_prompt_job", AsyncMock(return_value=True)) as dp:
        await sched_mod.drain_on_boot(
            conn=conn, settings=_settings(), providers=providers, bot=bot,
        )
    dp.assert_not_awaited()


def test_is_past_daily_time_today_handles_malformed_time():
    from bot.scheduler import _is_past_daily_time_today
    # Pydantic would reject obviously broken input at startup, but the helper
    # should be defensive against weird values rather than crashing drain.
    settings = _settings(DAILY_PROMPT_LOCAL_TIME="not-a-time")
    assert _is_past_daily_time_today(settings) is False
