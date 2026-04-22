from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from bot import db as db_mod
from bot import scheduler as sched_mod
from bot.config import Settings
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=1, DOB="1990-01-01",
        TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        GITHUB_TOKEN="ghp_test", GITHUB_REPO="u/r",
    )
    base.update(kw)
    return Settings(**base)


class _FakeProv:
    name = "anthropic"
    def __init__(self, text: str):
        self._text = text
    async def chat(self, **kwargs):
        return LlmResponse(
            text=self._text, model=kwargs["model"], provider="anthropic",
            input_tokens=10, output_tokens=5,
        )


# ---- process_pending ------------------------------------------------------

@pytest.mark.asyncio
async def test_process_pending_reruns_stale_pending_captures(conn):
    # Insert a capture with an old created_at so it's past the 30s grace.
    past = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
    cid = await db_mod.insert_capture(
        conn, kind="text", raw="the impediment to action",
        dob=date(1990, 1, 1), tz_name="UTC", created_at=past,
    )
    assert cid is not None

    providers = Providers(
        _FakeProv('{"title":"t","tags":["a"],"quotes":[],"summary":"s"}'), None,
    )
    with patch("bot.github_sync.push_capture", AsyncMock(return_value=True)):
        count = await sched_mod.process_pending(
            conn=conn, settings=_settings(), providers=providers,
        )
    assert count == 1

    async with conn.execute("SELECT status, processed FROM captures WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "processed"
    assert '"title":' in row["processed"]


@pytest.mark.asyncio
async def test_process_pending_pushes_to_github_after_processing(conn):
    """Regression: process_pending must push to GitHub on success. Without
    it, captures rescued by this job wait for nightly_sync instead of
    landing in the repo promptly.
    """
    past = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
    cid = await db_mod.insert_capture(
        conn, kind="text", raw="a line",
        dob=date(1990, 1, 1), tz_name="UTC", created_at=past,
    )
    providers = Providers(
        _FakeProv('{"title":"t","tags":[],"quotes":[],"summary":""}'), None,
    )
    push_mock = AsyncMock(return_value=True)
    with patch("bot.github_sync.push_capture", push_mock):
        await sched_mod.process_pending(
            conn=conn, settings=_settings(), providers=providers,
        )
    push_mock.assert_awaited_once()
    # Called with the capture id we expect
    assert push_mock.await_args.args[0] == cid


@pytest.mark.asyncio
async def test_process_pending_ignores_recent_rows(conn):
    """A capture created moments ago is still being worked on by its
    in-flight background task; the retry sweep shouldn't double-process it.
    """
    recent = datetime.now(timezone.utc)
    cid = await db_mod.insert_capture(
        conn, kind="text", raw="recent", dob=date(1990, 1, 1), tz_name="UTC",
        created_at=recent,
    )
    providers = Providers(_FakeProv('{}'), None)
    count = await sched_mod.process_pending(
        conn=conn, settings=_settings(), providers=providers,
    )
    assert count == 0
    async with conn.execute("SELECT status FROM captures WHERE id = ?", (cid,)) as cur:
        assert (await cur.fetchone())["status"] == "pending"


@pytest.mark.asyncio
async def test_process_pending_marks_failed_on_exception(conn):
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    cid = await db_mod.insert_capture(
        conn, kind="text", raw="x",
        dob=date(1990, 1, 1), tz_name="UTC", created_at=past,
    )

    class _Broken:
        name = "anthropic"
        async def chat(self, **kwargs):
            raise RuntimeError("llm down")
    providers = Providers(_Broken(), None)

    await sched_mod.process_pending(
        conn=conn, settings=_settings(), providers=providers,
    )
    async with conn.execute("SELECT status, error FROM captures WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert "llm down" in row["error"]


@pytest.mark.asyncio
async def test_process_pending_uses_scraped_text_from_payload(conn):
    """For URL captures, _derive_content should prefer the scraped article
    body (stored in payload.scrape) over the raw URL string.
    """
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    cid = await db_mod.insert_capture(
        conn, kind="url", source="article",
        url="https://ex.com/a",
        raw="https://ex.com/a",
        payload={"scrape": {
            "source": "article",
            "title": "A Piece",
            "text": "the extracted body of the article",
            "method": "readability",
        }},
        dob=date(1990, 1, 1), tz_name="UTC", created_at=past,
    )

    seen_content: list[str] = []
    class _Spy:
        name = "anthropic"
        async def chat(self, **kwargs):
            seen_content.append(kwargs["messages"][0].content)
            return LlmResponse(
                text='{"title":"t","tags":[],"quotes":[],"summary":"s"}',
                model="m", provider="anthropic", input_tokens=1, output_tokens=1,
            )
    providers = Providers(_Spy(), None)

    with patch("bot.github_sync.push_capture", AsyncMock(return_value=True)):
        await sched_mod.process_pending(
            conn=conn, settings=_settings(), providers=providers,
        )

    assert seen_content and "extracted body" in seen_content[0]
    assert "A Piece" in seen_content[0]


# ---- nightly_sync ---------------------------------------------------------

@pytest.mark.asyncio
async def test_nightly_sync_picks_up_unsynced_rows(conn):
    synced = await db_mod.insert_capture(
        conn, kind="text", raw="already",
        dob=date(1990, 1, 1), tz_name="UTC",
    )
    await conn.execute("UPDATE captures SET github_sha = 'x' WHERE id = ?", (synced,))
    unsynced = await db_mod.insert_capture(
        conn, kind="text", raw="fresh",
        dob=date(1990, 1, 1), tz_name="UTC", telegram_msg_id=2,
    )
    await conn.commit()

    pushed_ids: list[int] = []
    async def _fake_push(cap_id, *, settings, conn, client=None):
        pushed_ids.append(cap_id)
        return True

    with patch("bot.scheduler.github_sync.push_capture", AsyncMock(side_effect=_fake_push)):
        count = await sched_mod.nightly_sync(conn=conn, settings=_settings())

    assert count == 1
    assert pushed_ids == [unsynced]


@pytest.mark.asyncio
async def test_nightly_sync_is_noop_when_github_not_configured(conn):
    await db_mod.insert_capture(
        conn, kind="text", raw="x", dob=date(1990, 1, 1), tz_name="UTC",
    )
    settings = _settings(GITHUB_TOKEN="", GITHUB_REPO="")
    count = await sched_mod.nightly_sync(conn=conn, settings=settings)
    assert count == 0


# ---- build_scheduler ------------------------------------------------------

def test_build_scheduler_registers_expected_jobs(conn):
    providers = Providers(_FakeProv(""), None)
    scheduler = sched_mod.build_scheduler(
        conn=conn, settings=_settings(), providers=providers,
    )
    ids = {j.id for j in scheduler.get_jobs()}
    assert ids == {"process_pending", "nightly_sync"}
    # Scheduler is NOT started — build should construct only.
    assert not scheduler.running


# ---- drain_on_boot --------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_on_boot_runs_both_jobs_once(conn):
    providers = Providers(_FakeProv('{"title":"t","tags":[],"quotes":[],"summary":"s"}'), None)

    with patch("bot.scheduler.process_pending",
               AsyncMock(return_value=3)) as pp, \
         patch("bot.scheduler.nightly_sync",
               AsyncMock(return_value=2)) as ns:
        await sched_mod.drain_on_boot(
            conn=conn, settings=_settings(), providers=providers,
        )

    pp.assert_awaited_once()
    ns.assert_awaited_once()
