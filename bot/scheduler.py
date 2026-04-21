"""APScheduler jobs.

Stage 3:
  - process_pending: every 60s, retry any capture stuck in status='pending'
    for more than 30s (LLM processing failed or was cancelled mid-run).
  - nightly_sync: 03:00 local, push captures missing github_sha.

Stages 4-7 add daily_prompt and weekly_digest.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot import github_sync, process, reflection
from bot.config import Settings
from bot.digest import weekly as digest_weekly
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_DAILY
from bot.week import fz_week_idx, iso_week_key, local_date_for, parse_dob

log = logging.getLogger(__name__)


async def _select_pending(conn: aiosqlite.Connection, *, older_than_seconds: int = 30) -> list[aiosqlite.Row]:
    """Captures still in status='pending' beyond the grace period."""
    cutoff_iso = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    # SQLite date math on ISO strings works because ISO-8601 sorts lexicographically.
    async with conn.execute(
        """
        SELECT id, kind, raw, payload, parent_id
        FROM captures
        WHERE status = 'pending'
          AND datetime(created_at) <= datetime(?, ?)
        ORDER BY id
        """,
        (cutoff_iso, f"-{older_than_seconds} seconds"),
    ) as cur:
        return list(await cur.fetchall())


def _derive_content(row: aiosqlite.Row) -> str:
    """Pick the best text to send to the ingest LLM.

    For URL captures we stored the scraped body in payload.scrape; for voice
    we stored the transcript in payload.transcript; for images, vision output.
    Fall back to raw.
    """
    payload_raw = row["payload"]
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    scrape = payload.get("scrape") or {}
    if isinstance(scrape, dict):
        text = scrape.get("text")
        title = scrape.get("title")
        if text:
            return (f"{title}\n\n{text}" if title else text)
    vision = payload.get("vision") or {}
    if isinstance(vision, dict):
        parts = [vision.get("ocr") or "", vision.get("description") or ""]
        combined = "\n\n".join(p for p in parts if p and p.strip())
        if combined:
            return combined
    transcript = payload.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        return transcript
    return row["raw"] or ""


async def process_pending(
    *, conn: aiosqlite.Connection, settings: Settings, providers: Providers,
) -> int:
    """Re-run LLM ingest for pending captures. Returns count processed."""
    rows = await _select_pending(conn)
    count = 0
    for row in rows:
        content = _derive_content(row)
        if not content.strip():
            continue
        try:
            processed = await process.process_capture(
                content=content, settings=settings,
                providers=providers, conn=conn,
            )
            await process.mark_processed(conn, capture_id=row["id"], processed=processed)
            count += 1
        except Exception as e:
            log.exception("process_pending failed for capture %s", row["id"])
            await process.mark_failed(conn, capture_id=row["id"], error=str(e))
    return count


async def nightly_sync(
    *, conn: aiosqlite.Connection, settings: Settings,
) -> int:
    """Push every capture still missing a github_sha. Returns count pushed."""
    if not github_sync.is_configured(settings):
        return 0
    ids = await github_sync.unsynced_capture_ids(conn)
    pushed = 0
    for cap_id in ids:
        try:
            ok = await github_sync.push_capture(cap_id, settings=settings, conn=conn)
            if ok:
                pushed += 1
        except Exception:
            log.exception("nightly_sync push failed for capture %s", cap_id)
    return pushed


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _is_past_daily_time_today(settings: Settings) -> bool:
    """True when the current wall-clock time in the user's timezone is at or
    past today's scheduled daily-prompt time. Used by `drain_on_boot` to
    distinguish 'catching up after a crash' (fire now) from 'started early'
    (let the scheduler handle it at the normal time).
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(settings.TIMEZONE)
    now_local = datetime.now(tz)
    try:
        h, m = _parse_hhmm(settings.DAILY_PROMPT_LOCAL_TIME)
    except ValueError:
        return False
    scheduled = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    return now_local >= scheduled


