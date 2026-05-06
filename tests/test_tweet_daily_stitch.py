import json

import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import FakeProviders, fake_settings


def _row(id_, kind="text", local_date="2026-05-03"):
    return {"id": id_, "kind": kind, "local_date": local_date}


@pytest.mark.asyncio
async def test_name_theme_returns_label(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({"theme": "childlike-wonder"})
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)
        monkeypatch.setattr(
            "bot.tweet_daily.format_pool_for_themes", lambda caps: "x",
        )

        label = await tweet_daily.name_theme(
            [_row(1), _row(2)], settings=settings,
            providers=FakeProviders(), conn=conn,
        )
        assert label == "childlike-wonder"


@pytest.mark.asyncio
async def test_name_theme_normalizes_dirty_label(monkeypatch):
    """LLMs sometimes return 'Patient Craft' or 'patient_craft' — coerce
    to clean kebab-case rather than rejecting."""
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({"theme": "Patient_Craft  "})
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)
        monkeypatch.setattr(
            "bot.tweet_daily.format_pool_for_themes", lambda caps: "x",
        )

        label = await tweet_daily.name_theme(
            [_row(1), _row(2)], settings=settings,
            providers=FakeProviders(), conn=conn,
        )
        assert label == "patient-craft"


@pytest.mark.asyncio
async def test_name_theme_falls_back_on_empty(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({"theme": ""})
            return R()

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)
        monkeypatch.setattr(
            "bot.tweet_daily.format_pool_for_themes", lambda caps: "x",
        )

        label = await tweet_daily.name_theme(
            [_row(1), _row(2)], settings=settings,
            providers=FakeProviders(), conn=conn,
        )
        assert label == "loose-rhyme"


@pytest.mark.asyncio
async def test_name_theme_falls_back_on_llm_failure(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)
        monkeypatch.setattr(
            "bot.tweet_daily.format_pool_for_themes", lambda caps: "x",
        )

        label = await tweet_daily.name_theme(
            [_row(1), _row(2)], settings=settings,
            providers=FakeProviders(), conn=conn,
        )
        assert label == "loose-rhyme"


def test_select_for_draft_takes_top_n_most_recent():
    pool = [_row(5), _row(4), _row(3), _row(2), _row(1)]
    out = tweet_daily.select_for_draft(pool, n=3)
    assert [r["id"] for r in out] == [5, 4, 3]


def test_select_for_draft_skips_excluded():
    pool = [_row(5), _row(4), _row(3), _row(2), _row(1)]
    out = tweet_daily.select_for_draft(
        pool, exclude_ids={5, 4}, n=3,
    )
    assert [r["id"] for r in out] == [3, 2, 1]


def test_select_for_draft_falls_back_when_exclusion_starves_pool():
    pool = [_row(5), _row(4), _row(3)]
    out = tweet_daily.select_for_draft(
        pool, exclude_ids={5, 4, 3}, n=3,
    )
    # Only 1 capture would remain after exclusion (none); fall back to
    # full pool to keep /next functional even on small pools.
    assert [r["id"] for r in out] == [5, 4, 3]


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
