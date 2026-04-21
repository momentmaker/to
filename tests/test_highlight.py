"""Tests for /highlight — inline child captures attached to a parent."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import db as db_mod
from bot import forget
from bot.config import Settings
from bot.markdown_out import render_capture_markdown


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
    )
    base.update(kw)
    return Settings(**base)


async def _insert(conn, **kw):
    defaults = dict(
        kind="text", source="telegram", raw="original",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    defaults.update(kw)
    return await db_mod.insert_capture(conn, **defaults)


def _mock_update(*, text: str, reply_to_msg_id: int | None, message_id: int = 999):
    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.chat = MagicMock(); update.message.chat.type = "private"
    update.message.message_id = message_id
    update.message.reply_text = AsyncMock()
    if reply_to_msg_id is None:
        update.message.reply_to_message = None
    else:
        reply_msg = MagicMock()
        reply_msg.message_id = reply_to_msg_id
        update.message.reply_to_message = reply_msg
    return update


def _context(conn, args, settings=None):
    ctx = MagicMock()
    ctx.bot_data = {"settings": settings or _settings(), "db": conn}
    ctx.args = args
    return ctx


# ---- handler rejections ---------------------------------------------------

@pytest.mark.asyncio
async def test_highlight_rejects_without_reply(conn):
    from bot.handlers import highlight_handler
    update = _mock_update(text="x", reply_to_msg_id=None)
    await highlight_handler(update, _context(conn, ["some", "text"]))
    msg = update.message.reply_text.await_args.args[0]
    assert "reply" in msg.lower()
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_highlight_rejects_without_text(conn):
    from bot.handlers import highlight_handler
    await _insert(conn, telegram_msg_id=111)
    update = _mock_update(text="x", reply_to_msg_id=111)
    await highlight_handler(update, _context(conn, []))
    msg = update.message.reply_text.await_args.args[0]
    assert "usage" in msg.lower()
    async with conn.execute("SELECT COUNT(*) FROM captures WHERE kind='highlight'") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_highlight_rejects_when_parent_unknown(conn):
    from bot.handlers import highlight_handler
    update = _mock_update(text="x", reply_to_msg_id=555)  # nothing stored for msg 555
    await highlight_handler(update, _context(conn, ["the", "line"]))
    msg = update.message.reply_text.await_args.args[0]
    assert "couldn't find" in msg.lower() or "not" in msg.lower()
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


# ---- happy path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_highlight_happy_path_creates_child(conn):
    from bot.handlers import highlight_handler
    parent_id = await _insert(
        conn, kind="pdf", raw="long body", telegram_msg_id=777,
    )
    assert parent_id is not None
    update = _mock_update(text="x", reply_to_msg_id=777, message_id=778)

    # github not configured → no async push
    settings = _settings()  # GITHUB_TOKEN absent
    await highlight_handler(update, _context(conn, ["the", "sharpest", "line"], settings))

    async with conn.execute(
        "SELECT id, kind, raw, parent_id, telegram_msg_id "
        "FROM captures WHERE kind='highlight'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["kind"] == "highlight"
    assert row["raw"] == "the sharpest line"
    assert row["parent_id"] == parent_id
    assert row["telegram_msg_id"] == 778

    reply = update.message.reply_text.await_args.args[0]
    assert "highlight" in reply.lower()
    assert str(parent_id) in reply


@pytest.mark.asyncio
async def test_highlight_joins_multi_word_args_verbatim(conn):
    from bot.handlers import highlight_handler
    parent_id = await _insert(conn, telegram_msg_id=42)
    update = _mock_update(text="x", reply_to_msg_id=42)
    await highlight_handler(
        update, _context(conn, ["what", "we", "could", "not", "name"]),
    )
    async with conn.execute(
        "SELECT raw FROM captures WHERE kind='highlight'",
    ) as cur:
        row = await cur.fetchone()
    assert row["raw"] == "what we could not name"


# ---- markdown rendering --------------------------------------------------

def test_render_includes_highlights_under_their_own_heading():
    parent = {
        "id": 10, "kind": "text", "source": "telegram", "url": None,
        "parent_id": None, "telegram_msg_id": 1,
        "created_at": "2026-04-21T10:00:00Z", "local_date": "2026-04-21",
        "iso_week_key": "2026-W17", "fz_week_idx": 1888,
        "processed": json.dumps({
            "title": "t", "tags": [], "quotes": [], "summary": "",
        }),
        "raw": "a parent line",
    }
    hl_a = {
        "id": 20, "kind": "highlight", "parent_id": 10,
        "created_at": "2026-04-21T10:05:00Z",
        "raw": "the first highlight",
    }
    hl_b = {
        "id": 21, "kind": "highlight", "parent_id": 10,
        "created_at": "2026-04-21T10:06:00Z",
        "raw": "the second highlight",
    }
    md = render_capture_markdown(parent, highlight_children=[hl_a, hl_b])
    assert "## highlights" in md
    assert "the first highlight" in md
    assert "the second highlight" in md
    # Position check: under its own heading, not under ## why?
    assert "## why?" not in md


def test_render_keeps_why_and_highlight_sections_distinct():
    parent = {
        "id": 10, "kind": "text", "source": "telegram", "url": None,
        "parent_id": None, "telegram_msg_id": 1,
        "created_at": "2026-04-21T10:00:00Z", "local_date": "2026-04-21",
        "iso_week_key": "2026-W17", "fz_week_idx": 1888,
        "processed": json.dumps({
            "title": "", "tags": [], "quotes": [], "summary": "",
        }),
        "raw": "parent",
    }
    why = {
        "id": 11, "kind": "why", "parent_id": 10,
        "created_at": "2026-04-21T10:01:00Z",
        "raw": "because",
    }
    hl = {
        "id": 12, "kind": "highlight", "parent_id": 10,
        "created_at": "2026-04-21T10:02:00Z",
        "raw": "the line",
    }
    md = render_capture_markdown(parent, why_children=[why], highlight_children=[hl])
    # Both sections appear, in order: why then highlights
    why_pos = md.index("## why?")
    hl_pos = md.index("## highlights")
    assert why_pos < hl_pos
    assert md.index("because") < hl_pos
    assert md.index("the line") > hl_pos


# ---- forget cascade -------------------------------------------------------

@pytest.mark.asyncio
async def test_forget_primary_cascades_highlights(conn):
    parent = await _insert(conn, kind="text", raw="parent", telegram_msg_id=1)
    hl_a = await _insert(
        conn, kind="highlight", raw="a", parent_id=parent, telegram_msg_id=2,
        status="processed",
    )
    hl_b = await _insert(
        conn, kind="highlight", raw="b", parent_id=parent, telegram_msg_id=3,
        status="processed",
    )
    assert parent is not None and hl_a is not None and hl_b is not None

    with patch("bot.github_sync.delete_file", AsyncMock(return_value=True)):
        result = await forget.forget_capture(
            conn, parent,
            settings=_settings(GITHUB_TOKEN="ghp", GITHUB_REPO="u/r"),
        )
    assert result is not None
    assert set(result["cascaded_children"]) == {hl_a, hl_b}
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        (n,) = await cur.fetchone()
    assert n == 0


@pytest.mark.asyncio
async def test_forget_highlight_keeps_parent_and_other_siblings(conn):
    parent = await _insert(conn, kind="text", raw="parent", telegram_msg_id=1)
    why = await _insert(
        conn, kind="why", raw="because", parent_id=parent, telegram_msg_id=2,
        status="processed",
    )
    hl_a = await _insert(
        conn, kind="highlight", raw="a", parent_id=parent, telegram_msg_id=3,
        status="processed",
    )
    hl_b = await _insert(
        conn, kind="highlight", raw="b", parent_id=parent, telegram_msg_id=4,
        status="processed",
    )

    # No github_sha on parent → no rewrite call; still deletes SQLite row.
    result = await forget.forget_capture(conn, hl_a, settings=_settings())
    assert result is not None
    assert result["id"] == hl_a
    assert result["kind"] == "highlight"

    async with conn.execute(
        "SELECT id FROM captures ORDER BY id",
    ) as cur:
        remaining = [r[0] for r in await cur.fetchall()]
    assert remaining == [parent, why, hl_b]