def _format_captures_for_daily(rows: list[aiosqlite.Row]) -> str:
    """Render today's captures as a flat bundle for the daily-prompt LLM."""
    lines = ["Today's fragments:"]
    for i, r in enumerate(rows, 1):
        kind = r["kind"]
        title = None
        try:
            p = json.loads(r["processed"]) if r["processed"] else None
            if isinstance(p, dict):
                title = p.get("title")
        except (TypeError, json.JSONDecodeError):
            pass
        body = r["raw"] or ""
        if title:
            lines.append(f"[{i}] ({kind}) {title}: {body[:400]}")
        else:
            lines.append(f"[{i}] ({kind}) {body[:400]}")
    return "\n".join(lines)


async def weekly_reminder_job(
    *,
    conn: aiosqlite.Connection,
    settings: Settings,
    bot,
) -> bool:
    """When WEEKLY_DIGEST_ENABLED=false, ping the owner at the configured
    weekly time with a nudge to run the digest locally. Zero LLM cost, no
    GitHub push — just a DM so the ritual still happens.

    Skipped silently if today has no captures (nothing to reflect on) or
    if the bot has no way to DM (no owner).
    """
    if settings.TELEGRAM_OWNER_ID == 0:
        return False

    today_local = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    fz_week = fz_week_idx(today_local, parse_dob(settings.DOB))
    iso_week = iso_week_key(today_local)

    async with conn.execute(
        "SELECT COUNT(*) FROM captures WHERE fz_week_idx = ? AND kind != 'why'",
        (fz_week,),
    ) as cur:
        row = await cur.fetchone()
    count = int(row[0]) if row else 0
    if count == 0:
        log.info("weekly_reminder: zero captures in %s, skipping", iso_week)
        return False

    text = (
        f"🕯  digest time — {iso_week} has {count} captures. "
        f"pull the repo and run the digest prompt locally when you're ready."
    )
    try:
        await bot.send_message(chat_id=settings.TELEGRAM_OWNER_ID, text=text)
    except Exception:
        log.exception("weekly_reminder: send_message failed")
        return False
    return True


