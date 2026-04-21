"""Weekly digest orchestrator.

Steps:
1. Pick the target week (default: the week that just ended in the user's TZ).
2. Idempotent check — skip if `weekly` row already has status='processed'.
3. Gather captures for that week (captures + their whys + that day's
   reflections). Build a numbered corpus.
4. LLM with [SYSTEM_DIGEST, QUOTE_ONLY_RULES] (no orchurator voice).
5. Parse `{essay, whisper, mark}`. Validate: single-grapheme mark, ≤240-char
   whisper, quote-only essay.
6. On validation failure, re-prompt ONCE with the offenders quoted back.
7. Persist the `weekly` row (status='processed' if final output valid,
   'failed' otherwise).
8. Push `YYYY-wNN/digest.md` + `fz-ax-backup.json` (cumulative) to the
   private repo.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from bot.config import Settings
from bot.digest import fz_state, validate
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.prompts import QUOTE_ONLY_RULES, SYSTEM_DIGEST, SYSTEM_DIGEST_RETRY_SUFFIX
from bot.week import fz_week_idx, iso_week_key, local_date_for, parse_dob

log = logging.getLogger(__name__)


# ------- corpus assembly --------------------------------------------------

def _json_or_empty(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


async def _load_week_captures(
    conn: aiosqlite.Connection, *, fz_week: int,
) -> tuple[list[aiosqlite.Row], dict[int, list[aiosqlite.Row]]]:
    """Return (primary_rows, whys_by_parent_id).

    Primary rows include kind in (text, url, image, voice, reflection) — i.e.
    everything except whys, which hang off their parent.
    """
    async with conn.execute(
        """
        SELECT id, kind, source, url, raw, payload, processed, parent_id,
               created_at, local_date, iso_week_key, fz_week_idx
        FROM captures
        WHERE fz_week_idx = ? AND kind != 'why'
        ORDER BY id
        """,
        (fz_week,),
    ) as cur:
        primary = list(await cur.fetchall())

    async with conn.execute(
        """
        SELECT id, parent_id, raw, created_at
        FROM captures
        WHERE fz_week_idx = ? AND kind = 'why'
        ORDER BY created_at
        """,
        (fz_week,),
    ) as cur:
        whys = list(await cur.fetchall())

    whys_by_parent: dict[int, list[aiosqlite.Row]] = {}
    for w in whys:
        pid = w["parent_id"]
        if pid is None:
            continue
        whys_by_parent.setdefault(int(pid), []).append(w)

    return primary, whys_by_parent


def _format_corpus(
    primary: list[aiosqlite.Row],
    whys_by_parent: dict[int, list[aiosqlite.Row]],
) -> tuple[str, list[str]]:
    """Format the LLM input bundle AND return the flat list of quotable
    strings used by the quote-only validator.
    """
    lines: list[str] = ["Week's fragments:"]
    quotable: list[str] = []

    def _add_quotable(s: str | None) -> None:
        if isinstance(s, str) and s.strip():
            quotable.append(s.strip())

    for i, r in enumerate(primary, 1):
        kind = r["kind"]
        raw = (r["raw"] or "").strip()
        processed = _json_or_empty(r["processed"])
        payload = _json_or_empty(r["payload"])
        title = processed.get("title") if isinstance(processed, dict) else None
        summary = processed.get("summary") if isinstance(processed, dict) else None
        quotes = processed.get("quotes") if isinstance(processed, dict) else None

        header = f"[{i}] ({kind}) {r['local_date']}"
        if title:
            header += f" — {title}"
        lines.append(header)
        if raw:
            lines.append(f"  raw: {raw[:600]}")
            _add_quotable(raw)
        # Scraped article body (URL captures)
        scrape = payload.get("scrape") if isinstance(payload, dict) else None
        if isinstance(scrape, dict) and scrape.get("text"):
            body = str(scrape["text"]).strip()
            lines.append(f"  body: {body[:1200]}")
            _add_quotable(body)
        # Voice transcript
        transcript = payload.get("transcript") if isinstance(payload, dict) else None
        if isinstance(transcript, str) and transcript.strip():
            _add_quotable(transcript)
        # Image OCR + description
        vision = payload.get("vision") if isinstance(payload, dict) else None
        if isinstance(vision, dict):
            ocr = vision.get("ocr") or ""
            desc = vision.get("description") or ""
            if ocr.strip():
                lines.append(f"  ocr: {ocr[:600]}")
                _add_quotable(ocr)
            if desc.strip():
                lines.append(f"  image: {desc[:300]}")
                _add_quotable(desc)
        # Processed extracts (quotes, summary)
        if isinstance(quotes, list):
            for q in quotes:
                if isinstance(q, str) and q.strip():
                    _add_quotable(q)
        if isinstance(summary, str) and summary.strip():
            _add_quotable(summary)

        for w in whys_by_parent.get(int(r["id"]), []):
            why_text = (w["raw"] or "").strip()
            if why_text:
                lines.append(f"  why: {why_text[:400]}")
                _add_quotable(why_text)

    return "\n".join(lines), quotable


# ------- LLM output parsing + validation ----------------------------------

def _coerce_digest_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _extract_single_grapheme(mark: str) -> str:
    """If the LLM returned multiple graphemes, take the first. Fz.ax rejects
    multi-grapheme marks, so we enforce one-ness at our boundary.
    """
    import grapheme as _g
    s = (mark or "").strip()
    if not s:
        return ""
    first = list(_g.graphemes(s))
    return first[0] if first else ""


def _validate_digest_output(
    obj: dict, corpus_quotables: list[str],
) -> tuple[bool, dict[str, Any], list[str]]:
    """Normalize + validate. Returns (ok, clean_obj, offenders)."""
    essay = obj.get("essay") if isinstance(obj.get("essay"), str) else ""
    whisper = obj.get("whisper") if isinstance(obj.get("whisper"), str) else ""
    mark = _extract_single_grapheme(obj.get("mark") or "")

    offenders: list[str] = []
    if not validate.is_single_grapheme(mark):
        offenders.append(f"[mark] '{mark}' is not a single grapheme")
    if not validate.whisper_ok(whisper):
        offenders.append(f"[whisper] length {len(whisper)} chars — must be 1..240")

    essay_ok, essay_offenders = validate.validate_quote_only(essay, corpus_quotables)
    offenders.extend(essay_offenders)

    clean = {"essay": essay.strip(), "whisper": whisper.strip(), "mark": mark}
    ok = not offenders
    return ok, clean, offenders


# ------- target-week arithmetic -------------------------------------------

def _most_recent_full_week(*, settings: Settings) -> int:
    """The fz_week_idx of the week that just ended (Sat or Sun in user TZ)."""
    dob_date = parse_dob(settings.DOB)
    today_local = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    # If WEEK_START=mon and we're running on sat/sun night, the week that
    # just ended is the current fz_week. The scheduled cron fires on the
    # user's chosen day (`WEEKLY_DIGEST_DOW`), so "this week's index" is
    # the right target.
    return fz_week_idx(today_local, dob_date)


# ------- orchestrator -----------------------------------------------------

async def weekly_digest_job(
    *,
    conn: aiosqlite.Connection,
    settings: Settings,
    providers: Providers,
    bot,
    fz_week: int | None = None,
    force: bool = False,
) -> bool:
    """Run the weekly digest for `fz_week` (default: current week).

    Idempotent per week via `weekly.status='processed'`. Pass `force=True`
    from `/export` to regenerate.
    """
    if fz_week is None:
        fz_week = _most_recent_full_week(settings=settings)

    # Idempotent check + load any user-set mark to preserve across the run.
    async with conn.execute(
        "SELECT status, mark FROM weekly WHERE fz_week_idx = ?", (fz_week,),
    ) as cur:
        existing = await cur.fetchone()
    if not force and existing is not None and existing["status"] == "processed":
        log.info("weekly_digest: week %s already processed, skipping", fz_week)
        return False
    # User may have run /setmark earlier in the week — that mark wins over
    # whatever the LLM comes up with. We distinguish a user-set mark from a
    # previously-LLM-generated one by status: /setmark leaves status='pending',
    # while a prior LLM run leaves status='processed'.
    user_mark_override = (
        existing["mark"]
        if (existing is not None
            and existing["mark"]
            and existing["status"] != "processed")
        else None
    )

    primary, whys_by_parent = await _load_week_captures(conn, fz_week=fz_week)
    if not primary:
        log.info("weekly_digest: zero captures in week %s, skipping", fz_week)
        return False

    corpus_text, quotables = _format_corpus(primary, whys_by_parent)
    dob_date = parse_dob(settings.DOB)
    today_local = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    iso_week = iso_week_key(today_local)

    messages: list[Message] = [Message(role="user", content=corpus_text)]
    clean: dict[str, Any] = {}
    all_offenders: list[str] = []
    ok = False

    for attempt in range(2):  # initial + 1 retry
        try:
            response = await call_llm(
                purpose="digest",
                system_blocks=[SYSTEM_DIGEST, QUOTE_ONLY_RULES],
                messages=messages,
                max_tokens=4096,
                settings=settings, providers=providers, conn=conn,
            )
        except Exception:
            log.exception("weekly_digest: LLM call failed on attempt %s", attempt + 1)
            break

        parsed = _coerce_digest_json(response.text) or {}
        ok, clean, offenders = _validate_digest_output(parsed, quotables)
        if ok:
            break
        all_offenders = offenders
        log.warning(
            "weekly_digest: validation failed on attempt %s with %s offender(s)",
            attempt + 1, len(offenders),
        )
        retry_msg = SYSTEM_DIGEST_RETRY_SUFFIX.format(
            offenders="\n".join(f"- {o[:200]}" for o in offenders[:10])
        )
        messages = [
            Message(role="user", content=corpus_text),
            Message(role="assistant", content=response.text or ""),
            Message(role="user", content=retry_msg),
        ]

    # Persist the weekly row. A user-set mark (via /setmark) wins over the
    # LLM's suggestion.
    if user_mark_override:
        clean["mark"] = user_mark_override
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    status = "processed" if ok else "failed"
    await conn.execute(
        """
        INSERT INTO weekly (
            fz_week_idx, iso_week_key, essay, whisper, mark, marked_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fz_week_idx) DO UPDATE SET
          iso_week_key = excluded.iso_week_key,
          essay = excluded.essay,
          whisper = excluded.whisper,
          mark = excluded.mark,
          marked_at = excluded.marked_at,
          status = excluded.status
        """,
        (fz_week, iso_week, clean.get("essay", ""), clean.get("whisper", ""),
         clean.get("mark", ""), now_iso, status),
    )
    await conn.commit()

    if not ok:
        try:
            from bot.notify import send_alert
            await send_alert(
                f"weekly digest for week {fz_week} failed quote-only validation "
                f"({len(all_offenders)} offender(s)); marked failed",
                severity="warning",
            )
        except Exception:
            pass
        return False

    # Push digest.md + cumulative fz-ax-backup.json
    from bot import github_sync
    if github_sync.is_configured(settings):
        await _push_weekly_artifacts(
            conn=conn, settings=settings, fz_week=fz_week,
            iso_week=iso_week, clean=clean,
        )

    # DM the owner the whisper + mark as a short heads-up
    try:
        await bot.send_message(
            chat_id=settings.TELEGRAM_OWNER_ID,
            text=f"{clean['mark']}  {clean['whisper']}",
        )
    except Exception:
        log.exception("weekly_digest: failed to DM owner the whisper")

    # Optional weekly tweet — only if user opted in + OAuth configured.
    from bot import tweet as tweet_mod
    if tweet_mod.is_configured_for_weekly(settings):
        try:
            tweet_text = await tweet_mod.generate_weekly_tweet(
                mark=clean.get("mark", ""),
                whisper=clean.get("whisper", ""),
                essay=clean.get("essay", ""),
                settings=settings, providers=providers, conn=conn,
            )
            if tweet_text:
                result = await tweet_mod.post_tweet(tweet_text, settings=settings)
                if result is not None:
                    tweet_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    await conn.execute(
                        "UPDATE weekly SET tweet_text = ?, tweet_posted_at = ? "
                        "WHERE fz_week_idx = ?",
                        (tweet_text, tweet_iso, fz_week),
                    )
                    await conn.commit()
        except Exception:
            log.exception("weekly_digest: tweet step failed")

    return True


async def _push_weekly_artifacts(
    *, conn: aiosqlite.Connection, settings: Settings,
    fz_week: int, iso_week: str, clean: dict[str, Any],
) -> None:
    from bot import github_sync

    # 1. digest.md
    week_dir = iso_week.replace("W", "w")
    md_path = f"{week_dir}/digest.md"
    digest_md = _render_digest_md(iso_week, clean)
    try:
        # We don't track digest.md's sha separately — overwrite if it exists
        # by reading its sha, or create fresh. Simpler: always PUT without sha;
        # on 422 (conflict) the GitHub API returns the existing sha and we retry.
        sha = await _put_with_auto_sha(
            settings=settings, path=md_path, content=digest_md,
            message=f"digest: week {iso_week}",
        )
        async with conn.execute(
            "UPDATE weekly SET github_sha = ? WHERE fz_week_idx = ?",
            (sha, fz_week),
        ) as _:
            pass
        await conn.commit()
    except Exception:
        log.exception("weekly_digest: digest.md push failed")

    # 2. fz-ax-backup.json
    try:
        state = await fz_state.build_fz_state(conn=conn, settings=settings)
        serialized = fz_state.serialize(state)
        # Use kv to track the single shared file's sha
        prev_sha = await _get_kv_text(conn, "fz_backup_sha")
        new_sha = await github_sync.put_file(
            settings=settings,
            path="fz-ax-backup.json",
            content=serialized,
            message=f"fz-ax: update through week {iso_week}",
            existing_sha=prev_sha,
        )
        await _set_kv_text(conn, "fz_backup_sha", new_sha)
        await conn.execute(
            "UPDATE weekly SET fz_export_sha = ? WHERE fz_week_idx = ?",
            (new_sha, fz_week),
        )
        await conn.commit()
    except Exception:
        log.exception("weekly_digest: fz-ax-backup.json push failed")


async def _put_with_auto_sha(
    *, settings, path: str, content: str, message: str,
) -> str:
    """PUT a file whose existing sha we don't track locally. If the file
    exists on GitHub, fetch its current sha first and include it.
    """
    from bot import github_sync
    import httpx

    owner_repo = settings.GITHUB_REPO
    url = f"https://api.github.com/repos/{owner_repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Probe for existing file
        try:
            probe = await client.get(url, headers=headers,
                                     params={"ref": settings.GITHUB_BRANCH})
        except httpx.TimeoutException:
            probe = None
        existing_sha = None
        if probe is not None and probe.status_code == 200:
            existing_sha = probe.json().get("sha")
        return await github_sync.put_file(
            settings=settings, path=path, content=content,
            message=message, existing_sha=existing_sha, client=client,
        )


def _render_digest_md(iso_week: str, clean: dict[str, Any]) -> str:
    lines = [
        f"# {iso_week}",
        "",
        f"**{clean.get('mark', '')}**  _{clean.get('whisper', '')}_",
        "",
        clean.get("essay", "").strip(),
        "",
    ]
    return "\n".join(lines)


async def _get_kv_text(conn: aiosqlite.Connection, key: str) -> str | None:
    async with conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0]) if row[0] else None
    except json.JSONDecodeError:
        return None


async def _set_kv_text(conn: aiosqlite.Connection, key: str, value: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await conn.execute(
        """
        INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value), now_iso),
    )
    await conn.commit()
