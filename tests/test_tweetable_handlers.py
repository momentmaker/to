import json

import pytest
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from bot import handlers
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


def _update(text, owner_id=1):
    u = MagicMock()
    u.effective_user.id = owner_id
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def _ctx(*, conn, settings):
    c = MagicMock()
    c.bot_data = {
        "conn": conn, "db": conn,
        "settings": settings,
        "providers": MagicMock(),
    }
    return c


async def _add_capture(conn, *, payload=None):
    await conn.execute(
        """
        INSERT INTO captures (kind, raw, payload, created_at, local_date,
                              iso_week_key, fz_week_idx, status, github_sha)
        VALUES ('text', 'body', ?, '2026-05-01T12:00:00Z', '2026-05-01',
                '2026-W18', 1900, 'done', 'abc123')
        """,
        (json.dumps(payload or {}),),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_tweetable_last_sets_flag(monkeypatch):
    settings = fake_settings(
        TELEGRAM_OWNER_ID=1, GITHUB_TOKEN="t", GITHUB_REPO="x/y",
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn)

        async def fake_push(capture_id, *, settings, conn, client=None):
            return True

        monkeypatch.setattr(
            "bot.handlers.github_sync.push_capture", fake_push,
        )

        await handlers.tweetable_handler(
            _update("/tweetable last"),
            _ctx(conn=conn, settings=settings),
        )
        async with conn.execute("SELECT payload FROM captures") as cur:
            row = await cur.fetchone()
        assert json.loads(row[0]).get("tweetable") is True


@pytest.mark.asyncio
async def test_tweetable_by_id(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn)
        await _add_capture(conn)

        async def fake_push(*args, **kwargs):
            return True

        monkeypatch.setattr(
            "bot.handlers.github_sync.push_capture", fake_push,
        )

        await handlers.tweetable_handler(
            _update("/tweetable 2"),
            _ctx(conn=conn, settings=settings),
        )
        async with conn.execute(
            "SELECT id, payload FROM captures ORDER BY id"
        ) as cur:
            rows = list(await cur.fetchall())
        assert json.loads(rows[0]["payload"]).get("tweetable") is None
        assert json.loads(rows[1]["payload"]).get("tweetable") is True


@pytest.mark.asyncio
async def test_untweetable_clears_flag(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, payload={"tweetable": True})

        async def fake_push(*args, **kwargs):
            return True

        monkeypatch.setattr(
            "bot.handlers.github_sync.push_capture", fake_push,
        )

        await handlers.untweetable_handler(
            _update("/untweetable last"),
            _ctx(conn=conn, settings=settings),
        )
        async with conn.execute("SELECT payload FROM captures") as cur:
            row = await cur.fetchone()
        assert json.loads(row[0]).get("tweetable") is False


@pytest.mark.asyncio
async def test_tweetable_unknown_id_replies():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _update("/tweetable 999")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.tweetable_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        msg = update.message.reply_text.call_args.args[0]
        # No capture exists at all → "last" returns None → "no such".
        assert "no such" in msg.lower() or "999" in msg


@pytest.mark.asyncio
async def test_tweetable_no_arg_shows_usage():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _update("/tweetable")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.tweetable_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "usage" in update.message.reply_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_tweetable_preserves_github_sha(monkeypatch):
    """Regression: setting tweetable must NOT clear github_sha. Doing so
    forces the next push to be treated as a create, which GitHub rejects
    with 422 because the file already exists.
    """
    settings = fake_settings(
        TELEGRAM_OWNER_ID=1, GITHUB_TOKEN="t", GITHUB_REPO="x/y",
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn)  # github_sha='abc123'

        captured: dict = {}

        async def fake_push(capture_id, *, settings, conn, client=None):
            async with conn.execute(
                "SELECT github_sha FROM captures WHERE id = ?", (capture_id,),
            ) as cur:
                row = await cur.fetchone()
            captured["sha_at_push"] = row[0]
            return True

        monkeypatch.setattr(
            "bot.handlers.github_sync.push_capture", fake_push,
        )

        await handlers.tweetable_handler(
            _update("/tweetable last"),
            _ctx(conn=conn, settings=settings),
        )
        # The sha row must still hold its prior value when push_capture runs.
        assert captured["sha_at_push"] == "abc123"


@pytest.mark.asyncio
async def test_tweetable_last_advances_on_subsequent_calls(monkeypatch):
    """`/tweetable last` should pick the most-recent UN-flagged capture.
    Calling it twice in a row must flag two different captures, not the
    same one twice. Regression: original implementation always picked the
    most recent capture regardless of tweetable state.
    """
    settings = fake_settings(
        TELEGRAM_OWNER_ID=1, GITHUB_TOKEN="t", GITHUB_REPO="x/y",
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # Three un-flagged captures, ascending id (3 is most recent).
        for _ in range(3):
            await _add_capture(conn)

        async def fake_push(*args, **kwargs):
            return True

        monkeypatch.setattr(
            "bot.handlers.github_sync.push_capture", fake_push,
        )

        ctx = _ctx(conn=conn, settings=settings)
        await handlers.tweetable_handler(_update("/tweetable last"), ctx)
        await handlers.tweetable_handler(_update("/tweetable last"), ctx)

        async with conn.execute(
            "SELECT id, payload FROM captures ORDER BY id"
        ) as cur:
            rows = list(await cur.fetchall())
        flags = [json.loads(r["payload"]).get("tweetable") for r in rows]
        # Captures 2 and 3 (the most recent two) flagged; capture 1 not.
        assert flags == [None, True, True]


@pytest.mark.asyncio
async def test_tweetable_last_replies_when_all_flagged(monkeypatch):
    """When every recent capture is already flagged, /tweetable last
    should give a clear message instead of pretending to flag again."""
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, payload={"tweetable": True})

        update = _update("/tweetable last")
        ctx = _ctx(conn=conn, settings=settings)
        await handlers.tweetable_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        msg = update.message.reply_text.call_args.args[0].lower()
        assert "already flagged" in msg


@pytest.mark.asyncio
async def test_untweetable_last_picks_most_recent_flagged(monkeypatch):
    """`/untweetable last` should target the most-recent FLAGGED capture,
    not just the most recent capture (which might already be un-flagged)."""
    settings = fake_settings(
        TELEGRAM_OWNER_ID=1, GITHUB_TOKEN="t", GITHUB_REPO="x/y",
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # id=1 flagged, id=2 unflagged. /untweetable last should hit 1.
        await _add_capture(conn, payload={"tweetable": True})
        await _add_capture(conn, payload={})

        async def fake_push(*args, **kwargs):
            return True

        monkeypatch.setattr(
            "bot.handlers.github_sync.push_capture", fake_push,
        )

        await handlers.untweetable_handler(
            _update("/untweetable last"),
            _ctx(conn=conn, settings=settings),
        )
        async with conn.execute(
            "SELECT id, payload FROM captures ORDER BY id"
        ) as cur:
            rows = list(await cur.fetchall())
        # id=1 was flagged → now cleared; id=2 still untouched.
        assert json.loads(rows[0]["payload"]).get("tweetable") is False
        assert json.loads(rows[1]["payload"]).get("tweetable") is None
