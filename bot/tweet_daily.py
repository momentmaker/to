"""Daily tweet pipeline: pick captures, find a theme, generate a stitch,
draft a tweet, gate on Telegram approval, post to X, ledger.

See `docs/superpowers/specs/2026-05-03-sparks-fix-and-daily-tweet-design.md`
for the full design.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import aiosqlite

from bot.config import Settings
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm

log = logging.getLogger(__name__)


@dataclass
class ThemeProposal:
    theme: str
    capture_ids: list[int]
    rationale: str


def _coerce_json(raw: str) -> Any:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]|\{.*\}", s, flags=re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


_THEME_DETECTION_PROMPT = """\
You read a pool of recent commonplace-book captures and propose
themes that connect 2-3 of them. Return between 0 and 5 proposals.
A "theme" is a short kebab-case label (privacy-asymmetry,
automation-as-craft). Each proposal lists exactly 2-3 capture ids
that share that theme.

Skip thin connections. Better to return [] than to pad with weak
rhymes.

Reply with JSON only — an array, no prose:

    [{"theme": "<label>", "capture_ids": [<id>, <id>],
      "rationale": "<one short sentence>"}]
"""


async def pick_eligible_pool(
    conn: aiosqlite.Connection,
    *,
    settings: Settings,
    today_iso: str | None = None,
) -> list[aiosqlite.Row]:
    """Captures eligible for tweeting today.

    Filters (all must pass):
    - kind in (text, url, voice, image, pdf, reflection)
    - status = 'done'
    - payload.tweetable == true (JSON1)
    - id not present in tweets.capture_ids of any past tweet
    - local_date within last TWEET_POOL_DAYS — unless that yields <2,
      in which case fall back to the full corpus.
    """
    today_iso = today_iso or date.today().isoformat()
    today = date.fromisoformat(today_iso)
    window_start = (today - timedelta(days=settings.TWEET_POOL_DAYS)).isoformat()

    base_query = """
        SELECT c.* FROM captures c
        WHERE c.kind IN ('text', 'url', 'voice', 'image', 'pdf', 'reflection')
          AND c.status = 'done'
          AND JSON_EXTRACT(c.payload, '$.tweetable') = 1
          AND c.id NOT IN (
              SELECT json_each.value
              FROM tweets, json_each(tweets.capture_ids)
          )
    """

    async with conn.execute(
        base_query
        + " AND c.local_date >= ? ORDER BY c.local_date DESC, c.id DESC",
        (window_start,),
    ) as cur:
        recent = list(await cur.fetchall())
    if len(recent) >= 2:
        return recent

    async with conn.execute(
        base_query + " ORDER BY c.local_date DESC, c.id DESC",
    ) as cur:
        return list(await cur.fetchall())


async def detect_themes(
    *,
    pool_summary: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> list[ThemeProposal]:
    try:
        response = await call_llm(
            purpose="ingest",
            system_blocks=[_THEME_DETECTION_PROMPT],
            messages=[Message(role="user", content=pool_summary)],
            max_tokens=600,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        log.exception("detect_themes: LLM call failed")
        return []
    data = _coerce_json(response.text)
    if not isinstance(data, list):
        return []
    out: list[ThemeProposal] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme") or "").strip()
        ids = item.get("capture_ids") or []
        if not theme or not isinstance(ids, list):
            continue
        try:
            ids_int = [int(x) for x in ids]
        except (TypeError, ValueError):
            continue
        if not (2 <= len(ids_int) <= 3):
            continue
        out.append(ThemeProposal(
            theme=theme,
            capture_ids=ids_int,
            rationale=str(item.get("rationale") or ""),
        ))
    return out


async def pick_theme(
    proposals: list[ThemeProposal],
    *,
    conn: aiosqlite.Connection,
) -> ThemeProposal | None:
    """Pick the proposal whose theme has been used least often in the
    ledger. Ties broken by proposal order (LLM ranking)."""
    if not proposals:
        return None
    histogram: dict[str, int] = {}
    async with conn.execute(
        "SELECT theme, COUNT(*) FROM tweets "
        "WHERE theme IS NOT NULL GROUP BY theme"
    ) as cur:
        for row in await cur.fetchall():
            histogram[str(row[0])] = int(row[1])

    def usage(p: ThemeProposal) -> tuple[int, int]:
        return histogram.get(p.theme, 0), proposals.index(p)

    return sorted(proposals, key=usage)[0]
