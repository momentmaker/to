"""/forget — delete a capture from SQLite AND its GitHub representation.

Cascade rules:
- Deleting a non-why capture also deletes all its why children (the whys
  lived inline in the parent's file, which is about to disappear anyway)
  and clears any `daily.reflection_capture_id` pointer at this row.
- Deleting a why re-renders its parent's file without the why — we don't
  delete the parent or the other whys.

GitHub errors are logged but don't block the SQLite delete. The "forget"
contract is: the row is gone from the owner's own commonplace. If the
GitHub side is out of sync for any reason, the SQLite state is still the
source of truth and nightly_sync can't resurrect a deleted row.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from bot import github_sync, markdown_out
from bot.config import Settings

log = logging.getLogger(__name__)


async def forget_capture(
    conn: aiosqlite.Connection,
    capture_id: int,
    *,
    settings: Settings,
) -> dict[str, Any] | None:
    """Fully remove `capture_id`. Returns a summary dict on success, None
    if the capture doesn't exist.
    """
    async with conn.execute(
        "SELECT * FROM captures WHERE id = ?", (capture_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None

    kind = row["kind"]
    if kind == "why":
        return await _forget_why(conn, row, settings=settings)
    return await _forget_primary(conn, row, settings=settings)


async def _forget_primary(
    conn: aiosqlite.Connection, row: Any, *, settings: Settings,
) -> dict[str, Any]:
    """Delete a non-why capture: its whys, its daily ref, the GitHub file, the row."""
    capture_id = int(row["id"])
    github_deleted = False

    # Delete the GitHub file first. If it fails we still continue with the
    # SQLite delete — the user asked to forget it, so don't block on external
    # state. A stale file on GitHub is a lesser evil than a phantom in the
    # bot's memory.
    if row["github_sha"] and github_sync.is_configured(settings):
        try:
            path = markdown_out.file_path_for(row)
            github_deleted = await github_sync.delete_file(
                settings=settings,
                path=path,
                sha=row["github_sha"],
                message=f"forget: {row['kind']} {row['local_date']} (capture {capture_id})",
            )
        except Exception:
            log.exception("forget: GitHub delete failed for capture %s", capture_id)

    # Cascade: delete all why children (they lived inside the parent's file).
    async with conn.execute(
        "DELETE FROM captures WHERE parent_id = ? RETURNING id", (capture_id,),
    ) as cur:
        removed_whys = [int(r[0]) for r in await cur.fetchall()]

    # Clear any daily.reflection_capture_id pointing at this row.
    await conn.execute(
        "UPDATE daily SET reflection_capture_id = NULL WHERE reflection_capture_id = ?",
        (capture_id,),
    )

    await conn.execute("DELETE FROM captures WHERE id = ?", (capture_id,))
    await conn.commit()

    return {
        "id": capture_id,
        "kind": row["kind"],
        "github_deleted": github_deleted,
        "cascaded_whys": removed_whys,
    }


async def _forget_why(
    conn: aiosqlite.Connection, why_row: Any, *, settings: Settings,
) -> dict[str, Any]:
    """Delete a why: re-render parent's file without this why, keep siblings."""
    why_id = int(why_row["id"])
    parent_id = why_row["parent_id"]

    if parent_id is None:
        # Orphan why with no parent to re-render — just drop the row.
        await conn.execute("DELETE FROM captures WHERE id = ?", (why_id,))
        await conn.commit()
        return {"id": why_id, "kind": "why", "github_deleted": False, "cascaded_whys": []}

    async with conn.execute(
        "SELECT * FROM captures WHERE id = ?", (parent_id,),
    ) as cur:
        parent = await cur.fetchone()
    if parent is None:
        # Parent's gone for some reason — just drop the why.
        await conn.execute("DELETE FROM captures WHERE id = ?", (why_id,))
        await conn.commit()
        return {"id": why_id, "kind": "why", "github_deleted": False, "cascaded_whys": []}

    # Rebuild parent's file with the remaining siblings.
    async with conn.execute(
        "SELECT * FROM captures WHERE parent_id = ? AND id != ? ORDER BY created_at",
        (parent_id, why_id),
    ) as cur:
        siblings = list(await cur.fetchall())

    github_updated = False
    if parent["github_sha"] and github_sync.is_configured(settings):
        try:
            markdown = markdown_out.render_capture_markdown(parent, why_children=siblings)
            path = markdown_out.file_path_for(parent)
            new_sha = await github_sync.put_file(
                settings=settings,
                path=path,
                content=markdown,
                message=f"forget: remove why {why_id} from capture {parent_id}",
                existing_sha=parent["github_sha"],
            )
            await conn.execute(
                "UPDATE captures SET github_sha = ? WHERE id = ?",
                (new_sha, int(parent["id"])),
            )
            # Siblings share the parent file's sha.
            if siblings:
                placeholders = ",".join("?" for _ in siblings)
                await conn.execute(
                    f"UPDATE captures SET github_sha = ? WHERE id IN ({placeholders})",
                    (new_sha, *[int(s["id"]) for s in siblings]),
                )
            github_updated = True
        except Exception:
            log.exception("forget: parent file re-render failed")

    await conn.execute("DELETE FROM captures WHERE id = ?", (why_id,))
    await conn.commit()
    return {
        "id": why_id,
        "kind": "why",
        "github_deleted": github_updated,
        "cascaded_whys": [],
    }


async def find_most_recent_id(conn: aiosqlite.Connection) -> int | None:
    async with conn.execute(
        "SELECT id FROM captures ORDER BY id DESC LIMIT 1",
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None
