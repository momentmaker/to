"""Telegram handlers: owner gate, /start, /help, /status, text + URL capture."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from bot import db, forget, github_sync, oracle, process, reflection, scheduler as sched_mod, tweet as tweet_mod, why
from bot.config import Settings
from bot.digest import fz_state as fz_state_mod
from bot.digest import validate as digest_validate
from bot.digest import weekly as digest_weekly
from bot.ingest import pdf as pdf_mod
from bot.ingest import vision as vision_mod
from bot.ingest import voice as voice_mod
from bot.ingest.router import classify_text, scrape_url
from bot.llm import budget as llm_budget
from bot.llm.router import Providers
from bot.persona import ACK_TEXT, GREETING, HELP_TEXT
from bot.week import fz_week_idx, iso_week_key, local_date_for

log = logging.getLogger(__name__)


def is_owner(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user is None or settings.TELEGRAM_OWNER_ID == 0:
        return False
    return user.id == settings.TELEGRAM_OWNER_ID


async def _ensure_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.bot_data["settings"]
    if not is_owner(update, settings):
        log.info("rejecting non-owner update from user_id=%s", getattr(update.effective_user, "id", None))
        return False
    return True


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    await update.message.reply_text(GREETING)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    await update.message.reply_text(HELP_TEXT)


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    dob = db.settings_dob(settings.DOB)

    total = await db.count_captures(conn)
    this_week = await db.count_captures_this_week(conn, dob=dob, tz_name=settings.TIMEZONE)
    today_local = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    w_idx = fz_week_idx(today_local, dob)
    w_key = iso_week_key(today_local)

    mtd_total = await llm_budget.month_to_date_usd(conn)
    mtd_by = await llm_budget.month_to_date_by_provider(conn)
    cap = float(settings.LLM_MONTHLY_USD_CAP)
    cache_ratio = await llm_budget.cache_hit_ratio(conn)

    degrade_note = ""
    if cap > 0 and mtd_total >= cap:
        degrade_note = " (above cap; non-digest degraded to *_CHEAP)"

    cost_lines = [f"llm month-to-date: ${mtd_total:.2f} / ${cap:.2f}{degrade_note}"]
    for name in ("anthropic", "openai"):
        val = mtd_by.get(name, 0.0)
        if val > 0:
            cost_lines.append(f"  {name}: ${val:.2f}")
    cache_line = f"cache hit: {cache_ratio * 100:.0f}%" if cache_ratio else "cache hit: 0%"

    tweet_line = (
        f"tweets: daily={'on' if settings.X_DAILY_ENABLED else 'off'} "
        f"weekly={'on' if settings.X_WEEKLY_ENABLED else 'off'}"
    )
    digest_line = (
        f"digest cron: {'on' if settings.WEEKLY_DIGEST_ENABLED else 'off'} "
        f"({settings.WEEKLY_DIGEST_DOW} {settings.WEEKLY_DIGEST_LOCAL_TIME})"
    )
    config_line = f"dob: {settings.DOB}  |  tz: {settings.TIMEZONE}"

    lines = [
        f"corpus: {total}",
        f"this week ({w_key}, fz-week {w_idx}): {this_week}",
        "",
        *cost_lines,
        cache_line,
        "",
        tweet_line,
        digest_line,
        config_line,
    ]
    await update.message.reply_text("\n".join(lines))


def _forward_origin_payload(message) -> dict | None:
    """Extract forwarded-message provenance, if any, for the capture's payload."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    data: dict[str, Any] = {"type": origin.__class__.__name__}
    for attr in ("sender_user", "sender_chat", "chat"):
        obj = getattr(origin, attr, None)
        if obj is not None:
            data[attr] = {
                "id": getattr(obj, "id", None),
                "name": getattr(obj, "full_name", None) or getattr(obj, "title", None),
            }
    sender_name = getattr(origin, "sender_user_name", None)
    if sender_name:
        data["sender_user_name"] = sender_name
    date_val = getattr(origin, "date", None)
    if date_val is not None:
        data["date"] = date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val)
    return data


