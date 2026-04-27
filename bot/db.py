"""SQLite schema + async helpers. Single writer; WAL mode.

Schema is versioned via `PRAGMA user_version`. Add new migrations to the
MIGRATIONS list — each entry is an upgrade from version N-1 to N.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import aiosqlite

from bot.week import fz_week_idx, iso_week_key, local_date_for, parse_dob


_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS captures (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,
  source        TEXT,
  url           TEXT,
  raw           TEXT,
  payload       TEXT,
  processed     TEXT,
  parent_id     INTEGER REFERENCES captures(id) ON DELETE SET NULL,
  telegram_msg_id INTEGER,
  created_at    TEXT NOT NULL,
  local_date    TEXT NOT NULL,
  iso_week_key  TEXT NOT NULL,
  fz_week_idx   INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  error         TEXT,
  github_sha    TEXT
);
CREATE INDEX IF NOT EXISTS idx_captures_local_date ON captures(local_date);
CREATE INDEX IF NOT EXISTS idx_captures_week ON captures(fz_week_idx);
CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status);
CREATE INDEX IF NOT EXISTS idx_captures_parent ON captures(parent_id);
-- dedupe: Telegram retries webhooks on non-2xx. A second delivery with the
-- same (source, telegram_msg_id) must be rejected. SQLite treats NULLs as
-- distinct in UNIQUE indexes, so captures without a telegram_msg_id are fine.
CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_tg_msg_dedupe
  ON captures(source, telegram_msg_id);

CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
  raw, processed, url,
  content='captures', content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
  INSERT INTO captures_fts(rowid, raw, processed, url)
  VALUES (new.id, new.raw, new.processed, new.url);
END;
CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
  INSERT INTO captures_fts(captures_fts, rowid, raw, processed, url)
  VALUES('delete', old.id, old.raw, old.processed, old.url);
END;
CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
  INSERT INTO captures_fts(captures_fts, rowid, raw, processed, url)
  VALUES('delete', old.id, old.raw, old.processed, old.url);
  INSERT INTO captures_fts(rowid, raw, processed, url)
  VALUES (new.id, new.raw, new.processed, new.url);
END;

CREATE TABLE IF NOT EXISTS media (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id  INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  mime        TEXT NOT NULL,
  path        TEXT NOT NULL,
  bytes       INTEGER
);

CREATE TABLE IF NOT EXISTS daily (
  local_date    TEXT PRIMARY KEY,
  prompt        TEXT,
  prompted_at   TEXT,
  reflection_capture_id INTEGER REFERENCES captures(id),
  tweet_text    TEXT,
  tweet_posted_at TEXT,
  github_sha    TEXT
);

CREATE TABLE IF NOT EXISTS weekly (
  fz_week_idx   INTEGER PRIMARY KEY,
  iso_week_key  TEXT NOT NULL,
  essay         TEXT,
  whisper       TEXT,
  mark          TEXT,
  marked_at     TEXT,
  tweet_text    TEXT,
  tweet_posted_at TEXT,
  github_sha    TEXT,
  fz_export_sha TEXT,
  status        TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS kv (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT NOT NULL,
  provider   TEXT NOT NULL,
  model      TEXT NOT NULL,
  purpose    TEXT NOT NULL,
  input_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_month ON llm_usage(substr(at,1,7));
"""

_MIGRATION_V2 = """
ALTER TABLE captures ADD COLUMN asset_bytes BLOB;
ALTER TABLE captures ADD COLUMN asset_mime TEXT;
"""


# Ordered list of migrations. MIGRATIONS[i] upgrades schema from v(i) to v(i+1).
# v0 = empty DB. Never modify a migration once shipped; append new ones.
MIGRATIONS: list[str] = [
    _MIGRATION_V1,
    _MIGRATION_V2,
]


async def init_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")

    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = int(row[0]) if row else 0

    target = len(MIGRATIONS)
    for version in range(current, target):
        await conn.executescript(MIGRATIONS[version])
        # PRAGMA doesn't support parameter binding
        await conn.execute(f"PRAGMA user_version = {version + 1}")
    await conn.commit()


async def connect(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await init_schema(conn)
    return conn


async def insert_capture(
    conn: aiosqlite.Connection,
    *,
    kind: str,
    raw: str | None = None,
    source: str | None = None,
    url: str | None = None,
    payload: dict[str, Any] | None = None,
    processed: dict[str, Any] | None = None,
    parent_id: int | None = None,
    telegram_msg_id: int | None = None,
    asset_bytes: bytes | None = None,
    asset_mime: str | None = None,
    dob: date,
    tz_name: str,
    created_at: datetime | None = None,
    status: str = "pending",
) -> int | None:
    """Insert a capture row. Returns the new row id, or None if the row is a
    duplicate (same source + telegram_msg_id as an existing row)."""
    created_at = created_at or datetime.now(timezone.utc)
    local_d = local_date_for(created_at, tz_name)
    cursor = await conn.execute(
        """
        INSERT INTO captures (
            kind, source, url, raw, payload, processed,
            parent_id, telegram_msg_id, asset_bytes, asset_mime,
            created_at, local_date, iso_week_key, fz_week_idx, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, telegram_msg_id) DO NOTHING
        RETURNING id
        """,
        (
            kind, source, url, raw,
            json.dumps(payload) if payload is not None else None,
            json.dumps(processed) if processed is not None else None,
            parent_id, telegram_msg_id, asset_bytes, asset_mime,
            created_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            local_d.isoformat(),
            iso_week_key(local_d),
            fz_week_idx(local_d, dob),
            status,
        ),
    )
    row = await cursor.fetchone()
    await conn.commit()
    return int(row[0]) if row else None


async def count_captures(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM captures") as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def count_captures_this_week(conn: aiosqlite.Connection, *, dob: date, tz_name: str) -> int:
    today = local_date_for(datetime.now(timezone.utc), tz_name)
    w = fz_week_idx(today, dob)
    async with conn.execute(
        "SELECT COUNT(*) FROM captures WHERE fz_week_idx = ?", (w,)
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


def settings_dob(dob_str: str) -> date:
    if not dob_str:
        raise ValueError("DOB env var is required (YYYY-MM-DD)")
    return parse_dob(dob_str)
