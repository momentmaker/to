from __future__ import annotations

import json
import tomllib
from datetime import date

import pytest

from bot import db as db_mod
from bot.markdown_out import file_path_for, make_slug, render_capture_markdown


def test_make_slug_basic_and_edge_cases():
    assert make_slug("The Impediment to Action") == "the-impediment-to-action"
    assert make_slug("  many   spaces  ") == "many-spaces"
    assert make_slug("with punctuation!!!") == "with-punctuation"
    assert make_slug("") == "untitled"
    assert make_slug("!!!") == "untitled"
    # length cap with trailing hyphen trim
    long_title = "a-very-" + ("long-" * 20) + "title"
    s = make_slug(long_title)
    assert len(s) <= 48
    assert not s.endswith("-")


def test_make_slug_preserves_unicode_letters():
    # Japanese + latin mix — unicode-aware slug keeps letters.
    s = make_slug("若尊 thoughts on attention")
    assert "若尊" in s
    assert "-" in s


async def _insert(conn, **kw):
    defaults = dict(
        kind="text",
        source="telegram",
        raw="the impediment to action advances action",
        dob=date(1990, 1, 1),
        tz_name="UTC",
    )
    defaults.update(kw)
    cid = await db_mod.insert_capture(conn, **defaults)
    async with conn.execute("SELECT * FROM captures WHERE id = ?", (cid,)) as cur:
        return await cur.fetchone()


@pytest.mark.asyncio
async def test_markdown_emits_valid_toml_frontmatter(conn):
    row = await _insert(
        conn,
        kind="url",
        source="article",
        url="https://example.com/a",
        raw="https://example.com/a check this",
        processed={
            "title": "Small Ignitions",
            "tags": ["stoic", "action"],
            "quotes": ["the impediment to action advances action"],
            "summary": "A fragment on moving through obstacles.",
        },
    )

    out = render_capture_markdown(row)
    assert out.startswith("+++\n")
    first_close = out.index("\n+++\n", 3)
    fm_toml = out[4:first_close + 1]
    meta = tomllib.loads(fm_toml)

    assert meta["id"] == row["id"]
    assert meta["kind"] == "url"
    assert meta["source"] == "article"
    assert meta["url"] == "https://example.com/a"
    assert meta["title"] == "Small Ignitions"
    assert meta["tags"] == ["stoic", "action"]
    assert meta["local_date"] == row["local_date"]
    assert meta["iso_week"] == row["iso_week_key"]
    assert meta["week_idx"] == row["fz_week_idx"]
    assert meta["captured_at"] == row["created_at"]


@pytest.mark.asyncio
async def test_markdown_frontmatter_includes_telegram_msg_id_when_present(conn):
    """telegram_msg_id is the breadcrumb back to the original chat message
    (needed to re-fetch the photo from Telegram for image captures)."""
    row = await _insert(conn, kind="text", telegram_msg_id=9876)
    out = render_capture_markdown(row)
    fm_end = out.index("\n+++\n", 3)
    meta = tomllib.loads(out[4:fm_end + 1])
    assert meta["telegram_msg_id"] == 9876


@pytest.mark.asyncio
async def test_markdown_frontmatter_surfaces_scrape_error(conn):
    """When a URL scrape fails silently (bare-URL body, no quotes/summary),
    the error string from payload.scrape_error should show up in the
    frontmatter so the .md file alone tells you why the capture is thin.
    Without this, you'd have to SSH into the server and query SQLite.
    """
    row = await _insert(
        conn, kind="url", url="https://x.com/u/status/1",
        payload={"scrape": {"source": "x"}, "scrape_error": "exa returned no content"},
    )
    out = render_capture_markdown(row)
    fm_end = out.index("\n+++\n", 3)
    meta = tomllib.loads(out[4:fm_end + 1])
    assert meta["scrape_error"] == "exa returned no content"