async def _process_in_background(
    *, capture_id: int, content: str, settings: Settings,
    providers: Providers, db_conn,
) -> None:
    """Fire-and-forget LLM processing + GitHub push. Updates the capture row
    on completion. GitHub failure is logged but doesn't block — nightly_sync
    will catch up.
    """
    try:
        processed = await process.process_capture(
            content=content,
            settings=settings,
            providers=providers,
            conn=db_conn,
        )
        await process.mark_processed(db_conn, capture_id=capture_id, processed=processed)
    except Exception as e:
        log.exception("processing failed for capture_id=%s", capture_id)
        try:
            await process.mark_failed(db_conn, capture_id=capture_id, error=str(e))
        except Exception:
            log.exception("mark_failed also failed for capture_id=%s", capture_id)
    # Regardless of LLM outcome, try to push the raw capture. A failed ingest
    # still deserves to be in the repo.
    try:
        await github_sync.push_capture(capture_id, settings=settings, conn=db_conn)
    except Exception:
        log.exception(
            "github push failed for capture_id=%s; nightly_sync will retry",
            capture_id,
        )


async def _push_in_background(
    *, capture_id: int, settings: Settings, db_conn,
) -> None:
    try:
        await github_sync.push_capture(capture_id, settings=settings, conn=db_conn)
    except Exception:
        log.exception(
            "background github push failed for capture_id=%s; nightly_sync will retry",
            capture_id,
        )


async def _consume_pending_if_any(
    message, settings: Settings, conn, dob, text: str,
    providers: Providers | None,
) -> bool:
    """If there's a live pending-why OR pending-reflection, store `text` as
    the appropriate kind, link it, ack, and kick off follow-up processing.
    Returns True when a pending state was consumed (caller should early-exit).
    """
    forward = _forward_origin_payload(message)
    base_payload: dict[str, Any] = {"forward_origin": forward} if forward else {}

    # Why has priority: it's specific to a URL the user just saved.
    pending_parent = await why.consume_if_live(conn)
    if pending_parent is not None:
        capture_id = await db.insert_capture(
            conn,
            kind="why",
            source="telegram",
            raw=text,
            payload=base_payload or None,
            parent_id=pending_parent,
            telegram_msg_id=message.message_id,
            dob=dob,
            tz_name=settings.TIMEZONE,
            # Whys render inline into their parent's file — no separate LLM
            # ingest needed, so mark processed at insert to keep the
            # process_pending sweeper from repeatedly re-running the LLM on them.
            status="processed",
        )
        if capture_id is None:
            return True
        await message.reply_text(ACK_TEXT)
        asyncio.create_task(
            _push_in_background(capture_id=capture_id, settings=settings, db_conn=conn)
        )
        return True

    # Daily reflection: attaches to today's `daily` row.
    pending_local_date = await reflection.consume_if_live(conn)
    if pending_local_date is not None:
        capture_id = await db.insert_capture(
            conn,
            kind="reflection",
            source="telegram",
            raw=text,
            payload=base_payload or None,
            telegram_msg_id=message.message_id,
            dob=dob,
            tz_name=settings.TIMEZONE,
        )
        if capture_id is None:
            return True
        await conn.execute(
            "UPDATE daily SET reflection_capture_id = ? WHERE local_date = ?",
            (capture_id, pending_local_date),
        )
        await conn.commit()
        await message.reply_text(ACK_TEXT)
        if providers is not None and text.strip():
            asyncio.create_task(
                _process_in_background(
                    capture_id=capture_id, content=text,
                    settings=settings, providers=providers, db_conn=conn,
                )
            )
        # Optional daily tweet — only if user opted in + OAuth configured.
        if providers is not None and tweet_mod.is_configured_for_daily(settings):
            asyncio.create_task(
                _post_daily_tweet(
                    local_date=pending_local_date, reflection_text=text,
                    settings=settings, providers=providers, db_conn=conn,
                )
            )
        return True

    return False


