import json

import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import FakeProviders, fake_settings


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, *, chat_id, text):
        self.sent.append({"chat_id": chat_id, "text": text})


async def _add_capture(conn, *, raw, local_date="2026-05-01", payload=None):
    await conn.execute(
        """
        INSERT INTO captures (kind, raw, payload, created_at, local_date,
                              iso_week_key, fz_week_idx, status)
        VALUES ('text', ?, ?, ?, ?, ?, ?, 'done')
        """,
        (raw, json.dumps(payload or {}),
         f"{local_date}T12:00:00Z", local_date, "2026-W18", 1900),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_daily_tweet_draft_job_full_flow(monkeypatch):
    settings = fake_settings(
        TWEET_DAILY_V2_ENABLED=True, TELEGRAM_OWNER_ID=123,
    )
    bot = FakeBot()

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        for raw in ["crazy last of privacy", "someone kept the data"]:
            await _add_capture(conn, raw=raw, payload={"tweetable": True})

        async def fake_call(*, purpose, **kwargs):
            class R:
                pass
            r = R()
            if purpose == "ingest":
                r.text = json.dumps([
                    {"theme": "privacy", "capture_ids": [1, 2],
                     "rationale": "both"},
                ])
            else:
                r.text = json.dumps({"stitch": "you caught it."})
            return r

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        reason = await tweet_daily.daily_tweet_draft_job(
            conn=conn, settings=settings,
            providers=FakeProviders(), bot=bot,
            today_iso="2026-05-03",
        )
        assert reason is None
        assert len(bot.sent) == 1
        assert bot.sent[0]["chat_id"] == 123
        assert "you caught it." in bot.sent[0]["text"]
        assert "draft 1/5" in bot.sent[0]["text"]
        p = await tweet_daily.get_pending(conn)
        assert p is not None
        assert p.theme == "privacy"


@pytest.mark.asyncio
async def test_daily_tweet_draft_job_disabled_when_flag_false():
    settings = fake_settings(TWEET_DAILY_V2_ENABLED=False, TELEGRAM_OWNER_ID=1)
    bot = FakeBot()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        reason = await tweet_daily.daily_tweet_draft_job(
            conn=conn, settings=settings,
            providers=FakeProviders(), bot=bot,
            today_iso="2026-05-03",
        )
        assert reason is not None
        assert bot.sent == []


@pytest.mark.asyncio
async def test_daily_tweet_draft_job_skips_when_pool_too_small():
    settings = fake_settings(TWEET_DAILY_V2_ENABLED=True, TELEGRAM_OWNER_ID=1)
    bot = FakeBot()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # Only one tweetable capture
        await _add_capture(conn, raw="lonely", payload={"tweetable": True})
        reason = await tweet_daily.daily_tweet_draft_job(
            conn=conn, settings=settings,
            providers=FakeProviders(), bot=bot,
            today_iso="2026-05-03",
        )
        assert reason is not None
        assert bot.sent == []


@pytest.mark.asyncio
async def test_daily_tweet_draft_job_skips_when_pending_already_set(monkeypatch):
    settings = fake_settings(TWEET_DAILY_V2_ENABLED=True, TELEGRAM_OWNER_ID=1)
    bot = FakeBot()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="a", payload={"tweetable": True})
        await _add_capture(conn, raw="b", payload={"tweetable": True})
        await tweet_daily.set_pending(
            conn, draft_text="existing", capture_ids=[1],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def boom(**_):
            raise AssertionError("should not be called")

        monkeypatch.setattr("bot.tweet_daily.call_llm", boom)

        reason = await tweet_daily.daily_tweet_draft_job(
            conn=conn, settings=settings,
            providers=FakeProviders(), bot=bot,
            today_iso="2026-05-03",
        )
        assert reason is not None
        # Existing pending preserved
        p = await tweet_daily.get_pending(conn)
        assert p.draft_text == "existing"
