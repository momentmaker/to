from __future__ import annotations

import pytest

import aiosqlite

from bot import sparks
from bot.config import Settings
from bot.db import init_schema
from tests.helpers.fakes import FakeProviders, fake_settings


@pytest.mark.asyncio
async def test_select_spark_returns_substring_of_a_capture(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                  iso_week_key, fz_week_idx, status)
            VALUES ('text', 'crazy last of privacy for employees - literally like neo-serfs',
                    '{}', '2026-04-22T12:00:00Z', '2026-04-22', '2026-W17', 1888, 'done')
            """
        )
        await conn.commit()

        async def fake_call(*, purpose, system_blocks, messages, max_tokens,
                            settings, providers, conn):
            class R: text = '{"line": "crazy last of privacy for employees"}'
            return R()
        monkeypatch.setattr("bot.sparks.call_llm", fake_call)

        line = await sparks.select_spark(
            conn, local_date="2026-04-22",
            settings=settings, providers=FakeProviders(),
        )
        assert line == "crazy last of privacy for employees"


@pytest.mark.asyncio
async def test_select_spark_none_when_no_captures():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        line = await sparks.select_spark(
            conn, local_date="2026-04-22",
            settings=settings, providers=FakeProviders(),
        )
        assert line is None


@pytest.mark.asyncio
async def test_select_spark_skips_when_llm_pick_not_substring(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                  iso_week_key, fz_week_idx, status)
            VALUES ('text', 'one capture body', '{}', '2026-04-22T12:00:00Z',
                    '2026-04-22', '2026-W17', 1888, 'done')
            """
        )
        await conn.commit()

        async def fake_call(**_):
            class R: text = '{"line": "totally invented sentence not present"}'
            return R()
        monkeypatch.setattr("bot.sparks.call_llm", fake_call)

        line = await sparks.select_spark(
            conn, local_date="2026-04-22",
            settings=settings, providers=FakeProviders(),
        )
        assert line is None
