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


# ---- fetch_file -----------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_file_returns_content_and_sha():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["params"] = dict(request.url.params)
        body = base64.b64encode("hello world".encode()).decode("ascii")
        return httpx.Response(200, json={"content": body, "sha": "sha-xyz"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await github_sync.fetch_file(
            settings=_settings(), path="2026-w17/digest.md", client=client,
        )
    assert result == ("hello world", "sha-xyz")
    assert captured["method"] == "GET"
    assert "/repos/user/to-commonplace/contents/2026-w17/digest.md" in captured["url"]
    assert captured["params"] == {"ref": "main"}


@pytest.mark.asyncio
async def test_fetch_file_returns_none_on_404():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await github_sync.fetch_file(
            settings=_settings(), path="missing.md", client=client,
        )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_file_raises_on_auth_error():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await github_sync.fetch_file(
                settings=_settings(), path="x.md", client=client,
            )


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
async def test_put_binary_file_base64_encodes_bytes_directly():
    """Binary uploads (photos) must NOT pass through .encode('utf-8') —
    that path corrupts non-text bytes. put_binary_file takes bytes and
    base64-encodes them straight."""
    captured = {}
    payload = bytes(range(256))  # all byte values, including non-utf8

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"content": {"sha": "asset-sha"}})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sha = await github_sync.put_binary_file(
            settings=_settings(),
            path="2026-w17/assets/000123-x.jpg",
            content=payload,
            message="asset",
            client=client,
        )
    assert sha == "asset-sha"
    assert "/2026-w17/assets/000123-x.jpg" in captured["url"]
    assert base64.b64decode(captured["body"]["content"]) == payload


@pytest.mark.asyncio
async def test_push_capture_image_pushes_asset_then_md(conn):
    """For image rows with asset_bytes, the asset must be PUT before the .md
    so a viewer following the .md never lands on a missing asset."""
    from unittest.mock import AsyncMock, patch

    cid = await _insert(
        conn,
        kind="image",
        source="telegram",
        raw="caption",
        processed={"title": "On The Train"},
        asset_bytes=b"\xff\xd8\xff\xe0fake-jpeg-data",
        asset_mime="image/jpeg",
        telegram_msg_id=777,
    )
    assert cid is not None
    await conn.execute(
        "UPDATE captures SET status = 'processed' WHERE id = ?", (cid,)
    )
    await conn.commit()

    call_log: list[tuple[str, str]] = []

    async def _fake_put_file(**kwargs):
        call_log.append(("md", kwargs["path"]))
        return "md-sha"

    async def _fake_put_binary(**kwargs):
        call_log.append(("asset", kwargs["path"]))
        # asset bytes flow through unchanged
        assert kwargs["content"] == b"\xff\xd8\xff\xe0fake-jpeg-data"
        return "asset-sha"

    with patch("bot.github_sync.put_file", AsyncMock(side_effect=_fake_put_file)), \
         patch("bot.github_sync.put_binary_file", AsyncMock(side_effect=_fake_put_binary)):
        ok = await github_sync.push_capture(cid, settings=_settings(), conn=conn)
    assert ok is True

    # Asset first, then md
    assert [c[0] for c in call_log] == ["asset", "md"]
    # Paths align: same week, same id+slug stem, different dir + extension
    asset_path = call_log[0][1]
    md_path = call_log[1][1]
    assert asset_path.endswith(".jpg")
    assert "/assets/" in asset_path
    assert md_path.endswith(".md")
    assert asset_path.split("/")[0] == md_path.split("/")[0]  # same week dir


@pytest.mark.asyncio
async def test_push_capture_image_without_asset_bytes_skips_binary_push(conn):
    """Legacy image rows captured before this feature shipped have no asset
    bytes — push only the .md, never call put_binary_file."""
    from unittest.mock import AsyncMock, patch

    cid = await _insert(
        conn, kind="image", source="telegram", raw="legacy",
        telegram_msg_id=778,
    )
    await conn.execute(
        "UPDATE captures SET status = 'processed' WHERE id = ?", (cid,)
    )
    await conn.commit()

    put = AsyncMock(return_value="md-sha")
    binary = AsyncMock(return_value="asset-sha")
    with patch("bot.github_sync.put_file", put), \
         patch("bot.github_sync.put_binary_file", binary):
        ok = await github_sync.push_capture(cid, settings=_settings(), conn=conn)
    assert ok is True
    binary.assert_not_awaited()
    put.assert_awaited_once()


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
