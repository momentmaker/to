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
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

import aiosqlite
import grapheme

from bot.config import Settings
from bot.github_sync import fetch_file, put_file
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_TWEET_STITCH
from bot.tweet_validate import validate_stitch, validate_tweet_total_length

log = logging.getLogger(__name__)


# ---- module-level constants ---------------------------------------------

_TCO_LEN = 23
_TWEET_MAX = 280
_MIN_QUOTE_LEN = 30

_KV_KEY = "pending_tweet_draft"
_LEDGER_FILENAME = "tweeted.json"


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
themes that connect 2-3 of them. A "theme" is a short kebab-case
label (privacy-asymmetry, automation-as-craft, tokens-and-art).
Each proposal lists exactly 2-3 capture ids that share that theme.

Be generous about what counts as a theme. Even loose conceptual
rhymes — a shared mood, a recurring noun, a parallel observation —
qualify. Return between 1 and 5 proposals whenever the pool has
2 or more captures; return [] only when no two captures share
ANY plausible thread (genuinely unrelated topics on different
days).

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
    - status = 'processed'
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
          AND c.status = 'processed'
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
    weekday: str | None = None,
) -> dict | None:
    """Call the tweet-purpose LLM to produce a stitched draft.

    Returns a dict with keys {shape, stitch, lead_quote} or None on
    failure. shape ∈ {"insight", "quote_led", "temporal"}. lead_quote
    is set only when shape == "quote_led" and is expected to be a
    verbatim substring of one capture body.

    `weekday` is a 3-letter lowercase hint ("mon", "tue", ...) that
    tilts the LLM's shape choice (Wed→question texture, Fri→quote_led).
    """
    body_lines = "\n".join(
        f'  ({date}) "{body}"' for date, body in capture_summaries
    )
    parts = [f"Theme: {theme}", "", "Captures:", body_lines]
    if weekday:
        parts.extend(["", f"day-of-week hint: {weekday}"])
    user_content = "\n".join(parts)
    try:
        response = await call_llm(
            purpose="tweet",
            system_blocks=[VOICE_ORCHURATOR, SYSTEM_TWEET_STITCH],
            messages=[Message(role="user", content=user_content)],
            max_tokens=400,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        log.exception("generate_stitch: LLM call failed")
        return None
    obj = _coerce_json(response.text)
    if not isinstance(obj, dict):
        return None
    stitch = obj.get("stitch")
    if not isinstance(stitch, str) or not stitch.strip():
        return None
    shape = obj.get("shape")
    if shape not in ("insight", "quote_led", "temporal"):
        shape = "insight"
    lead_quote = obj.get("lead_quote")
    if not isinstance(lead_quote, str) or not lead_quote.strip():
        lead_quote = None
    return {
        "shape": shape,
        "stitch": stitch.strip(),
        "lead_quote": lead_quote.strip() if lead_quote else None,
    }


def _pick_url(captures: list[dict]) -> str | None:
    """Pick the URL for the tweet's URL line. When multiple captures
    are kind='url', take the oldest. None if no URL captures."""
    url_caps = [c for c in captures if c.get("kind") == "url" and c.get("url")]
    if not url_caps:
        return None
    url_caps.sort(key=lambda c: c.get("local_date") or "")
    return url_caps[0]["url"]


def assemble_tweet(
    *,
    shape: str,
    stitch: str,
    captures: list[dict],
    lead_quote: str | None = None,
) -> str | None:
    """Compose the final tweet text in one of three shapes. Returns the
    rendered tweet string, or None if the result exceeds 280 graphemes.

    Shapes:
        insight  — <stitch>\\n\\n<url?>
        quote_led — "<lead_quote>"\\n\\n<stitch>\\n\\n<url?>
        temporal — <stitch>\\n\\n<url?>     (same render as insight; the
                                              time-gap nuance is in the
                                              stitch text itself)
    """
    if not stitch or not captures or len(captures) < 2:
        return None
    url = _pick_url(captures)

    if shape == "quote_led" and lead_quote:
        body_lines = [f'"{lead_quote.strip()}"', "", stitch.strip()]
    else:
        body_lines = [stitch.strip()]
    if url:
        body_lines.extend(["", url])
    out = "\n".join(body_lines)

    measured = re.sub(r"https?://\S+", "x" * _TCO_LEN, out)
    if grapheme.length(measured) > _TWEET_MAX:
        return None
    return out


class PendingDraft(NamedTuple):
    draft_text: str
    capture_ids: list[int]
    theme: str
    stitch: str
    draft_count: int
    char_count: int
    local_date: str
    created_at: str
    # tweet_id of a prior tweet on the same theme. When set, /post sends
    # this draft as a reply to that tweet, building a visible thread of
    # recurring thoughts on Twitter. Optional / nullable.
    chain_target: str | None = None


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _decode_pending(value: str) -> PendingDraft | None:
    try:
        d = json.loads(value)
        chain_target = d.get("chain_target")
        return PendingDraft(
            draft_text=str(d["draft_text"]),
            capture_ids=[int(x) for x in d["capture_ids"]],
            theme=str(d.get("theme") or ""),
            stitch=str(d.get("stitch") or ""),
            draft_count=int(d.get("draft_count") or 1),
            char_count=int(d.get("char_count") or 0),
            local_date=str(d["local_date"]),
            created_at=str(d.get("created_at") or ""),
            chain_target=str(chain_target) if chain_target else None,
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
    chain_target: str | None = None,
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
        "chain_target": chain_target,
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
    chain_target: str | None = None,
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
        "chain_target": chain_target,
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


async def record_tweet(
    conn: aiosqlite.Connection,
    *,
    tweet_id: str,
    tweeted_at: str,
    local_date: str,
    capture_ids: list[int],
    theme: str | None,
    stitch: str | None,
    text: str,
    draft_count: int,
    edited: bool,
    in_reply_to_tweet_id: str | None = None,
) -> None:
    """Insert a row into the tweets ledger. Idempotent on tweet_id —
    if the same id is submitted twice (e.g. X retried under the hood
    and returned the prior tweet's id), the second call is a no-op
    rather than an IntegrityError that would surface as a generic
    handler failure with the pending state already cleared.

    `in_reply_to_tweet_id` records the chain target so the ledger
    preserves the threading topology (used by future analytics, the
    captures-repo `tweeted.json`, and reconstructing chains if the
    Twitter API is ever lost).
    """
    await conn.execute(
        """
        INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                            theme, stitch, text, draft_count, edited,
                            in_reply_to_tweet_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tweet_id) DO NOTHING
        """,
        (tweet_id, tweeted_at, local_date, json.dumps(capture_ids),
         theme, stitch, text, draft_count, 1 if edited else 0,
         in_reply_to_tweet_id),
    )
    await conn.commit()


async def push_ledger_to_repo(*, settings: Settings, record: dict) -> None:
    """Append `record` to `tweeted.json` at the captures repo root.
    Failure is logged but not raised — SQLite ledger is canonical."""
    try:
        fetched = await fetch_file(settings=settings, path=_LEDGER_FILENAME)
    except Exception:
        log.exception("push_ledger_to_repo: fetch failed")
        return
    if fetched is None:
        existing_arr: list = []
        sha = None
    else:
        try:
            existing_arr = json.loads(fetched[0]) or []
            if not isinstance(existing_arr, list):
                existing_arr = []
        except json.JSONDecodeError:
            log.warning("tweeted.json malformed, starting fresh")
            existing_arr = []
        sha = fetched[1]
    existing_arr.append(record)
    content = json.dumps(existing_arr, indent=2, ensure_ascii=False) + "\n"
    try:
        await put_file(
            settings=settings,
            path=_LEDGER_FILENAME,
            content=content,
            message=f"tweet {record.get('tweet_id', '')}",
            existing_sha=sha,
        )
    except Exception:
        log.exception("push_ledger_to_repo: put failed")


def format_pool_for_themes(pool: list[aiosqlite.Row]) -> str:
    lines = []
    for r in pool[:30]:
        d = dict(r)
        title = ""
        try:
            p = json.loads(r["processed"]) if r["processed"] else None
            if isinstance(p, dict):
                title = (p.get("title") or "").strip()
        except (TypeError, json.JSONDecodeError):
            pass
        # Use the voice-body (LLM summary for URL captures, raw for text)
        # so theme detection has substantive content to rhyme on.
        body = _capture_body_for_voice(d)[:240].replace("\n", " ").strip()
        prefix = f"[{r['id']}] ({r['kind']}) "
        if title and title not in body:
            lines.append(prefix + f"{title}: {body}")
        else:
            lines.append(prefix + body)
    return "\n".join(lines)


async def find_chain_target(
    conn: aiosqlite.Connection, *, theme: str,
) -> str | None:
    """Return the most recent prior tweet's id with the same theme, or
    None if no prior tweet shares it. Used to thread today's draft as
    a self-reply, building visible recurring-thought chains on Twitter.
    """
    if not theme:
        return None
    async with conn.execute(
        "SELECT tweet_id FROM tweets WHERE theme = ? "
        "ORDER BY tweeted_at DESC LIMIT 1",
        (theme,),
    ) as cur:
        row = await cur.fetchone()
    return str(row[0]) if row else None


def render_draft_dm(
    *, draft_text: str, theme: str, char_count: int,
    draft_count: int, cap: int, chain_target: str | None = None,
) -> str:
    chain_line = ""
    if chain_target:
        chain_line = (
            f" · 🧵 reply to https://x.com/i/web/status/{chain_target}"
        )
    return (
        f"draft {draft_count}/{cap}{chain_line}\n\n"
        f"{draft_text}\n\n"
        f"{char_count}/280 chars · theme: {theme}\n\n"
        f"/post   /next   /edit <text>   /skip"
    )


def _capture_body_for_voice(c: dict) -> str:
    """The most useful 'body' representation of a capture for theme
    detection and stitch generation. For URL captures, prefer the LLM
    summary or scrape title over the raw URL string (which carries
    almost no signal). For everything else, use raw.
    """
    if c.get("kind") == "url":
        processed = c.get("processed")
        if isinstance(processed, str):
            try:
                processed = json.loads(processed)
            except json.JSONDecodeError:
                processed = None
        if isinstance(processed, dict):
            for key in ("summary", "title"):
                v = processed.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            quotes = processed.get("quotes")
            if isinstance(quotes, list):
                for q in quotes:
                    if isinstance(q, str) and q.strip():
                        return q.strip()
        payload = c.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            scrape = payload.get("scrape") or {}
            if isinstance(scrape, dict):
                for key in ("title", "text"):
                    v = scrape.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:300]
    return (c.get("raw") or "").strip()


def _is_verbatim_substring(needle: str, captures: list[dict]) -> bool:
    """True iff `needle` appears as a normalized substring of any
    capture's voice body."""
    from bot.digest.validate import normalize_for_quote_check
    needle_n = normalize_for_quote_check(needle)
    if not needle_n:
        return False
    for c in captures:
        body = _capture_body_for_voice(c)
        if not body:
            continue
        if needle_n in normalize_for_quote_check(body):
            return True
    return False


async def try_build_draft(
    *,
    captures: list[aiosqlite.Row],
    theme: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
    weekday: str | None = None,
) -> dict | None:
    """Up to 3 stitch attempts for this capture+theme combination.
    Returns dict {text, stitch, shape, char_count} or None on total
    failure.

    Verbatim invariant: when shape == "quote_led" the LLM-supplied
    lead_quote MUST be a normalized substring of one capture's body.
    Otherwise the attempt is rejected and we retry. assemble_tweet
    itself never invents quote text.
    """
    cap_dicts = [dict(c) for c in captures]
    summaries = [(c["local_date"], _capture_body_for_voice(dict(c))) for c in captures]

    for _ in range(3):
        gen = await generate_stitch(
            theme=theme, capture_summaries=summaries,
            settings=settings, providers=providers, conn=conn,
            weekday=weekday,
        )
        if gen is None:
            continue
        stitch = gen["stitch"]
        shape = gen["shape"]
        lead_quote = gen.get("lead_quote")

        ok, reason = validate_stitch(stitch)
        if not ok:
            log.info("stitch invalid: %s", reason)
            continue

        if shape == "quote_led":
            if not lead_quote:
                log.info("quote_led but lead_quote missing, retry")
                continue
            if not _is_verbatim_substring(lead_quote, cap_dicts):
                log.info("quote_led lead_quote not in any capture, retry")
                continue

        text = assemble_tweet(
            shape=shape, stitch=stitch,
            lead_quote=lead_quote, captures=cap_dicts,
        )
        if text is None:
            continue
        ok2, reason2 = validate_tweet_total_length(text)
        if not ok2:
            log.info("tweet length invalid: %s", reason2)
            continue
        measured = re.sub(r"https?://\S+", "x" * _TCO_LEN, text)
        return {
            "text": text,
            "stitch": stitch,
            "shape": shape,
            "char_count": grapheme.length(measured),
        }
    return None


async def daily_tweet_draft_job(
    *,
    conn: aiosqlite.Connection,
    settings: Settings,
    providers: Providers,
    bot,
    today_iso: str | None = None,
    force: bool = False,
) -> str | None:
    """Cron-driven entry. Returns None iff a draft was generated and DMed.
    Otherwise returns a short failure reason for the caller to surface.

    Pass force=True to bypass the TWEET_DAILY_V2_ENABLED master switch
    (matches the /reflect and /export pattern for explicit user requests).
    """
    if not force and not settings.TWEET_DAILY_V2_ENABLED:
        return "pipeline disabled (TWEET_DAILY_V2_ENABLED=false)"
    if settings.TELEGRAM_OWNER_ID == 0 or bot is None:
        return "no Telegram owner / bot configured"

    today_iso = today_iso or date.today().isoformat()
    weekday = date.fromisoformat(today_iso).strftime("%a").lower()

    await expire_if_stale(conn, today_local=today_iso)
    if not force and await get_pending(conn) is not None:
        log.info("daily_tweet_draft_job: pending draft already present, skipping")
        return "draft already pending"

    pool = await pick_eligible_pool(conn, settings=settings, today_iso=today_iso)
    if len(pool) < 2:
        log.info("daily_tweet_draft_job: pool < 2, no draft")
        # Diagnose: how many captures are flagged tweetable but excluded?
        async with conn.execute(
            "SELECT COUNT(*) FROM captures "
            "WHERE JSON_EXTRACT(payload, '$.tweetable') = 1"
        ) as cur:
            flagged_total = int((await cur.fetchone())[0])
        async with conn.execute(
            "SELECT COUNT(*) FROM captures "
            "WHERE JSON_EXTRACT(payload, '$.tweetable') = 1 "
            "  AND status = 'processed'"
        ) as cur:
            flagged_done = int((await cur.fetchone())[0])
        if flagged_total > flagged_done:
            return (
                f"pool too small ({len(pool)}). "
                f"{flagged_total} captures /tweetable'd but only {flagged_done} "
                "are status=done — others may be still processing or errored. "
                "/status to check"
            )
        return (
            f"pool too small ({len(pool)}, flagged={flagged_total}) — "
            "flag more captures with /tweetable last"
        )

    proposals = await detect_themes(
        pool_summary=format_pool_for_themes(pool),
        settings=settings, providers=providers, conn=conn,
    )
    if not proposals:
        log.info("daily_tweet_draft_job: no theme proposals")
        return "no theme proposals — captures may be too dissimilar"

    chosen = await pick_theme(proposals, conn=conn)
    candidates = [chosen] + [p for p in proposals if p is not chosen]
    pool_by_id = {r["id"]: r for r in pool}

    for proposal in candidates:
        captures = [
            pool_by_id[i] for i in proposal.capture_ids if i in pool_by_id
        ][:2]
        if len(captures) < 2:
            continue
        draft = await try_build_draft(
            captures=captures, theme=proposal.theme,
            settings=settings, providers=providers, conn=conn,
            weekday=weekday,
        )
        if draft is None:
            continue
        chain_target = await find_chain_target(conn, theme=proposal.theme)
        await set_pending(
            conn,
            draft_text=draft["text"],
            capture_ids=[c["id"] for c in captures],
            theme=proposal.theme,
            stitch=draft["stitch"],
            char_count=draft["char_count"],
            local_date=today_iso,
            chain_target=chain_target,
        )
        try:
            await bot.send_message(
                chat_id=settings.TELEGRAM_OWNER_ID,
                text=render_draft_dm(
                    draft_text=draft["text"], theme=proposal.theme,
                    char_count=draft["char_count"], draft_count=1,
                    cap=settings.TWEET_NEXT_CAP,
                    chain_target=chain_target,
                ),
            )
        except Exception:
            log.exception("daily_tweet_draft_job: bot.send_message failed")
            await clear_pending(conn)
            return "Telegram send failed"
        return None

    log.info("daily_tweet_draft_job: no proposal produced a valid draft")
    return "stitch validators rejected all candidates (3 retries each)"
