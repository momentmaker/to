from __future__ import annotations

import base64
import json
from datetime import date

import httpx
import pytest

from bot import db as db_mod
from bot import github_sync
from bot.config import Settings


def _settings(**kw) -> Settings:
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        GITHUB_TOKEN="ghp_test", GITHUB_REPO="user/to-commonplace",
        GITHUB_BRANCH="main",
    )
    base.update(kw)
    return Settings(**base)


async def _insert(conn, **kw):
    defaults = dict(
        kind="text", source="telegram", raw="a small thing",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    defaults.update(kw)
    return await db_mod.insert_capture(conn, **defaults)


# ---- put_file low-level ---------------------------------------------------

@pytest.mark.asyncio
async def test_put_file_success_returns_sha():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization", "")
        body = json.loads(request.content.decode())
        captured["body"] = body
        return httpx.Response(201, json={"content": {"sha": "abc123", "path": "test.md"}})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sha = await github_sync.put_file(
            settings=_settings(),
            path="2026-w16/x.md",
            content="hello",
            message="test",
            client=client,
        )
    assert sha == "abc123"
    assert captured["method"] == "PUT"
    assert "/repos/user/to-commonplace/contents/2026-w16/x.md" in captured["url"]
    assert captured["auth"] == "Bearer ghp_test"
    # Content is base64-encoded
    decoded = base64.b64decode(captured["body"]["content"]).decode()
    assert decoded == "hello"
    assert captured["body"]["branch"] == "main"
    # sha absent on create
    assert "sha" not in captured["body"]


@pytest.mark.asyncio
async def test_put_file_passes_sha_on_update():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"content": {"sha": "newsha"}})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sha = await github_sync.put_file(
            settings=_settings(), path="p.md", content="v2",
            message="update", existing_sha="oldsha", client=client,
        )
    assert sha == "newsha"
    assert captured["body"]["sha"] == "oldsha"


@pytest.mark.asyncio
async def test_github_sync_retries_on_5xx():
    attempts = []

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            return httpx.Response(503, json={"message": "service unavailable"})
        return httpx.Response(201, json={"content": {"sha": "after-retry"}})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Patch the backoff sleep to make the test fast
        import bot.github_sync as gs
        original = gs._BACKOFF_BASE_S
        gs._BACKOFF_BASE_S = 0.0
        try:
            sha = await github_sync.put_file(
                settings=_settings(), path="p.md", content="x",
                message="m", client=client,
            )
        finally:
            gs._BACKOFF_BASE_S = original

    assert sha == "after-retry"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_github_sync_gives_up_after_max_retries():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        import bot.github_sync as gs
        gs._BACKOFF_BASE_S = 0.0
        with pytest.raises(RuntimeError, match="after"):
            await github_sync.put_file(
                settings=_settings(), path="p.md", content="x",
                message="m", client=client,
            )


@pytest.mark.asyncio
async def test_github_sync_does_not_retry_on_4xx():
    attempts = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts[0] += 1
        return httpx.Response(422, json={"message": "sha mismatch"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await github_sync.put_file(
                settings=_settings(), path="p.md", content="x",
                message="m", client=client,
            )
    # 422 is a client error and should NOT retry — fail fast so caller can
    # investigate the sha conflict.
    assert attempts[0] == 1


# ---- push_capture (DB integration) ---------------------------------------

@pytest.mark.asyncio
async def test_push_capture_sets_sha_on_success(conn):
    from unittest.mock import AsyncMock, patch

    cid = await _insert(
        conn, kind="text",
        processed={"title": "t", "tags": ["a"], "quotes": [], "summary": "s"},
    )
    assert cid is not None
    # Mark it processed so the serialized shape is complete
    await conn.execute(
        "UPDATE captures SET processed = ?, status = 'processed' WHERE id = ?",
        (json.dumps({"title": "t", "tags": ["a"], "quotes": [], "summary": "s"}), cid),
    )
    await conn.commit()

    with patch("bot.github_sync.put_file", AsyncMock(return_value="pushed-sha")):
        ok = await github_sync.push_capture(cid, settings=_settings(), conn=conn)

    assert ok is True
    async with conn.execute("SELECT github_sha FROM captures WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["github_sha"] == "pushed-sha"


@pytest.mark.asyncio
async def test_push_capture_skips_when_not_configured(conn):
    cid = await _insert(conn)
    settings = _settings(GITHUB_TOKEN="", GITHUB_REPO="")
    ok = await github_sync.push_capture(cid, settings=settings, conn=conn)
    assert ok is False


@pytest.mark.asyncio
async def test_push_capture_for_why_requires_synced_parent(conn):
    from unittest.mock import AsyncMock, patch

    parent_id = await _insert(conn, kind="url", url="https://x.com")
    # parent NOT yet synced (github_sha is NULL)
    why_id = await _insert(
        conn, kind="why", raw="because", parent_id=parent_id, telegram_msg_id=5,
    )

    put = AsyncMock(return_value="new-sha")
    with patch("bot.github_sync.put_file", put):
        ok = await github_sync.push_capture(why_id, settings=_settings(), conn=conn)
    assert ok is False  # deferred to nightly_sync
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_capture_for_why_updates_parent_file_inline(conn):
    from unittest.mock import AsyncMock, patch

    parent_id = await _insert(conn, kind="url", url="https://x.com")
    # Pretend parent is already synced
    await conn.execute(
        "UPDATE captures SET github_sha = 'parent-sha-1' WHERE id = ?", (parent_id,)
    )
    await conn.commit()

    why_id = await _insert(
        conn, kind="why", raw="because the structure caught me",
        parent_id=parent_id, telegram_msg_id=42,
    )

    captured = {}
    async def _fake_put(**kwargs):
        captured["path"] = kwargs["path"]
        captured["content"] = kwargs["content"]
        captured["existing_sha"] = kwargs["existing_sha"]
        return "parent-sha-2"

    with patch("bot.github_sync.put_file", AsyncMock(side_effect=_fake_put)):
        ok = await github_sync.push_capture(why_id, settings=_settings(), conn=conn)
    assert ok is True

    # Parent sha updated
    async with conn.execute("SELECT github_sha FROM captures WHERE id = ?", (parent_id,)) as cur:
        prow = await cur.fetchone()
    assert prow["github_sha"] == "parent-sha-2"
    # Why row sha matches parent
    async with conn.execute("SELECT github_sha FROM captures WHERE id = ?", (why_id,)) as cur:
        wrow = await cur.fetchone()
    assert wrow["github_sha"] == "parent-sha-2"
    # The PUT used the old parent sha (concurrent update protection)
    assert captured["existing_sha"] == "parent-sha-1"
    # Body contains the why text
    assert "because the structure caught me" in captured["content"]


@pytest.mark.asyncio
async def test_unsynced_capture_ids_parents_first(conn):
    """nightly_sync must push parents before orphaned whys so the parent file
    exists when the why inline-update tries to happen.
    """
    p_id = await _insert(conn, kind="url", url="https://x.com")
    w_id = await _insert(conn, kind="why", raw="because", parent_id=p_id, telegram_msg_id=1)
    t_id = await _insert(conn, kind="text", raw="a line", telegram_msg_id=2)

    ids = await github_sync.unsynced_capture_ids(conn)
    # whys come after non-whys
    w_pos = ids.index(w_id)
    assert ids.index(p_id) < w_pos
    assert ids.index(t_id) < w_pos
