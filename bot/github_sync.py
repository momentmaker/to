"""Commit captures to the user's private GitHub repo.

GitHub REST API: PUT /repos/{owner}/{repo}/contents/{path}
  - body: {message, content (base64), branch, sha? (for update)}
  - returns: {content: {sha, path, ...}}

Why and highlight captures don't get their own file — their text is rendered
inline into their parent's markdown. Updating either re-PUTs the parent file
with the current list of whys + highlights attached.
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
from bot.markdown_out import asset_path_for, file_path_for, render_capture_markdown

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


async def fetch_file(
    *,
    settings: Settings,
    path: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str] | None:
    """GET a file from the repo. Returns (utf-8 content, sha) or None on 404.

    Raises on auth/repo errors so callers don't confuse them with "not found".
    Use fetch_file_sha for binary files — calling .decode('utf-8') on a JPEG
    will blow up.
    """
    owner_repo = settings.GITHUB_REPO
    url = f"{_API_BASE}/repos/{owner_repo}/contents/{path}"
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.get(
            url,
            headers=_headers(settings),
            params={"ref": settings.GITHUB_BRANCH},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, str(data["sha"])
    finally:
        if owned:
            await client.aclose()


async def fetch_file_sha(
    *,
    settings: Settings,
    path: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """GET just the sha of a file (any type). Returns None on 404. Skips the
    body decode, so safe for binary files like image assets."""
    owner_repo = settings.GITHUB_REPO
    url = f"{_API_BASE}/repos/{owner_repo}/contents/{path}"
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.get(
            url,
            headers=_headers(settings),
            params={"ref": settings.GITHUB_BRANCH},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return str(resp.json()["sha"])
    finally:
        if owned:
            await client.aclose()


async def delete_file(
    *,
    settings: Settings,
    path: str,
    sha: str,
    message: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """DELETE file contents on GitHub. Returns True on success.

    Retries on 5xx/timeout. Does NOT retry on 4xx — 404 (already gone) and
    409/422 (sha conflict from a local edit) need operator attention, not
    blind retry.
    """
    owner_repo = settings.GITHUB_REPO
    url = f"{_API_BASE}/repos/{owner_repo}/contents/{path}"
    body: dict[str, Any] = {
        "message": message,
        "sha": sha,
        "branch": settings.GITHUB_BRANCH,
    }

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.request(
                    "DELETE", url, headers=_headers(settings), json=body,
                )
            except httpx.TimeoutException:
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                continue
            if 500 <= resp.status_code < 600:
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                continue
            if resp.status_code == 404:
                # Already gone on GitHub — nothing to do, success.
                return True
            resp.raise_for_status()
            return True
        return False
    finally:
        if owned:
            await client.aclose()


async def _put_raw(
    *,
    settings: Settings,
    path: str,
    content_b64: str,
    message: str,
    existing_sha: str | None,
    client: httpx.AsyncClient | None,
) -> str:
    """Shared PUT-with-retries against the contents API. Caller supplies
    already-base64-encoded content (so this works for both text and binary)."""
    owner_repo = settings.GITHUB_REPO
    url = f"{_API_BASE}/repos/{owner_repo}/contents/{path}"
    body: dict[str, Any] = {
        "message": message,
        "content": content_b64,
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


async def put_file(
    *,
    settings: Settings,
    path: str,
    content: str,
    message: str,
    existing_sha: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """PUT a text file. Returns the new sha. UTF-8 encodes before base64."""
    return await _put_raw(
        settings=settings,
        path=path,
        content_b64=base64.b64encode(content.encode("utf-8")).decode("ascii"),
        message=message,
        existing_sha=existing_sha,
        client=client,
    )


async def put_binary_file(
    *,
    settings: Settings,
    path: str,
    content: bytes,
    message: str,
    existing_sha: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """PUT a binary file (image, etc.). Bytes go straight to base64 — no
    UTF-8 encoding step that would corrupt non-text payloads."""
    return await _put_raw(
        settings=settings,
        path=path,
        content_b64=base64.b64encode(content).decode("ascii"),
        message=message,
        existing_sha=existing_sha,
        client=client,
    )


async def _put_asset_idempotent(
    *,
    settings: Settings,
    path: str,
    content: bytes,
    message: str,
    client: httpx.AsyncClient | None,
) -> str:
    """Upload a photo asset, tolerating "file already exists" from a prior
    partial sync. We don't track an asset sha per capture, so on 422 we
    fetch the live sha and retry once. Asset content is deterministic per
    (capture id, slug), so the second PUT is safe.
    """
    try:
        return await put_binary_file(
            settings=settings, path=path, content=content,
            message=message, client=client,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 422:
            raise
        # 422 = sha mismatch / file exists. Fetch existing sha and retry.
        # Use fetch_file_sha to avoid the utf-8 decode in fetch_file (would
        # blow up on a JPEG).
        existing_sha = await fetch_file_sha(
            settings=settings, path=path, client=client,
        )
        if existing_sha is None:
            # Race: file gone between PUT and GET. Try a clean PUT once more.
            return await put_binary_file(
                settings=settings, path=path, content=content,
                message=message, client=client,
            )
        return await put_binary_file(
            settings=settings, path=path, content=content,
            message=message, existing_sha=existing_sha, client=client,
        )


async def _fetch_rows(conn: aiosqlite.Connection, query: str, args: tuple) -> list[aiosqlite.Row]:
    async with conn.execute(query, args) as cur:
        return list(await cur.fetchall())


async def _fetch_row(conn: aiosqlite.Connection, query: str, args: tuple) -> aiosqlite.Row | None:
    async with conn.execute(query, args) as cur:
        return await cur.fetchone()


def _commit_message(row: Any) -> str:
    kind = row["kind"]
    date = row["local_date"]
    if kind in ("why", "highlight"):
        return f"{kind} update {date} (capture {row['id']})"
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

    if row["kind"] in ("why", "highlight"):
        return await _push_child(row, settings=settings, conn=conn, client=client)

    whys = await _fetch_rows(
        conn,
        "SELECT * FROM captures WHERE parent_id = ? AND kind = 'why' ORDER BY created_at",
        (capture_id,),
    )
    highlights = await _fetch_rows(
        conn,
        "SELECT * FROM captures WHERE parent_id = ? AND kind = 'highlight' ORDER BY created_at",
        (capture_id,),
    )
    markdown = render_capture_markdown(
        row, why_children=whys, highlight_children=highlights,
    )
    path = file_path_for(row)

    # Push the photo asset (if any) BEFORE the .md, so a viewer following
    # the .md's `asset = "..."` reference never lands on a missing file.
    asset_path = asset_path_for(row)
    if asset_path:
        await _put_asset_idempotent(
            settings=settings,
            path=asset_path,
            content=bytes(row["asset_bytes"]),
            message=f"asset {row['local_date']} (capture {row['id']})",
            client=client,
        )

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
    # Whys and highlights bundled into this file share the sha.
    child_ids = [r["id"] for r in whys] + [r["id"] for r in highlights]
    if child_ids:
        placeholders = ",".join("?" for _ in child_ids)
        await conn.execute(
            f"UPDATE captures SET github_sha = ? WHERE id IN ({placeholders})",
            (sha, *child_ids),
        )
    await conn.commit()
    return True


async def _push_child(
    child_row: Any, *, settings: Settings, conn: aiosqlite.Connection,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Push a why or highlight by routing through its parent — the child
    renders inline in the parent's file, so what we're really doing is
    rewriting that file with the current list of children."""
    kind = child_row["kind"]
    parent_id = child_row["parent_id"]
    if parent_id is None:
        log.info("%s %s has no parent_id, nothing to update", kind, child_row["id"])
        return False
    parent = await _fetch_row(conn, "SELECT * FROM captures WHERE id = ?", (parent_id,))
    if parent is None:
        log.warning("parent %s of %s %s missing", parent_id, kind, child_row["id"])
        return False
    if not parent["github_sha"]:
        log.info(
            "parent %s not yet synced; deferring %s %s to nightly_sync",
            parent_id, kind, child_row["id"],
        )
        return False
    return await push_capture(parent_id, settings=settings, conn=conn, client=client)


async def unsynced_capture_ids(conn: aiosqlite.Connection) -> list[int]:
    """Captures still needing a GitHub push. Orders parents first so their
    files exist before any orphaned child (why or highlight) tries to
    update them.
    """
    async with conn.execute(
        """
        SELECT id FROM captures
        WHERE github_sha IS NULL
        ORDER BY (kind IN ('why', 'highlight')) ASC, id ASC
        """
    ) as cur:
        rows = await cur.fetchall()
    return [int(r[0]) for r in rows]
