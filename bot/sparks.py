"""Server-side spark selection + file write.

Replaces the spark step in `.claude/routines/daily.md`. The Routine
remains responsible for echo detection only.

The previous Routine-driven path was unreliable: the cloud-running
model intermittently appended sparks via shell `echo` instead of the
documented Python block, producing run-on Markdown paragraphs.
Server-side write fixes this by removing the model from the file-IO
path entirely.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiosqlite

from bot.config import Settings
from bot.digest.validate import normalize_for_quote_check
from bot.github_sync import (
    fetch_file,
    is_configured as github_configured,
    put_file,
)
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_SPARK

log = logging.getLogger(__name__)

_MIN_LEN = 8
_MAX_LEN = 200
_HEADER = "# sparks\n"


async def _load_candidates(
    conn: aiosqlite.Connection, *, local_date: str,
) -> list[str]:
    async with conn.execute(
        """
        SELECT raw, payload FROM captures
        WHERE local_date = ?
          AND kind IN ('text', 'reflection', 'url', 'voice', 'image', 'pdf')
          AND status = 'done'
        ORDER BY id
        """,
        (local_date,),
    ) as cur:
        rows = list(await cur.fetchall())
    bodies: list[str] = []
    for r in rows:
        body = (r["raw"] or "").strip()
        if body:
            bodies.append(body)
        if r["payload"]:
            try:
                p = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
            scrape = p.get("scrape") or {}
            if isinstance(scrape, dict):
                txt = scrape.get("text")
                if isinstance(txt, str) and txt.strip():
                    bodies.append(txt.strip())
            vision = p.get("vision") or {}
            if isinstance(vision, dict):
                for key in ("ocr", "description"):
                    v = vision.get(key)
                    if isinstance(v, str) and v.strip():
                        bodies.append(v.strip())
            tx = p.get("transcript")
            if isinstance(tx, str) and tx.strip():
                bodies.append(tx.strip())
    return bodies


def _coerce_line(raw: str) -> str:
    """Extract `line` field from a JSON-wrapped LLM response. Returns
    empty string on parse failure rather than raising — caller treats
    empty as 'skip'."""
    if not raw:
        return ""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if not m:
            return ""
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return ""
    if isinstance(obj, dict):
        line = obj.get("line")
        if isinstance(line, str):
            return line.strip()
    return ""


async def select_spark(
    conn: aiosqlite.Connection,
    *,
    local_date: str,
    settings: Settings,
    providers: Providers,
) -> str | None:
    """Pick the sharpest verbatim line from `local_date`'s captures.

    Returns None if there's nothing worth surfacing — caller skips
    appending. Two attempts total: first attempt + one retry,
    both must produce a substring of one capture body.
    """
    bodies = await _load_candidates(conn, local_date=local_date)
    if not bodies:
        return None

    norm_corpus = " ".join(normalize_for_quote_check(b) for b in bodies)
    user_content = (
        f"Date: {local_date}\n\nCapture bodies:\n\n"
        + "\n\n---\n\n".join(bodies)
    )

    for attempt in range(2):
        try:
            response = await call_llm(
                purpose="ingest",
                system_blocks=[VOICE_ORCHURATOR, SYSTEM_SPARK],
                messages=[Message(role="user", content=user_content)],
                max_tokens=200,
                settings=settings, providers=providers, conn=conn,
            )
        except Exception:
            log.exception("select_spark: LLM call failed (attempt %d)", attempt + 1)
            continue
        line = _coerce_line(response.text)
        if not line:
            continue
        if not (_MIN_LEN <= len(line) <= _MAX_LEN):
            log.info("select_spark: rejected length %d", len(line))
            continue
        if normalize_for_quote_check(line) not in norm_corpus:
            log.info("select_spark: not a substring, retry")
            continue
        return line
    return None


_SPARKS_FILENAME = "sparks.md"


def _appended_text(existing: str, *, date: str, line: str) -> str:
    """Return `existing` with `<date> — <line>` appended, preserving
    blank-line spacing between entries. Idempotent: returns `existing`
    unchanged when the entry duplicates the current last line."""
    new_entry = f"{date} — {line.strip()}"
    if existing:
        for prev in reversed(existing.splitlines()):
            if prev.strip():
                if prev == new_entry:
                    return existing
                break
    if not existing:
        return _HEADER + "\n" + new_entry + "\n"
    return existing.rstrip("\n") + "\n\n" + new_entry + "\n"


def append_spark(path: Path, *, date: str, line: str) -> None:
    """Append `<date> — <line>` to sparks.md preserving blank-line spacing.

    Idempotent: re-appending the same `date — line` as the current last
    entry is a no-op.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = _appended_text(existing, date=date, line=line)
    if new_text != existing:
        path.write_text(new_text, encoding="utf-8")


async def daily_sparks_job(
    *,
    conn: aiosqlite.Connection,
    settings: Settings,
    providers: Providers,
    yesterday: str,
) -> bool:
    """Run once per day at SPARKS_LOCAL_TIME. Returns True iff a spark
    was selected and pushed. Silent on no-spark days."""
    if not settings.SPARKS_ENABLED:
        return False
    if not github_configured(settings):
        log.info("daily_sparks_job: github not configured, skipping")
        return False

    line = await select_spark(
        conn, local_date=yesterday,
        settings=settings, providers=providers,
    )
    if not line:
        log.info("daily_sparks_job: no spark for %s", yesterday)
        return False

    fetched = await fetch_file(settings=settings, path=_SPARKS_FILENAME)
    existing, sha = ("", None) if fetched is None else fetched
    new_content = _appended_text(existing, date=yesterday, line=line)
    if new_content == existing:
        log.info("daily_sparks_job: idempotent no-op for %s", yesterday)
        return False

    await put_file(
        settings=settings,
        path=_SPARKS_FILENAME,
        content=new_content,
        message=f"spark {yesterday}",
        existing_sha=sha,
    )
    log.info("daily_sparks_job: appended spark for %s", yesterday)
    return True