async def _post_daily_tweet(
    *, local_date: str, reflection_text: str,
    settings: Settings, providers: Providers, db_conn,
) -> None:
    """Generate + post the day's tweet, then persist text + sent time on `daily`.
    All errors are swallowed (logged only) — tweets are a bonus, not critical.
    """
    try:
        # Pull today's fragments for context
        async with db_conn.execute(
            "SELECT raw, processed FROM captures "
            "WHERE local_date = ? AND kind IN ('text', 'url', 'image', 'voice') "
            "ORDER BY id",
            (local_date,),
        ) as cur:
            rows = list(await cur.fetchall())
        fragments_text = "\n".join(
            f"- {(r['raw'] or '')[:300]}" for r in rows if r['raw']
        )
        tweet_text = await tweet_mod.generate_daily_tweet(
            fragments_text=fragments_text or "(no fragments)",
            reflection=reflection_text,
            settings=settings, providers=providers, conn=db_conn,
        )
        if not tweet_text:
            return
        result = await tweet_mod.post_tweet(tweet_text, settings=settings)
        if result is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        await db_conn.execute(
            "UPDATE daily SET tweet_text = ?, tweet_posted_at = ? WHERE local_date = ?",
            (tweet_text, now_iso, local_date),
        )
        await db_conn.commit()
    except Exception:
        log.exception("daily tweet post failed")


async def _ask_and_set_pending_why(
    *, parent_id: int, url: str, title: str | None,
    settings: Settings, providers: Providers, conn, bot, chat_id: int,
) -> None:
    question = await why.ask_why_question(
        url=url, title=title,
        settings=settings, providers=providers, conn=conn,
    )
    try:
        await bot.send_message(chat_id=chat_id, text=question)
    except Exception:
        log.exception("failed to send why question")
        return
    await why.set_pending(
        conn, parent_id=parent_id, window_minutes=settings.WHY_WINDOW_MINUTES
    )


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    if update.message is None or update.message.text is None:
        return
    if update.message.chat.type != "private":
        return

    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    dob = db.settings_dob(settings.DOB)

    text = update.message.text
    kind, url = classify_text(text)

    # Plain-text messages may be an in-flight "why" or daily-reflection reply.
    # URL messages are never treated as whys — the user has moved on.
    if kind == "text":
        consumed = await _consume_pending_if_any(update.message, settings, conn, dob, text, providers)
        if consumed:
            return

    forward = _forward_origin_payload(update.message)
    payload: dict[str, Any] = {}
    if forward:
        payload["forward_origin"] = forward

    processing_content: str = text
    source = "telegram"
    scrape_title: str | None = None

    if kind == "url" and url is not None:
        scrape = await scrape_url(url, settings=settings)
        payload["scrape"] = {"source": scrape.source, **scrape.payload}
        if scrape.error:
            payload["scrape_error"] = scrape.error
        processing_content = scrape.content or text
        source = scrape.source
        # HN payloads nest title under "story"; article/reddit/x have it at top level.
        if isinstance(scrape.payload, dict):
            scrape_title = (
                scrape.payload.get("title")
                or (scrape.payload.get("story") or {}).get("title")
            )

    capture_id = await db.insert_capture(
        conn,
        kind=kind,
        source=source,
        url=url,
        raw=text,
        payload=payload or None,
        telegram_msg_id=update.message.message_id,
        dob=dob,
        tz_name=settings.TIMEZONE,
    )
    if capture_id is None:
        log.info("ignoring duplicate telegram_msg_id=%s", update.message.message_id)
        return

    await update.message.reply_text(ACK_TEXT)

    # Don't burn tokens processing a bare URL. A failed scrape leaves
    # processing_content equal to the URL (or the raw message text which IS
    # just the URL in the pure-link case) — nothing useful for the LLM.
    content_stripped = processing_content.strip() if processing_content else ""
    should_process = (
        providers is not None
        and bool(content_stripped)
        and content_stripped != (url or "").strip()
    )
    if should_process:
        asyncio.create_task(
            _process_in_background(
                capture_id=capture_id,
                content=processing_content,
                settings=settings,
                providers=providers,
                db_conn=conn,
            )
        )
    elif github_sync.is_configured(settings):
        # Scrape failed or content is unprocessable (e.g. bare URL). We still
        # want the capture to land in the repo — don't wait for nightly_sync.
        asyncio.create_task(
            _push_to_github(capture_id=capture_id, settings=settings, conn=conn)
        )

    # Kick off the capture-time "why?" for URLs only.
    if kind == "url" and url is not None and providers is not None:
        asyncio.create_task(
            _ask_and_set_pending_why(
                parent_id=capture_id, url=url, title=scrape_title,
                settings=settings, providers=providers, conn=conn,
                bot=context.bot, chat_id=update.message.chat.id,
            )
        )