@pytest.mark.asyncio
async def test_markdown_frontmatter_omits_scrape_error_when_clean(conn):
    """Successful captures shouldn't carry a scrape_error field at all."""
    row = await _insert(
        conn, kind="url", url="https://x.com/u/status/1",
        payload={"scrape": {"source": "x", "text": "ok"}},
    )
    out = render_capture_markdown(row)
    fm_end = out.index("\n+++\n", 3)
    meta = tomllib.loads(out[4:fm_end + 1])
    assert "scrape_error" not in meta


@pytest.mark.asyncio
async def test_markdown_frontmatter_omits_telegram_msg_id_when_absent(conn):
    """Captures without a telegram_msg_id (e.g. scheduled-generated rows)
    shouldn't have the field at all."""
    row = await _insert(conn, kind="text")
    out = render_capture_markdown(row)
    fm_end = out.index("\n+++\n", 3)
    meta = tomllib.loads(out[4:fm_end + 1])
    assert "telegram_msg_id" not in meta


@pytest.mark.asyncio
async def test_markdown_body_contains_quotes_summary_and_raw(conn):
    row = await _insert(
        conn,
        processed={
            "title": "t",
            "tags": [],
            "quotes": ["first quote", "second quote"],
            "summary": "one-line summary",
        },
    )
    out = render_capture_markdown(row)
    assert "> first quote" in out
    assert "> second quote" in out
    assert "one-line summary" in out
    assert "the impediment to action advances action" in out
    # Horizontal rule separating processed from raw
    assert "\n---\n" in out


@pytest.mark.asyncio
async def test_markdown_handles_row_without_processed(conn):
    row = await _insert(conn, raw="a short line")
    out = render_capture_markdown(row)
    assert out.startswith("+++")
    assert "a short line" in out
    # No summary/quotes/title in frontmatter when processed is None
    fm_end = out.index("\n+++\n", 3)
    meta = tomllib.loads(out[4:fm_end + 1])
    assert "title" not in meta
    assert "tags" not in meta


@pytest.mark.asyncio
async def test_why_row_renders_inline_in_parents_file(conn):
    parent = await _insert(
        conn,
        kind="url", source="article", url="https://ex.com",
        processed={"title": "Piece", "tags": [], "quotes": [], "summary": "s"},
    )
    # Two why children
    async def _insert_why(text, msg_id):
        cid = await db_mod.insert_capture(
            conn, kind="why", source="telegram", raw=text,
            parent_id=parent["id"], telegram_msg_id=msg_id,
            dob=date(1990, 1, 1), tz_name="UTC",
        )
        async with conn.execute("SELECT * FROM captures WHERE id = ?", (cid,)) as cur:
            return await cur.fetchone()

    w1 = await _insert_why("because the structure caught me", 1001)
    w2 = await _insert_why("a second thought, later", 1002)

    out = render_capture_markdown(parent, why_children=[w1, w2])
    assert "## why?" in out
    assert "because the structure caught me" in out
    assert "a second thought, later" in out
    # Each why has its timestamp prefix
    assert w1["created_at"] in out
    assert w2["created_at"] in out


@pytest.mark.asyncio
async def test_file_path_for_uses_lowercase_w_week_dir(conn):
    row = await _insert(conn, raw="hello world")
    path = file_path_for(row)
    parts = path.split("/")
    assert len(parts) == 2
    # directory like "2026-w16"
    assert parts[0][4] == "-" and parts[0][5] == "w"
    # filename starts with local_date
    assert parts[1].startswith(row["local_date"])
    assert parts[1].endswith(".md")


@pytest.mark.asyncio
async def test_file_path_includes_capture_id_for_uniqueness(conn):
    """Two captures on the same local_date with the same slug-basis must get
    distinct paths. The id in the filename guarantees that.
    """
    row1 = await _insert(conn, raw="identical")
    row2 = await _insert(conn, raw="identical", telegram_msg_id=99)  # different msg_id
    assert file_path_for(row1) != file_path_for(row2)