async def daily_prompt_job(
    *,
    conn: aiosqlite.Connection,
    settings: Settings,
    providers: Providers,
    bot,
    force: bool = False,
) -> bool:
    """Generate the evening prompt and DM it to the owner.

    Idempotent per day via `daily.prompted_at` — repeated calls on the same
    day no-op. Pass `force=True` to bypass the idempotent check (used by
    `/reflect`, which is an explicit user request to re-fire).
    Returns True if a prompt was sent.
    """
    today_local = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    today_str = today_local.isoformat()

    # Idempotent check (skipped when force=True)
    if not force:
        async with conn.execute(
            "SELECT prompted_at FROM daily WHERE local_date = ?", (today_str,)
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None and existing["prompted_at"]:
            log.info("daily_prompt: already prompted on %s, skipping", today_str)
            return False

    # Collect today's captures (skip whys — they attach to their parent)
    async with conn.execute(
        """
        SELECT id, kind, raw, payload, processed
        FROM captures
        WHERE local_date = ? AND kind != 'why'
        ORDER BY id
        """,
        (today_str,),
    ) as cur:
        todays = list(await cur.fetchall())
    if not todays:
        log.info("daily_prompt: zero captures on %s, skipping", today_str)
        return False

    user_content = _format_captures_for_daily(todays)
    try:
        response = await call_llm(
            purpose="daily",
            system_blocks=[VOICE_ORCHURATOR, SYSTEM_DAILY],
            messages=[Message(role="user", content=user_content)],
            max_tokens=120,
            settings=settings, providers=providers, conn=conn,
        )
        question = (response.text or "").strip() or "what stopped you today?"
    except Exception:
        log.exception("daily_prompt: LLM call failed; using fallback question")
        question = "what stopped you today?"

    # Send to the owner FIRST. If send fails (Telegram outage, bad token),
    # don't persist the daily row or pending_reflection — otherwise the user
    # never saw the question but their next message would silently become a
    # mystery "reflection" attached to a prompt they never received.
    try:
        await bot.send_message(chat_id=settings.TELEGRAM_OWNER_ID, text=question)
    except Exception:
        log.exception("daily_prompt: failed to send question; state not persisted")
        return False

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await conn.execute(
        """
        INSERT INTO daily (local_date, prompt, prompted_at)
        VALUES (?, ?, ?)
        ON CONFLICT(local_date) DO UPDATE
          SET prompt = excluded.prompt, prompted_at = excluded.prompted_at
        """,
        (today_str, question, now_iso),
    )
    await conn.commit()

    # Mark next text/voice as the reflection reply
    await reflection.set_pending(conn, local_date=today_str, tz_name=settings.TIMEZONE)
    return True


def build_scheduler(
    *, conn: aiosqlite.Connection, settings: Settings, providers: Providers,
    bot=None,
) -> AsyncIOScheduler:
    """Construct and configure (but don't start) the scheduler."""
    scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)

    scheduler.add_job(
        process_pending,
        kwargs={"conn": conn, "settings": settings, "providers": providers},
        trigger=IntervalTrigger(seconds=60),
        id="process_pending",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    hour, minute = 3, 0  # 03:00 local for nightly_sync
    scheduler.add_job(
        nightly_sync,
        kwargs={"conn": conn, "settings": settings},
        trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.TIMEZONE),
        id="nightly_sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # Daily prompt requires a bot to DM the owner; skip registration if we
    # don't have one (e.g. tests that only want the interval/cron jobs).
    if bot is not None:
        dh, dm = _parse_hhmm(settings.DAILY_PROMPT_LOCAL_TIME)
        scheduler.add_job(
            daily_prompt_job,
            kwargs={
                "conn": conn, "settings": settings,
                "providers": providers, "bot": bot,
            },
            trigger=CronTrigger(hour=dh, minute=dm, timezone=settings.TIMEZONE),
            id="daily_prompt",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        # Weekly cron is opt-in for the digest itself. Either way, the bot
        # fires something at the configured weekend time:
        # - WEEKLY_DIGEST_ENABLED=true  → generate the essay server-side
        # - WEEKLY_DIGEST_ENABLED=false → ping the owner to run it locally
        wh, wm = _parse_hhmm(settings.WEEKLY_DIGEST_LOCAL_TIME)
        weekly_trigger = CronTrigger(
            day_of_week=settings.WEEKLY_DIGEST_DOW,
            hour=wh, minute=wm, timezone=settings.TIMEZONE,
        )
        if settings.WEEKLY_DIGEST_ENABLED:
            scheduler.add_job(
                digest_weekly.weekly_digest_job,
                kwargs={
                    "conn": conn, "settings": settings,
                    "providers": providers, "bot": bot,
                },
                trigger=weekly_trigger,
                id="weekly_digest",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        else:
            scheduler.add_job(
                weekly_reminder_job,
                kwargs={"conn": conn, "settings": settings, "bot": bot},
                trigger=weekly_trigger,
                id="weekly_reminder",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )

    return scheduler


async def drain_on_boot(
    *, conn: aiosqlite.Connection, settings: Settings, providers: Providers,
    bot=None,
) -> None:
    """Run the catch-up jobs once at startup to recover from prior
    crash/shutdown. daily_prompt is idempotent per day, so calling it on
    every boot is safe."""
    try:
        processed = await process_pending(conn=conn, settings=settings, providers=providers)
        if processed:
            log.info("boot drain: reprocessed %s pending captures", processed)
    except Exception:
        log.exception("boot drain: process_pending failed")
    try:
        pushed = await nightly_sync(conn=conn, settings=settings)
        if pushed:
            log.info("boot drain: pushed %s unsynced captures", pushed)
    except Exception:
        log.exception("boot drain: nightly_sync failed")
    # Only fire the daily prompt during boot drain if we're past today's
    # scheduled time — otherwise a daytime restart would send the prompt
    # early and the evening scheduler would no-op on idempotency.
    if bot is not None and _is_past_daily_time_today(settings):
        try:
            await daily_prompt_job(
                conn=conn, settings=settings, providers=providers, bot=bot,
            )
        except Exception:
            log.exception("boot drain: daily_prompt_job failed")