async def skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any pending 'why?' or daily reflection without storing."""
    if not await _ensure_owner(update, context):
        return
    conn = context.bot_data["db"]
    await why.clear_pending(conn)
    await reflection.clear_pending(conn)
    await update.message.reply_text("skipped.")


async def setvow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setvow <text> — store the user's vow for fz.ax's dashboard."""
    if not await _ensure_owner(update, context):
        return
    conn = context.bot_data["db"]
    text = " ".join(context.args) if context.args else ""
    if not text.strip():
        await update.message.reply_text(
            "usage: /setvow <the line you want pinned above the year>",
        )
        return
    await fz_state_mod.set_vow(conn, text)
    await update.message.reply_text("vow set.")


async def setmark_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmark <single-grapheme> — override the current week's mark.

    The mark is stored now but essay/whisper generation still happens at the
    scheduled weekly digest time. Status is left 'pending' on new rows so the
    scheduler doesn't skip the week on idempotency.
    """
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    raw = " ".join(context.args) if context.args else ""
    mark = raw.strip()
    if not digest_validate.is_single_grapheme(mark):
        await update.message.reply_text(
            "mark must be exactly one character or emoji.",
        )
        return

    dob = db.settings_dob(settings.DOB)
    today_local = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    w_idx = fz_week_idx(today_local, dob)
    w_key = iso_week_key(today_local)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    await conn.execute(
        """
        INSERT INTO weekly (fz_week_idx, iso_week_key, mark, marked_at, status)
        VALUES (?, ?, ?, ?, 'pending')
        ON CONFLICT(fz_week_idx) DO UPDATE SET
          mark = excluded.mark, marked_at = excluded.marked_at
        """,
        (w_idx, w_key, mark, now_iso),
    )
    await conn.commit()
    await update.message.reply_text(f"{mark}  set for {w_key}.")


async def _run_export_in_background(
    *, conn, settings: Settings, providers: Providers, bot, chat_id: int,
) -> None:
    try:
        ok = await digest_weekly.weekly_digest_job(
            conn=conn, settings=settings, providers=providers, bot=bot,
            force=True,
        )
    except Exception:
        log.exception("export background task crashed")
        ok = False
    if not ok:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="digest could not be generated (no captures, or validation failed).",
            )
        except Exception:
            log.exception("failed to notify owner of export failure")


async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/export — force the weekly digest to run now (doesn't wait for Saturday).

    The digest is a ~30s LLM call plus GitHub I/O. Running inline would block
    the webhook for the whole duration, risking Telegram's ~75s timeout and
    a retried /export that fires a second digest. We ack fast and run in a
    background task; results (or failure) are DM'd separately.
    """
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    if providers is None:
        await update.message.reply_text("no LLM configured; cannot export.")
        return

    await update.message.reply_text("running weekly digest...")
    asyncio.create_task(
        _run_export_in_background(
            conn=conn, settings=settings, providers=providers,
            bot=context.bot, chat_id=update.message.chat.id,
        )
    )


