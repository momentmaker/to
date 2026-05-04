import json

import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


@pytest.mark.asyncio
async def test_record_tweet_writes_sqlite_row():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.record_tweet(
            conn,
            tweet_id="1789",
            tweeted_at="2026-05-03T14:14:00Z",
            local_date="2026-05-03",
            capture_ids=[1, 2],
            theme="privacy",
            stitch="you saw it.",
            text="full tweet",
            draft_count=2,
            edited=False,
        )
        async with conn.execute("SELECT * FROM tweets") as cur:
            row = await cur.fetchone()
        assert row["tweet_id"] == "1789"
        assert json.loads(row["capture_ids"]) == [1, 2]
        assert row["edited"] == 0
        assert row["draft_count"] == 2


@pytest.mark.asyncio
async def test_record_tweet_marks_edited_when_true():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.record_tweet(
            conn,
            tweet_id="1",
            tweeted_at="2026-05-03T14:14:00Z",
            local_date="2026-05-03",
            capture_ids=[1, 2],
            theme=None,
            stitch=None,
            text="user override",
            draft_count=1,
            edited=True,
        )
        async with conn.execute("SELECT edited FROM tweets") as cur:
            row = await cur.fetchone()
        assert row["edited"] == 1


@pytest.mark.asyncio
async def test_push_ledger_to_repo_creates_file_on_first_push(monkeypatch):
    settings = fake_settings(GITHUB_TOKEN="t", GITHUB_REPO="x/y")
    captured: dict = {}

    async def fake_fetch(*, settings, path, client=None):
        return None  # 404 — file doesn't exist yet

    async def fake_put(*, settings, path, content, message,
                       existing_sha=None, client=None):
        captured["path"] = path
        captured["content"] = content
        captured["sha"] = existing_sha
        captured["message"] = message
        return "newsha"

    monkeypatch.setattr("bot.tweet_daily.fetch_file", fake_fetch)
    monkeypatch.setattr("bot.tweet_daily.put_file", fake_put)

    record = {
        "tweet_id": "1789",
        "tweeted_at": "2026-05-03T14:14:00Z",
        "local_date": "2026-05-03",
        "capture_ids": [1, 2],
        "theme": "privacy",
        "stitch": "x",
        "text": "y",
        "edited": False,
        "url": "https://x.com/i/web/status/1789",
    }
    await tweet_daily.push_ledger_to_repo(settings=settings, record=record)
    assert captured["path"] == "tweeted.json"
    assert captured["sha"] is None  # no prior file
    arr = json.loads(captured["content"])
    assert len(arr) == 1
    assert arr[0]["tweet_id"] == "1789"


@pytest.mark.asyncio
async def test_push_ledger_to_repo_appends_to_existing(monkeypatch):
    settings = fake_settings(GITHUB_TOKEN="t", GITHUB_REPO="x/y")
    captured: dict = {}
    existing_array = [{"tweet_id": "old", "text": "first"}]

    async def fake_fetch(*, settings, path, client=None):
        return (json.dumps(existing_array), "deadbeef")

    async def fake_put(*, settings, path, content, message,
                       existing_sha=None, client=None):
        captured["content"] = content
        captured["sha"] = existing_sha
        return "newsha"

    monkeypatch.setattr("bot.tweet_daily.fetch_file", fake_fetch)
    monkeypatch.setattr("bot.tweet_daily.put_file", fake_put)

    await tweet_daily.push_ledger_to_repo(
        settings=settings,
        record={"tweet_id": "new", "text": "second"},
    )
    arr = json.loads(captured["content"])
    assert [r["tweet_id"] for r in arr] == ["old", "new"]
    assert captured["sha"] == "deadbeef"


@pytest.mark.asyncio
async def test_push_ledger_swallows_put_failure(monkeypatch):
    settings = fake_settings(GITHUB_TOKEN="t", GITHUB_REPO="x/y")

    async def fake_fetch(*, settings, path, client=None):
        return None

    async def fake_put(**_):
        raise RuntimeError("github down")

    monkeypatch.setattr("bot.tweet_daily.fetch_file", fake_fetch)
    monkeypatch.setattr("bot.tweet_daily.put_file", fake_put)

    # Should not raise (sqlite ledger is canonical).
    await tweet_daily.push_ledger_to_repo(
        settings=settings, record={"tweet_id": "x"},
    )
