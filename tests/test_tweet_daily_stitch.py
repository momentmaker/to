import json

import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import FakeProviders, fake_settings


@pytest.mark.asyncio
async def test_detect_themes_returns_proposals(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps([
                    {"theme": "privacy", "capture_ids": [1, 2],
                     "rationale": "both about kept data"},
                ])
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        proposals = await tweet_daily.detect_themes(
            pool_summary="[1] privacy snip\n[2] kept data snip",
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert len(proposals) == 1
        assert proposals[0].theme == "privacy"
        assert proposals[0].capture_ids == [1, 2]


@pytest.mark.asyncio
async def test_detect_themes_skips_proposals_with_wrong_capture_count(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps([
                    {"theme": "single", "capture_ids": [1], "rationale": ""},
                    {"theme": "ok", "capture_ids": [1, 2], "rationale": ""},
                    {"theme": "fournope", "capture_ids": [1, 2, 3, 4],
                     "rationale": ""},
                ])
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        proposals = await tweet_daily.detect_themes(
            pool_summary="x",
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert [p.theme for p in proposals] == ["ok"]


@pytest.mark.asyncio
async def test_detect_themes_returns_empty_on_llm_failure(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        proposals = await tweet_daily.detect_themes(
            pool_summary="x",
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert proposals == []


@pytest.mark.asyncio
async def test_pick_theme_least_used_first():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        for i in range(3):
            await conn.execute(
                """
                INSERT INTO tweets (tweet_id, tweeted_at, local_date,
                                    capture_ids, theme, text, draft_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"p{i}", "2026-05-01T01:00:00Z", "2026-05-01", "[]",
                 "privacy", "x", 1),
            )
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date,
                                capture_ids, theme, text, draft_count)
            VALUES ('c1', '2026-05-02T01:00:00Z', '2026-05-02', '[]',
                    'craft', 'x', 1)
            """
        )
        await conn.commit()

        props = [
            tweet_daily.ThemeProposal("privacy", [1, 2], ""),
            tweet_daily.ThemeProposal("craft", [3, 4], ""),
            tweet_daily.ThemeProposal("silence", [5, 6], ""),
        ]
        chosen = await tweet_daily.pick_theme(props, conn=conn)
        # 'silence' is unused → least used → wins.
        assert chosen.theme == "silence"


@pytest.mark.asyncio
async def test_pick_theme_returns_none_for_empty():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        chosen = await tweet_daily.pick_theme([], conn=conn)
        assert chosen is None


@pytest.mark.asyncio
async def test_generate_stitch_returns_dict_with_default_shape(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({
                    "shape": "insight",
                    "stitch": "you caught the asymmetry.",
                })
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        out = await tweet_daily.generate_stitch(
            theme="privacy",
            capture_summaries=[
                ("2026-04-22", "crazy last of privacy for employees"),
                ("2026-04-21", "didn't even know someone kept this data"),
            ],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert out == {
            "shape": "insight",
            "stitch": "you caught the asymmetry.",
            "lead_quote": None,
        }


@pytest.mark.asyncio
async def test_generate_stitch_quote_led_keeps_lead_quote(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({
                    "shape": "quote_led",
                    "lead_quote": "using samurai swords to cut",
                    "stitch": "the smallest blade finishes the work.",
                })
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        out = await tweet_daily.generate_stitch(
            theme="craft",
            capture_summaries=[("2026-04-26", "samurai")],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert out["shape"] == "quote_led"
        assert out["lead_quote"] == "using samurai swords to cut"
        assert out["stitch"] == "the smallest blade finishes the work."


@pytest.mark.asyncio
async def test_generate_stitch_unknown_shape_falls_back_to_insight(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({
                    "shape": "haiku",  # not a known shape
                    "stitch": "you saw it.",
                })
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        out = await tweet_daily.generate_stitch(
            theme="x",
            capture_summaries=[("2026-01-01", "a")],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert out["shape"] == "insight"


@pytest.mark.asyncio
async def test_generate_stitch_returns_none_on_llm_failure(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        out = await tweet_daily.generate_stitch(
            theme="x",
            capture_summaries=[("2026-01-01", "a")],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert out is None


@pytest.mark.asyncio
async def test_generate_stitch_returns_none_on_malformed_json(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = "not even close to json"
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        out = await tweet_daily.generate_stitch(
            theme="x",
            capture_summaries=[("2026-01-01", "a")],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert out is None
