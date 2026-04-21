from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import db as db_mod
from bot import forget
from bot.config import Settings


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        GITHUB_TOKEN="ghp_test", GITHUB_REPO="u/r",
    )
    base.update(kw)
    return Settings(**base)


async def _insert(conn, **kw):
    defaults = dict(
        kind="text", source="telegram", raw="a line",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    defaults.update(kw)
    return await db_mod.insert_capture(conn, **defaults)


# ---- find_most_recent_id -------------------------------------------------

@pytest.mark.asyncio
async def test_find_most_recent_id_returns_last_inserted(conn):
    assert await forget.find_most_recent_id(conn) is None
    await _insert(conn, raw="first", telegram_msg_id=1)
    second = await _insert(conn, raw="second", telegram_msg_id=2)
    assert await forget.find_most_recent_id(conn) == second


# ---- primary-capture forget --------------------------------------------

@pytest.mark.asyncio
async def test_forget_nonexistent_returns_none(conn):
    result = await forget.forget_capture(conn, 9999, settings=_settings())
    assert result is None


@pytest.mark.asyncio
async def test_forget_primary_removes_row_from_sqlite(conn):
    cid = await _insert(conn, raw="bye", telegram_msg_id=1)
    assert cid is not None
    # No github_sha → no GitHub call
    with patch("bot.github_sync.delete_file", AsyncMock(return_value=True)) as mock:
        result = await forget.forget_capture(
            conn, cid, settings=_settings(GITHUB_TOKEN="", GITHUB_REPO=""),
        )
    assert result is not None
    assert result["id"] == cid
    assert await db_mod.count_captures(conn) == 0
    mock.assert_not_awaited()  # no github config → no call


@pytest.mark.asyncio
async def test_forget_primary_calls_github_delete_when_configured(conn):
    cid = await _insert(conn, raw="bye", telegram_msg_id=1)
    await conn.execute(
        "UPDATE captures SET github_sha = 'abc' WHERE id = ?", (cid,),
    )
    await conn.commit()

    with patch("bot.github_sync.delete_file",
               AsyncMock(return_value=True)) as mock:
        result = await forget.forget_capture(conn, cid, settings=_settings())
    assert result["github_deleted"] is True
    mock.assert_awaited_once()
    call_kwargs = mock.await_args.kwargs
    assert call_kwargs["sha"] == "abc"
    assert call_kwargs["path"].endswith(".md")


@pytest.mark.asyncio
async def test_forget_primary_cascades_to_whys(conn):
    parent = await _insert(conn, kind="url", url="https://ex.com", telegram_msg_id=1)
    why_a = await _insert(
        conn, kind="why", raw="first why", parent_id=parent, telegram_msg_id=2,
        status="processed",
    )
    why_b = await _insert(
        conn, kind="why", raw="second why", parent_id=parent, telegram_msg_id=3,
        status="processed",
    )
    assert await db_mod.count_captures(conn) == 3

    with patch("bot.github_sync.delete_file", AsyncMock(return_value=True)):
        result = await forget.forget_capture(conn, parent, settings=_settings())

    assert set(result["cascaded_whys"]) == {why_a, why_b}
    assert await db_mod.count_captures(conn) == 0


@pytest.mark.asyncio
async def test_forget_primary_clears_daily_reflection_ref(conn):
    reflection_id = await _insert(conn, kind="reflection", raw="the way light", telegram_msg_id=1)
    # Pin it to a daily row
    await conn.execute(
        "INSERT INTO daily (local_date, prompt, prompted_at, reflection_capture_id) "
        "VALUES ('2026-04-21', 'q', '2026-04-21T21:30:00Z', ?)",
        (reflection_id,),
    )
    await conn.commit()

    with patch("bot.github_sync.delete_file", AsyncMock(return_value=True)):
        await forget.forget_capture(
            conn, reflection_id, settings=_settings(GITHUB_TOKEN="", GITHUB_REPO=""),
        )

    async with conn.execute(
        "SELECT reflection_capture_id FROM daily WHERE local_date = '2026-04-21'"
    ) as cur:
        row = await cur.fetchone()
    assert row["reflection_capture_id"] is None


@pytest.mark.asyncio
async def test_forget_survives_github_failure(conn):
    """GitHub API failure during delete must NOT prevent the SQLite delete —
    the user asked to forget it, so forget it. Log and move on."""
    cid = await _insert(conn, raw="x", telegram_msg_id=1)
    await conn.execute(
        "UPDATE captures SET github_sha = 'stale' WHERE id = ?", (cid,),
    )
    await conn.commit()

    with patch("bot.github_sync.delete_file",
               AsyncMock(side_effect=RuntimeError("409 conflict"))):
        result = await forget.forget_capture(conn, cid, settings=_settings())
    assert result is not None
    assert result["github_deleted"] is False
    assert await db_mod.count_captures(conn) == 0


# ---- why-specific forget -------------------------------------------------

@pytest.mark.asyncio
async def test_forget_why_keeps_parent_and_siblings(conn):
    parent = await _insert(conn, kind="url", url="https://ex.com", telegram_msg_id=1)
    await conn.execute(
        "UPDATE captures SET github_sha = 'parent-sha' WHERE id = ?", (parent,),
    )
    await conn.commit()

    why_a = await _insert(
        conn, kind="why", raw="keep me", parent_id=parent, telegram_msg_id=2,
        status="processed",
    )
    why_b = await _insert(
        conn, kind="why", raw="remove me", parent_id=parent, telegram_msg_id=3,
        status="processed",
    )

    with patch("bot.github_sync.put_file",
               AsyncMock(return_value="new-parent-sha")) as mock_put:
        result = await forget.forget_capture(conn, why_b, settings=_settings())

    assert result["id"] == why_b
    mock_put.assert_awaited_once()
    # Parent's file was re-rendered with the remaining why
    content = mock_put.await_args.kwargs["content"]
    assert "keep me" in content
    assert "remove me" not in content

    # SQLite: parent + other why still present
    ids = set()
    async with conn.execute("SELECT id FROM captures") as cur:
        async for row in cur:
            ids.add(int(row[0]))
    assert parent in ids
    assert why_a in ids
    assert why_b not in ids

    # Parent + surviving sibling share the new sha
    async with conn.execute(
        "SELECT github_sha FROM captures WHERE id IN (?, ?)", (parent, why_a),
    ) as cur:
        shas = {r[0] for r in await cur.fetchall()}
    assert shas == {"new-parent-sha"}


@pytest.mark.asyncio
async def test_forget_orphan_why_just_drops_row(conn):
    """A why whose parent is somehow missing (or was deleted first) should
    still be deletable — no file to re-render."""
    why_id = await _insert(
        conn, kind="why", raw="orphan", parent_id=None, telegram_msg_id=1,
        status="processed",
    )
    with patch("bot.github_sync.put_file", AsyncMock()) as mock:
        result = await forget.forget_capture(conn, why_id, settings=_settings())
    assert result["id"] == why_id
    mock.assert_not_awaited()
    assert await db_mod.count_captures(conn) == 0


# ---- handler integration ------------------------------------------------

@pytest.mark.asyncio
async def test_forget_handler_last(conn):
    from bot.handlers import forget_handler

    settings = _settings()
    await _insert(conn, raw="first", telegram_msg_id=1)
    second = await _insert(conn, raw="second", telegram_msg_id=2)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["last"]
    context.bot_data = {"settings": settings, "db": conn}

    with patch("bot.github_sync.delete_file", AsyncMock(return_value=True)):
        await forget_handler(update, context)

    async with conn.execute("SELECT id FROM captures") as cur:
        remaining = [int(r[0]) for r in await cur.fetchall()]
    assert second not in remaining
    update.message.reply_text.assert_awaited_once()
    msg = update.message.reply_text.await_args.args[0]
    assert "forgotten" in msg.lower()


@pytest.mark.asyncio
async def test_forget_handler_explicit_id(conn):
    from bot.handlers import forget_handler
    settings = _settings()
    cid = await _insert(conn, raw="bye", telegram_msg_id=1)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = [str(cid)]
    context.bot_data = {"settings": settings, "db": conn}

    with patch("bot.github_sync.delete_file", AsyncMock(return_value=True)):
        await forget_handler(update, context)

    assert await db_mod.count_captures(conn) == 0


@pytest.mark.asyncio
async def test_forget_handler_usage_on_empty_args(conn):
    from bot.handlers import forget_handler
    settings = _settings()
    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock(); context.args = []
    context.bot_data = {"settings": settings, "db": conn}
    await forget_handler(update, context)
    msg = update.message.reply_text.await_args.args[0]
    assert "usage" in msg.lower()
    assert "last" in msg


@pytest.mark.asyncio
async def test_forget_handler_not_found(conn):
    from bot.handlers import forget_handler
    settings = _settings()
    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()
    context = MagicMock(); context.args = ["9999"]
    context.bot_data = {"settings": settings, "db": conn}
    await forget_handler(update, context)
    msg = update.message.reply_text.await_args.args[0]
    assert "not found" in msg.lower()
