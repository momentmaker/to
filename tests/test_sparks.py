from __future__ import annotations

from pathlib import Path

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


def test_append_spark_to_empty_file(tmp_path):
    p = tmp_path / "sparks.md"
    sparks.append_spark(p, date="2026-05-03", line="hello world")
    assert p.read_text() == "# sparks\n\n2026-05-03 — hello world\n"


def test_append_spark_to_header_only(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n")
    sparks.append_spark(p, date="2026-05-03", line="hello world")
    assert p.read_text() == "# sparks\n\n2026-05-03 — hello world\n"


def test_append_spark_inserts_blank_line(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n2026-05-02 — yesterday\n")
    sparks.append_spark(p, date="2026-05-03", line="today")
    assert p.read_text() == (
        "# sparks\n\n2026-05-02 — yesterday\n\n2026-05-03 — today\n"
    )


def test_append_spark_strips_extra_trailing_newlines(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n2026-05-02 — yesterday\n\n\n\n")
    sparks.append_spark(p, date="2026-05-03", line="today")
    assert p.read_text() == (
        "# sparks\n\n2026-05-02 — yesterday\n\n2026-05-03 — today\n"
    )


def test_append_spark_idempotent_on_duplicate_last_entry(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n2026-05-03 — already here\n")
    sparks.append_spark(p, date="2026-05-03", line="already here")
    assert p.read_text() == "# sparks\n\n2026-05-03 — already here\n"


@pytest.mark.asyncio
async def test_daily_sparks_job_writes_and_pushes(monkeypatch, tmp_path):
    settings = fake_settings(GITHUB_TOKEN="t", GITHUB_REPO="x/y")

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                  iso_week_key, fz_week_idx, status)
            VALUES ('text', 'a sharp line worth keeping', '{}',
                    '2026-05-02T12:00:00Z', '2026-05-02', '2026-W18', 1900, 'done')
            """
        )
        await conn.commit()

        async def fake_call(**_):
            class R:
                text = '{"line": "a sharp line worth keeping"}'
            return R()

        monkeypatch.setattr("bot.sparks.call_llm", fake_call)

        captured: dict = {}

        async def fake_fetch_file(*, settings, path, client=None):
            return ("# sparks\n", "deadbeef")

        async def fake_put_file(
            *, settings, path, content, message,
            existing_sha=None, client=None,
        ):
            captured["path"] = path
            captured["content"] = content
            captured["sha"] = existing_sha
            return "newsha"

        monkeypatch.setattr(
            "bot.github_sync.fetch_file", fake_fetch_file,
        )
        monkeypatch.setattr(
            "bot.github_sync.put_file", fake_put_file,
        )

        ok = await sparks.daily_sparks_job(
            conn=conn, settings=settings, providers=FakeProviders(),
            yesterday="2026-05-02",
        )
        assert ok is True
        assert captured["path"] == "sparks.md"
        assert "2026-05-02 — a sharp line worth keeping" in captured["content"]
        assert captured["sha"] == "deadbeef"


@pytest.mark.asyncio
async def test_daily_sparks_job_skips_when_disabled():
    settings = fake_settings(SPARKS_ENABLED=False, GITHUB_TOKEN="t", GITHUB_REPO="x/y")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        ok = await sparks.daily_sparks_job(
            conn=conn, settings=settings, providers=FakeProviders(),
            yesterday="2026-05-02",
        )
        assert ok is False


@pytest.mark.asyncio
async def test_daily_sparks_job_skips_when_github_unconfigured():
    settings = fake_settings()  # no GITHUB_TOKEN/REPO
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        ok = await sparks.daily_sparks_job(
            conn=conn, settings=settings, providers=FakeProviders(),
            yesterday="2026-05-02",
        )
        assert ok is False
