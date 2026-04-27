"""/forget — delete a capture from SQLite AND its GitHub representation.

Cascade rules:
- Deleting a primary capture also deletes all its inline children (whys and
  highlights, which lived inside the parent's file that's about to
  disappear) and clears any `daily.reflection_capture_id` pointer at this
  row.
- Deleting a why or highlight re-renders its parent's file without that
  child — we don't delete the parent or the other siblings.

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
    if kind in ("why", "highlight"):
        return await _forget_child(conn, row, settings=settings)
    return await _forget_primary(conn, row, settings=settings)


async def _forget_primary(
    conn: aiosqlite.Connection, row: Any, *, settings: Settings,
) -> dict[str, Any]:
    """Delete a primary capture: all its inline children (whys + highlights),
    its daily reflection ref, the GitHub file, and the row itself."""
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

        # For image captures, the .md references a sibling asset under
        # assets/. Remove that too — otherwise /forget leaves a JPEG behind
        # that re-appears whenever someone browses the week directory.
        asset_path = markdown_out.asset_path_for(row)
        if asset_path:
            try:
                asset_sha = await github_sync.fetch_file_sha(
                    settings=settings, path=asset_path,
                )
                if asset_sha:
                    await github_sync.delete_file(
                        settings=settings,
                        path=asset_path,
                        sha=asset_sha,
                        message=f"forget: asset {row['local_date']} (capture {capture_id})",
                    )
            except Exception:
                log.exception(
                    "forget: GitHub asset delete failed for capture %s", capture_id,
                )

    # Cascade: delete all inline children (whys and highlights live inside
    # the parent's file, which is about to disappear).
    async with conn.execute(
        "DELETE FROM captures WHERE parent_id = ? RETURNING id", (capture_id,),
    ) as cur:
        removed_children = [int(r[0]) for r in await cur.fetchall()]

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
        "cascaded_children": removed_children,
    }


async def _forget_child(
    conn: aiosqlite.Connection, child_row: Any, *, settings: Settings,
) -> dict[str, Any]:
    """Delete a why or highlight: re-render parent's file without this child,
    keep the siblings (of both kinds) intact."""
    child_id = int(child_row["id"])
    kind = child_row["kind"]
    parent_id = child_row["parent_id"]

    if parent_id is None:
        await conn.execute("DELETE FROM captures WHERE id = ?", (child_id,))
        await conn.commit()
        return {"id": child_id, "kind": kind, "github_deleted": False, "cascaded_children": []}

    async with conn.execute(
        "SELECT * FROM captures WHERE id = ?", (parent_id,),
    ) as cur:
        parent = await cur.fetchone()
    if parent is None:
        await conn.execute("DELETE FROM captures WHERE id = ?", (child_id,))
        await conn.commit()
        return {"id": child_id, "kind": kind, "github_deleted": False, "cascaded_children": []}

    # Rebuild parent's file with the remaining siblings, split by kind so the
    # renderer can place each under its right section.
    async with conn.execute(
        "SELECT * FROM captures WHERE parent_id = ? AND kind = 'why' AND id != ? "
        "ORDER BY created_at",
        (parent_id, child_id),
    ) as cur:
        why_siblings = list(await cur.fetchall())
    async with conn.execute(
        "SELECT * FROM captures WHERE parent_id = ? AND kind = 'highlight' AND id != ? "
        "ORDER BY created_at",
        (parent_id, child_id),
    ) as cur:
        highlight_siblings = list(await cur.fetchall())

    github_updated = False
    if parent["github_sha"] and github_sync.is_configured(settings):
        try:
            markdown = markdown_out.render_capture_markdown(
                parent,
                why_children=why_siblings,
                highlight_children=highlight_siblings,
            )
            path = markdown_out.file_path_for(parent)
            new_sha = await github_sync.put_file(
                settings=settings,
                path=path,
                content=markdown,
                message=f"forget: remove {kind} {child_id} from capture {parent_id}",
                existing_sha=parent["github_sha"],
            )
            await conn.execute(
                "UPDATE captures SET github_sha = ? WHERE id = ?",
                (new_sha, int(parent["id"])),
            )
            sibling_ids = [int(s["id"]) for s in why_siblings] + \
                          [int(s["id"]) for s in highlight_siblings]
            if sibling_ids:
                placeholders = ",".join("?" for _ in sibling_ids)
                await conn.execute(
                    f"UPDATE captures SET github_sha = ? WHERE id IN ({placeholders})",
                    (new_sha, *sibling_ids),
                )
            github_updated = True
        except Exception:
            log.exception("forget: parent file re-render failed")

    await conn.execute("DELETE FROM captures WHERE id = ?", (child_id,))
    await conn.commit()
    return {
        "id": child_id,
        "kind": kind,
        "github_deleted": github_updated,
        "cascaded_children": [],
    }


async def find_most_recent_id(conn: aiosqlite.Connection) -> int | None:
    async with conn.execute(
        "SELECT id FROM captures ORDER BY id DESC LIMIT 1",
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None
