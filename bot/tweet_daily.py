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
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_TWEET_STITCH

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


async def generate_stitch(
    *,
    theme: str,
    capture_summaries: list[tuple[str, str]],
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> str:
    """Call the tweet-purpose LLM to produce one stitch sentence.
    `capture_summaries` is a list of (date, body) tuples.
    Returns "" on any failure (caller should retry or abandon)."""
    body_lines = "\n".join(
        f'  ({date}) "{body}"' for date, body in capture_summaries
    )
    user_content = f"Theme: {theme}\n\nCaptures:\n{body_lines}"
    try:
        response = await call_llm(
            purpose="tweet",
            system_blocks=[VOICE_ORCHURATOR, SYSTEM_TWEET_STITCH],
            messages=[Message(role="user", content=user_content)],
            max_tokens=200,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        log.exception("generate_stitch: LLM call failed")
        return ""
    obj = _coerce_json(response.text)
    if not isinstance(obj, dict):
        return ""
    s = obj.get("stitch")
    return s.strip() if isinstance(s, str) else ""


import grapheme

_TCO_LEN = 23
_TWEET_MAX = 280
_MIN_QUOTE_LEN = 30


def _word_truncate(text: str, max_len: int) -> str:
    """Truncate `text` to ≤ max_len graphemes at a word boundary.
    Returns empty string when max_len < 1."""
    if max_len < 1:
        return ""
    if grapheme.length(text) <= max_len:
        return text
    chars = list(grapheme.graphemes(text))
    cut = "".join(chars[:max_len])
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip()


def assemble_tweet(
    *,
    stitch: str,
    captures: list[dict],
) -> str | None:
    """Compose the final tweet text. Returns None if the captures cannot
    be made to fit (any required quote would shrink below 30 chars).

    Format:
        <stitch>

        — "<quote 1>" (YYYY-MM-DD)
        — "<quote 2>" (YYYY-MM-DD)
        [<url>]
    """
    if not stitch or not captures or len(captures) < 2:
        return None
    cap_pair = captures[:2]

    url_caps = [c for c in cap_pair if c.get("kind") == "url" and c.get("url")]
    url = None
    if url_caps:
        url_caps.sort(key=lambda c: c.get("local_date") or "")
        url = url_caps[0]["url"]

    overhead_per_line = 18
    overhead_total = grapheme.length(stitch) + 2 + (overhead_per_line * 2)
    if url:
        overhead_total += 1 + _TCO_LEN

    available = _TWEET_MAX - overhead_total
    if available < _MIN_QUOTE_LEN * 2:
        return None

    bodies = [(c.get("raw") or "").strip() for c in cap_pair]
    if not all(bodies):
        return None
    body_lens = [grapheme.length(b) for b in bodies]
    if sum(body_lens) == 0:
        return None

    # Iterative shortest-first allocation:
    # - Process bodies in length-ascending order so short bodies can stay
    #   verbatim and yield their unused budget to longer ones.
    # - For each body, reserve MIN_QUOTE_LEN per remaining body so the
    #   final quota is never forced below the floor on a long body.
    indexed = sorted(range(len(bodies)), key=lambda i: body_lens[i])
    remaining_budget = available
    truncated_by_idx: dict[int, str] = {}
    for n, idx in enumerate(indexed):
        bodies_left_after = len(indexed) - n - 1
        fair_max = remaining_budget - bodies_left_after * _MIN_QUOTE_LEN
        if fair_max < _MIN_QUOTE_LEN and body_lens[idx] > fair_max:
            return None  # would force a long body below the floor
        quota = min(body_lens[idx], fair_max)
        if quota >= body_lens[idx]:
            t = bodies[idx]
        else:
            t = _word_truncate(bodies[idx], quota)
            if grapheme.length(t) < _MIN_QUOTE_LEN:
                return None
        truncated_by_idx[idx] = t
        remaining_budget -= grapheme.length(t)
    truncated = [truncated_by_idx[i] for i in range(len(bodies))]

    lines = [stitch.strip(), ""]
    for body, cap in zip(truncated, cap_pair):
        lines.append(f'— "{body}" ({cap["local_date"]})')
    if url:
        lines.append(url)
    out = "\n".join(lines)

    measured = re.sub(r"https?://\S+", "x" * _TCO_LEN, out)
    if grapheme.length(measured) > _TWEET_MAX:
        return None
    return out


from datetime import datetime, timezone
from typing import NamedTuple

_KV_KEY = "pending_tweet_draft"


class PendingDraft(NamedTuple):
    draft_text: str
    capture_ids: list[int]
    theme: str
    stitch: str
    draft_count: int
    char_count: int
    local_date: str
    created_at: str


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _decode_pending(value: str) -> PendingDraft | None:
    try:
        d = json.loads(value)
        return PendingDraft(
            draft_text=str(d["draft_text"]),
            capture_ids=[int(x) for x in d["capture_ids"]],
            theme=str(d.get("theme") or ""),
            stitch=str(d.get("stitch") or ""),
            draft_count=int(d.get("draft_count") or 1),
            char_count=int(d.get("char_count") or 0),
            local_date=str(d["local_date"]),
            created_at=str(d.get("created_at") or ""),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


async def set_pending(
    conn: aiosqlite.Connection,
    *,
    draft_text: str,
    capture_ids: list[int],
    theme: str,
    stitch: str,
    char_count: int,
    local_date: str,
) -> None:
    payload = {
        "draft_text": draft_text,
        "capture_ids": capture_ids,
        "theme": theme,
        "stitch": stitch,
        "draft_count": 1,
        "char_count": char_count,
        "local_date": local_date,
        "created_at": _utcnow_iso(),
    }
    now = _utcnow_iso()
    await conn.execute(
        """
        INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE
          SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (_KV_KEY, json.dumps(payload), now),
    )
    await conn.commit()


async def get_pending(conn: aiosqlite.Connection) -> PendingDraft | None:
    async with conn.execute(
        "SELECT value FROM kv WHERE key = ?", (_KV_KEY,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    pending = _decode_pending(row[0])
    if pending is None:
        log.warning("corrupt pending_tweet_draft row, clearing")
        await clear_pending(conn)
    return pending


async def update_for_next(
    conn: aiosqlite.Connection,
    *,
    draft_text: str,
    capture_ids: list[int],
    theme: str,
    stitch: str,
    char_count: int,
) -> int | None:
    """Atomic UPDATE of an existing pending row. Returns the new
    draft_count, or None if no row exists. Preserves local_date and
    created_at so midnight expiry still applies to the original day."""
    cur = await get_pending(conn)
    if cur is None:
        return None
    new_count = cur.draft_count + 1
    payload = {
        "draft_text": draft_text,
        "capture_ids": capture_ids,
        "theme": theme,
        "stitch": stitch,
        "draft_count": new_count,
        "char_count": char_count,
        "local_date": cur.local_date,
        "created_at": cur.created_at,
    }
    async with conn.execute(
        """
        UPDATE kv SET value = ?, updated_at = ?
        WHERE key = ?
        RETURNING value
        """,
        (json.dumps(payload), _utcnow_iso(), _KV_KEY),
    ) as c:
        row = await c.fetchone()
    await conn.commit()
    return new_count if row is not None else None


async def clear_pending(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM kv WHERE key = ?", (_KV_KEY,))
    await conn.commit()


async def consume_for_post(conn: aiosqlite.Connection) -> PendingDraft | None:
    """Atomic DELETE...RETURNING. Mirrors bot/why.py pattern."""
    async with conn.execute(
        "DELETE FROM kv WHERE key = ? RETURNING value", (_KV_KEY,),
    ) as cur:
        row = await cur.fetchone()
    await conn.commit()
    if row is None:
        return None
    return _decode_pending(row[0])


async def expire_if_stale(
    conn: aiosqlite.Connection, *, today_local: str,
) -> bool:
    """Drop pending draft if its local_date is < today. Returns True
    iff a row was dropped."""
    pending = await get_pending(conn)
    if pending is None:
        return False
    if pending.local_date < today_local:
        await clear_pending(conn)
        log.info(
            "expire_if_stale: dropped tweet draft from %s", pending.local_date,
        )
        return True
    return False