async def forget_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/forget <id>  or  /forget last  — irrevocably remove a capture.

    Cascades: deleting a primary capture also drops its inline children
    (whys + highlights) and clears any daily.reflection_capture_id
    pointer. Deleting a why or highlight re-renders the parent's GitHub
    file without that child.
    """
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "usage: /forget <capture_id>   or   /forget last",
        )
        return

    arg = args[0].strip().lower()
    if arg == "last":
        capture_id = await forget.find_most_recent_id(conn)
        if capture_id is None:
            await update.message.reply_text("nothing to forget.")
            return
    else:
        try:
            capture_id = int(arg)
        except ValueError:
            await update.message.reply_text(
                "usage: /forget <capture_id>   or   /forget last",
            )
            return

    result = await forget.forget_capture(conn, capture_id, settings=settings)
    if result is None:
        await update.message.reply_text(f"capture {capture_id} not found.")
        return

    bits = [f"forgotten: capture {result['id']} ({result['kind']})"]
    if result["cascaded_children"]:
        n = len(result["cascaded_children"])
        bits.append(
            f"  also dropped {n} inline child{'' if n == 1 else 'ren'} "
            f"(why/highlight)"
        )
    if not result["github_deleted"] and github_sync.is_configured(settings):
        bits.append("  (github side not updated — check logs)")
    await update.message.reply_text("\n".join(bits))


async def highlight_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/highlight <text> — attach a verbatim highlight to a previous capture.

    Must be a reply to the message that created the parent capture. The
    highlight renders inline inside the parent's markdown (same pattern as
    /why) and pushes to GitHub via the parent's file.
    """
    if not await _ensure_owner(update, context):
        return
    if update.message is None:
        return
    if update.message.chat.type != "private":
        return

    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    dob = db.settings_dob(settings.DOB)

    parent_msg = update.message.reply_to_message
    if parent_msg is None:
        await update.message.reply_text(
            "/highlight must be a reply — reply to the capture's original "
            "message with /highlight <text>."
        )
        return

    text = " ".join(context.args) if context.args else ""
    text = text.strip()
    if not text:
        await update.message.reply_text(
            "usage: /highlight <text>   (as a reply to a previous capture)"
        )
        return

    async with conn.execute(
        "SELECT id, kind, parent_id FROM captures "
        "WHERE source = 'telegram' AND telegram_msg_id = ?",
        (parent_msg.message_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await update.message.reply_text(
            "couldn't find a capture for that message. reply to the message "
            "you originally sent (not the bot's ack)."
        )
        return
    # If the user replied to a why or highlight, attach the new highlight to
    # the ROOT primary instead — chained children would render incoherently
    # because whys/highlights don't have their own files.
    if row["kind"] in ("why", "highlight") and row["parent_id"] is not None:
        parent_id = int(row["parent_id"])
    else:
        parent_id = int(row["id"])

    capture_id = await db.insert_capture(
        conn,
        kind="highlight",
        source="telegram",
        raw=text,
        parent_id=parent_id,
        telegram_msg_id=update.message.message_id,
        dob=dob,
        tz_name=settings.TIMEZONE,
    )
    if capture_id is None:
        return

    await update.message.reply_text(f"{ACK_TEXT} (highlight → {parent_id})")

    # Push through the parent so the updated markdown lands in GitHub.
    if github_sync.is_configured(settings):
        asyncio.create_task(
            _push_to_github(capture_id=capture_id, settings=settings, conn=conn)
        )


async def _push_to_github(
    *, capture_id: int, settings: Settings, conn,
) -> None:
    """Push-only task. For captures that either skip LLM processing (bare
    URLs, empty content) or are children of a parent (highlights route
    through their parent's markdown via push_capture)."""
    try:
        await github_sync.push_capture(capture_id, settings=settings, conn=conn)
    except Exception:
        log.exception("push_to_github failed for capture %s", capture_id)


_WEEK_ARG_RE = re.compile(r"^\d{4}-w\d{2}$")


async def tweetweekly_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tweetweekly [YYYY-wNN] — fetch a digest.md from the captures repo and
    post a tweet drawn from it. For users on the local-digest workflow who
    also want the weekly tweet their server-side counterparts get for free.

    Defaults to today's week. Requires X_WEEKLY_ENABLED + OAuth + GitHub sync.
    """
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")

    if not tweet_mod.is_configured_for_weekly(settings):
        await update.message.reply_text(
            "weekly tweet is not configured. "
            "set X_WEEKLY_ENABLED=true and the four X_* OAuth creds."
        )
        return
    if providers is None:
        await update.message.reply_text("no LLM configured; cannot draft tweet.")
        return
    if not github_sync.is_configured(settings):
        await update.message.reply_text(
            "github is not configured; cannot fetch the digest."
        )
        return

    today = local_date_for(datetime.now(timezone.utc), settings.TIMEZONE)
    current_iso_week = iso_week_key(today)

    args = context.args or []
    if args:
        week_arg = args[0].strip().lower()
        if not _WEEK_ARG_RE.match(week_arg):
            await update.message.reply_text(
                "usage: /tweetweekly [YYYY-wNN]   (e.g. 2026-w17)"
            )
            return
        week_dir = week_arg
        iso_week = week_arg.upper()
    else:
        iso_week = current_iso_week
        week_dir = iso_week.replace("W", "w")

    path = f"{week_dir}/digest.md"

    try:
        fetched = await github_sync.fetch_file(settings=settings, path=path)
    except Exception:
        log.exception("tweetweekly: fetch_file failed")
        await update.message.reply_text(
            "couldn't reach github to read the digest. check logs."
        )
        return
    if fetched is None:
        await update.message.reply_text(
            f"no digest found at {path} — run `weekly_digest` locally first, "
            f"then push."
        )
        return
    content, _sha = fetched

    parsed = tweet_mod.parse_digest_md(content)
    if parsed is None:
        await update.message.reply_text(
            f"{path} exists but didn't parse as a digest. expected format:\n"
            f"  # YYYY-WNN\n"
            f"  \n"
            f"  **<mark>**  _<whisper>_\n"
            f"  \n"
            f"  <essay>"
        )
        return

    await update.message.reply_text("drafting tweet…")

    tweet_text = await tweet_mod.generate_weekly_tweet(
        mark=parsed["mark"],
        whisper=parsed["whisper"],
        essay=parsed["essay"],
        settings=settings, providers=providers, conn=conn,
    )
    if not tweet_text:
        await update.message.reply_text(
            "tweet generation failed. try again or check logs."
        )
        return

    result = await tweet_mod.post_tweet(tweet_text, settings=settings)
    if result is None:
        await update.message.reply_text("tweet post failed — check logs.")
        return

    # Backfill the weekly row for the current week only — past weeks need the
    # right fz_week_idx, which we can't safely derive from iso_week alone.
    if iso_week == current_iso_week:
        try:
            dob = db.settings_dob(settings.DOB)
            fz_week = fz_week_idx(today, dob)
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            await conn.execute(
                """
                INSERT INTO weekly (
                    fz_week_idx, iso_week_key, essay, whisper, mark, marked_at,
                    tweet_text, tweet_posted_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processed')
                ON CONFLICT(fz_week_idx) DO UPDATE SET
                  essay = excluded.essay,
                  whisper = excluded.whisper,
                  mark = excluded.mark,
                  tweet_text = excluded.tweet_text,
                  tweet_posted_at = excluded.tweet_posted_at
                """,
                (fz_week, iso_week, parsed["essay"], parsed["whisper"], parsed["mark"],
                 now_iso, tweet_text, now_iso),
            )
            await conn.commit()
        except Exception:
            log.exception("tweetweekly: weekly row backfill failed")

    await update.message.reply_text(f"tweeted:\n{result.url}")


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ask <question> — consult the commonplace. Supports `since:YYYY-MM-DD`
    and `limit:N` modifiers anywhere in the question.
    """
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    if providers is None:
        await update.message.reply_text("no LLM configured; cannot ask.")
        return

    raw = " ".join(context.args) if context.args else ""
    if not raw.strip():
        await update.message.reply_text(
            "usage: /ask <question>   modifiers: since:YYYY-MM-DD  limit:N",
        )
        return

    answer, _fragments = await oracle.ask(
        question_raw=raw,
        settings=settings, providers=providers, conn=conn,
    )
    await update.message.reply_text(answer)


async def reflect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force today's daily prompt to fire now. Useful when the owner wants to
    reflect outside the scheduled window."""
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    if providers is None:
        await update.message.reply_text("no LLM configured; cannot generate prompt.")
        return
    sent = await sched_mod.daily_prompt_job(
        conn=conn, settings=settings, providers=providers, bot=context.bot,
        force=True,
    )
    if not sent:
        await update.message.reply_text("nothing to reflect on yet today.")


async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    if update.message is None:
        return
    if update.message.chat.type != "private":
        return

    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    dob = db.settings_dob(settings.DOB)

    audio = update.message.voice or update.message.audio
    if audio is None:
        return

    # Transcribe first so we can route the text through the same
    # pending-why / pending-reflection logic as text messages.
    try:
        file = await audio.get_file()
        audio_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        log.exception("voice download failed")
        await update.message.reply_text("could not fetch the voice note. try again.")
        return
    transcript = ""
    transcription_error: str | None = None
    try:
        transcript = await voice_mod.transcribe_voice_bytes(
            audio_bytes, filename=getattr(audio, "file_name", None) or "voice.ogg",
            settings=settings,
        )
    except Exception as e:
        log.exception("whisper transcription failed")
        transcription_error = str(e)[:200]

    if transcript.strip():
        consumed = await _consume_pending_if_any(
            update.message, settings, conn, dob, transcript, providers,
        )
        if consumed:
            return

    payload: dict[str, Any] = {}
    forward = _forward_origin_payload(update.message)
    if forward:
        payload["forward_origin"] = forward
    if transcript:
        payload["transcript"] = transcript
    if transcription_error:
        payload["transcript_error"] = transcription_error

    capture_id = await db.insert_capture(
        conn,
        kind="voice",
        source="telegram",
        raw=transcript or None,
        payload=payload or None,
        telegram_msg_id=update.message.message_id,
        dob=dob,
        tz_name=settings.TIMEZONE,
    )
    if capture_id is None:
        return

    await update.message.reply_text(ACK_TEXT)

    if providers is not None and transcript:
        asyncio.create_task(
            _process_in_background(
                capture_id=capture_id, content=transcript,
                settings=settings, providers=providers, db_conn=conn,
            )
        )


async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    if update.message is None:
        return
    if update.message.chat.type != "private":
        return

    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    dob = db.settings_dob(settings.DOB)

    # Pick the largest available photo size
    photo_sizes = update.message.photo or []
    if not photo_sizes:
        return
    photo = photo_sizes[-1]

    try:
        file = await photo.get_file()
        image_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        log.exception("photo download failed")
        await update.message.reply_text("could not fetch the photo. try again.")
        return

    caption = update.message.caption or ""
    payload: dict[str, Any] = {"caption": caption} if caption else {}
    forward = _forward_origin_payload(update.message)
    if forward:
        payload["forward_origin"] = forward

    vision_text: str = caption  # fallback content if vision fails
    if providers is not None:
        try:
            vision_result = await vision_mod.ocr_and_describe(
                image_bytes,
                mime_type="image/jpeg",
                settings=settings, providers=providers, conn=conn,
            )
            payload["vision"] = vision_result
            # combine OCR + description + caption for downstream processing
            parts = [vision_result.get("ocr") or "", vision_result.get("description") or ""]
            if caption:
                parts.insert(0, caption)
            vision_text = "\n\n".join(p for p in parts if p.strip())
        except Exception as e:
            log.exception("vision failed")
            payload["vision_error"] = str(e)[:200]

    capture_id = await db.insert_capture(
        conn,
        kind="image",
        source="telegram",
        raw=caption or None,
        payload=payload or None,
        telegram_msg_id=update.message.message_id,
        dob=dob,
        tz_name=settings.TIMEZONE,
    )
    if capture_id is None:
        return

    await update.message.reply_text(ACK_TEXT)

    if providers is not None and vision_text.strip():
        asyncio.create_task(
            _process_in_background(
                capture_id=capture_id, content=vision_text,
                settings=settings, providers=providers, db_conn=conn,
            )
        )


_PDF_MIME = "application/pdf"


async def document_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram Document messages — currently only PDFs.

    Tier the PDF by token estimate:
      - tiny (≤5k) / medium (5k–20k): extract, insert, process normally
      - large (>20k tokens or >50 pages): reject with a nudge to send highlights
    """
    if not await _ensure_owner(update, context):
        return
    if update.message is None:
        return
    if update.message.chat.type != "private":
        return

    doc = update.message.document
    if doc is None:
        return
    # MIME can carry parameters like "application/pdf; charset=binary" —
    # split on ';' and match the base type exactly so we don't accept
    # spoofed types like "application/pdfhax".
    mime = (doc.mime_type or "").lower().split(";", 1)[0].strip()
    if mime != _PDF_MIME:
        # Other document types aren't supported yet — keep silent so we don't
        # spam the owner when they forward random files.
        return

    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    providers: Providers | None = context.bot_data.get("providers")
    dob = db.settings_dob(settings.DOB)

    try:
        file = await doc.get_file()
        pdf_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        log.exception("pdf download failed")
        await update.message.reply_text("could not fetch the pdf. try again.")
        return

    extract = pdf_mod.extract_pdf_bytes(pdf_bytes)

    if extract.rejected_reason:
        await update.message.reply_text(extract.rejected_reason)
        return

    if not extract.text.strip():
        await update.message.reply_text(
            "the pdf has no selectable text — likely a scan. "
            "send a photo of the passage instead so vision can read it."
        )
        return

    filename = doc.file_name or "document.pdf"
    caption = update.message.caption or ""
    payload: dict[str, Any] = {
        "filename": filename,
        "page_count": extract.page_count,
        "char_count": extract.char_count,
        "token_estimate": extract.token_estimate,
        "tier": extract.tier,
    }
    if caption:
        payload["caption"] = caption
    forward = _forward_origin_payload(update.message)
    if forward:
        payload["forward_origin"] = forward

    capture_id = await db.insert_capture(
        conn,
        kind="pdf",
        source="telegram",
        raw=extract.text,
        payload=payload,
        telegram_msg_id=update.message.message_id,
        dob=dob,
        tz_name=settings.TIMEZONE,
    )
    if capture_id is None:
        return

    await update.message.reply_text(
        f"{ACK_TEXT} ({extract.page_count}p · ~{extract.token_estimate} tokens · {extract.tier})"
    )

    if providers is not None:
        # process.process_capture truncates at 30k chars before the LLM call,
        # so medium PDFs are naturally cost-bounded.
        content_for_ingest = extract.text
        if caption:
            content_for_ingest = f"{caption}\n\n{content_for_ingest}"
        asyncio.create_task(
            _process_in_background(
                capture_id=capture_id, content=content_for_ingest,
                settings=settings, providers=providers, db_conn=conn,
            )
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("unhandled error", exc_info=context.error)
    try:
        from bot.notify import send_alert
        msg = str(context.error)[:200] if context.error else "unknown error"
        await send_alert(f"bot error: <code>{msg}</code>", severity="critical")
    except Exception:
        pass
    if isinstance(update, Update) and update.message is not None:
        try:
            await update.message.reply_text("something slipped. try again.")
        except Exception:
            pass
