"""Commit captures to the user's private GitHub repo.

GitHub REST API: PUT /repos/{owner}/{repo}/contents/{path}
  - body: {message, content (base64), branch, sha? (for update)}
  - returns: {content: {sha, path, ...}}

Why captures don't get their own file — their text is rendered inline into
their parent's markdown. Updating a why re-PUTs the parent file with the
current list of whys attached.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import aiosqlite
import httpx

from bot.config import Settings
from bot.markdown_out import file_path_for, render_capture_markdown

log = logging.getLogger(__name__)


_API_BASE = "https://api.github.com"
_TIMEOUT = 20.0
_MAX_RETRIES = 4  # attempts: 1 + 3 retries
_BACKOFF_BASE_S = 1.0


def is_configured(settings: Settings) -> bool:
    return bool(settings.GITHUB_TOKEN and settings.GITHUB_REPO)


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "to-commonplace-bot",
    }


async def put_file(
    *,
    settings: Settings,
    path: str,
    content: str,
    message: str,
    existing_sha: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """PUT file contents. Returns the new sha.

    Retries with exponential backoff on 5xx and timeouts. Does NOT retry on
    4xx (auth failure, sha conflict) — those need human intervention.
    """
    owner_repo = settings.GITHUB_REPO
    url = f"{_API_BASE}/repos/{owner_repo}/contents/{path}"
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": settings.GITHUB_BRANCH,
    }
    if existing_sha:
        body["sha"] = existing_sha

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        last_status: int | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.put(url, headers=_headers(settings), json=body)
            except httpx.TimeoutException:
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                continue
            if 500 <= resp.status_code < 600:
                last_status = resp.status_code
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["content"]["sha"]
        raise RuntimeError(
            f"github PUT {path} failed after {_MAX_RETRIES} attempts "
            f"(last status={last_status})"
        )
    finally:
        if owned:
            await client.aclose()


async def _fetch_rows(conn: aiosqlite.Connection, query: str, args: tuple) -> list[aiosqlite.Row]:
    async with conn.execute(query, args) as cur:
        return list(await cur.fetchall())


async def _fetch_row(conn: aiosqlite.Connection, query: str, args: tuple) -> aiosqlite.Row | None:
    async with conn.execute(query, args) as cur:
        return await cur.fetchone()


def _commit_message(row: Any) -> str:
    kind = row["kind"]
    date = row["local_date"]
    if kind == "why":
        return f"why update {date} (capture {row['id']})"
    return f"{kind} {date} (capture {row['id']})"


async def push_capture(
    capture_id: int,
    *,
    settings: Settings,
    conn: aiosqlite.Connection,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Push a single capture (or update its parent for whys).

    Returns True on success, False if skipped (github not configured, or a
    why whose parent isn't synced yet — nightly_sync will retry).

    Raises on actual errors (network/auth/repo issues) so callers can log.
    """
    if not is_configured(settings):
        log.debug("github not configured; skipping push for capture %s", capture_id)
        return False

    row = await _fetch_row(conn, "SELECT * FROM captures WHERE id = ?", (capture_id,))
    if row is None:
        log.warning("capture %s not found, cannot push", capture_id)
        return False

    if row["kind"] == "why":
        return await _push_why(row, settings=settings, conn=conn, client=client)

    whys = await _fetch_rows(
        conn,
        "SELECT * FROM captures WHERE parent_id = ? AND kind = 'why' ORDER BY created_at",
        (capture_id,),
    )
    markdown = render_capture_markdown(row, why_children=whys)
    path = file_path_for(row)
    sha = await put_file(
        settings=settings,
        path=path,
        content=markdown,
        message=_commit_message(row),
        existing_sha=row["github_sha"],
        client=client,
    )
    await conn.execute(
        "UPDATE captures SET github_sha = ? WHERE id = ?",
        (sha, capture_id),
    )
    # Whys that were bundled into this file share the sha.
    if whys:
        why_ids = [w["id"] for w in whys]
        placeholders = ",".join("?" for _ in why_ids)
        await conn.execute(
            f"UPDATE captures SET github_sha = ? WHERE id IN ({placeholders})",
            (sha, *why_ids),
        )
    await conn.commit()
    return True


async def _push_why(
    why_row: Any, *, settings: Settings, conn: aiosqlite.Connection,
    client: httpx.AsyncClient | None = None,
) -> bool:
    parent_id = why_row["parent_id"]
    if parent_id is None:
        log.info("why %s has no parent_id, nothing to update", why_row["id"])
        return False
    parent = await _fetch_row(conn, "SELECT * FROM captures WHERE id = ?", (parent_id,))
    if parent is None:
        log.warning("parent %s of why %s missing", parent_id, why_row["id"])
        return False
    if not parent["github_sha"]:
        log.info(
            "parent %s not yet synced; deferring why %s to nightly_sync",
            parent_id, why_row["id"],
        )
        return False
    # Push through the parent — _fetch_rows picks up all siblings including this why.
    return await push_capture(parent_id, settings=settings, conn=conn, client=client)


async def unsynced_capture_ids(conn: aiosqlite.Connection) -> list[int]:
    """Captures still needing a GitHub push. Orders parents first so their
    files exist before any orphaned why tries to update them.
    """
    async with conn.execute(
        """
        SELECT id FROM captures
        WHERE github_sha IS NULL
        ORDER BY (kind = 'why') ASC, id ASC
        """
    ) as cur:
        rows = await cur.fetchall()
    return [int(r[0]) for r in rows]
