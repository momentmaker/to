"""Build the cumulative FzState JSON that imports into fz.ax.

Schema (matches fz.ax/types/state.ts exactly):

    {
      "fzAxBackup": true,
      "exportedAt": "2026-04-21T22:00:00Z",
      "state": {
        "version": 1,
        "dob": "YYYY-MM-DD",
        "weeks": { "<idx>": {"mark": "...", "whisper": "...", "markedAt": "..."} },
        "vow": null | {"text": "...", "writtenAt": "..."},
        "letters": [],
        "anchors": [sorted week indices],
        "prefs": {...},
        "meta": {...}
      }
    }

This is written to `fz-ax-backup.json` at the repo root and overwrites weekly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from bot.config import Settings

log = logging.getLogger(__name__)


_DEFAULT_PREFS: dict[str, Any] = {
    "theme": "auto",
    "pushOptIn": False,
    "reducedMotion": "auto",
    "weekStart": "mon",
}

# Whitelist for kv-stored prefs. Any value outside these constraints is
# dropped rather than passed through to the FzState export — fz.ax's
# `isValidFzState` would reject the whole backup on unexpected data,
# silently breaking import.
_PREFS_ALLOWED: dict[str, tuple] = {
    "theme":          ("auto", "light", "dark"),
    "pushOptIn":      (True, False),
    "reducedMotion":  ("auto", True, False),
    "weekStart":      ("mon", "sun"),
}


def _sanitize_prefs(stored: dict[str, Any]) -> dict[str, Any]:
    """Keep only whitelisted keys with whitelisted values."""
    clean: dict[str, Any] = {}
    for key, allowed in _PREFS_ALLOWED.items():
        if key not in stored:
            continue
        value = stored[key]
        if value in allowed:
            clean[key] = value
    return clean


async def _get_kv_json(conn: aiosqlite.Connection, key: str) -> Any | None:
    async with conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        log.warning("corrupt kv value at key=%s", key)
        return None


async def set_vow(conn: aiosqlite.Connection, text: str) -> None:
    """Store the user's vow. writtenAt is the moment they set it."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    value = json.dumps({"text": text.strip(), "writtenAt": now_iso})
    await conn.execute(
        """
        INSERT INTO kv (key, value, updated_at) VALUES ('vow', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (value, now_iso),
    )
    await conn.commit()


async def get_or_init_created_at(conn: aiosqlite.Connection) -> str:
    """Return the first-run timestamp (stored in kv), setting it on first call."""
    existing = await _get_kv_json(conn, "meta_created_at")
    if isinstance(existing, str):
        return existing
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await conn.execute(
        """
        INSERT INTO kv (key, value, updated_at) VALUES ('meta_created_at', ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (json.dumps(now_iso), now_iso),
    )
    await conn.commit()
    # Re-read in case another path wrote it first (DO NOTHING would have kept
    # the earlier value). Fall back to our own `now_iso` if the read fails.
    persisted = await _get_kv_json(conn, "meta_created_at")
    return persisted if isinstance(persisted, str) else now_iso


async def build_fz_state(
    *, conn: aiosqlite.Connection, settings: Settings,
) -> dict[str, Any]:
    """Assemble the full backup object, ready to json.dumps and push.

    Reads all completed `weekly` rows (status='processed') and composes the
    sparse `weeks` map + sorted `anchors` list.
    """
    dob = settings.DOB
    if not dob:
        raise RuntimeError("DOB is required to build FzState")

    async with conn.execute(
        """
        SELECT fz_week_idx, mark, whisper, marked_at
        FROM weekly
        WHERE status = 'processed' AND mark IS NOT NULL AND mark != ''
        ORDER BY fz_week_idx
        """
    ) as cur:
        rows = list(await cur.fetchall())

    weeks: dict[str, dict[str, Any]] = {}
    anchors: list[int] = []
    for r in rows:
        idx = int(r["fz_week_idx"])
        entry: dict[str, Any] = {
            "mark": r["mark"],
            "markedAt": r["marked_at"] or _now_iso(),
        }
        whisper = r["whisper"]
        if isinstance(whisper, str) and whisper.strip():
            entry["whisper"] = whisper
        # JSON dict keys must be strings — fz.ax parses them back to numbers.
        weeks[str(idx)] = entry
        anchors.append(idx)

    vow = await _get_kv_json(conn, "vow")
    if not (isinstance(vow, dict) and isinstance(vow.get("text"), str)):
        vow = None

    stored_prefs = await _get_kv_json(conn, "prefs")
    prefs = dict(_DEFAULT_PREFS)
    prefs["weekStart"] = settings.WEEK_START if settings.WEEK_START in ("mon", "sun") else "mon"
    if isinstance(stored_prefs, dict):
        prefs.update(_sanitize_prefs(stored_prefs))

    created_at = await get_or_init_created_at(conn)
    stored_meta = await _get_kv_json(conn, "meta_extras") or {}
    meta: dict[str, Any] = {"createdAt": created_at}
    if isinstance(stored_meta, dict):
        for k in ("lastSundayPrompt", "lastEcho", "lastVisitedWeek", "installedPwa"):
            if k in stored_meta:
                meta[k] = stored_meta[k]

    state: dict[str, Any] = {
        "version": 1,
        "dob": dob,
        "weeks": weeks,
        "vow": vow,
        "letters": [],
        "anchors": sorted(set(anchors)),
        "prefs": prefs,
        "meta": meta,
    }
    return {
        "fzAxBackup": True,
        "exportedAt": _now_iso(),
        "state": state,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def serialize(state: dict[str, Any]) -> str:
    """Deterministic JSON for a stable file on GitHub (fewer spurious diffs)."""
    return json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
