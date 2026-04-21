from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import db as db_mod
from bot.config import Settings
from bot.digest import weekly
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42,
        DOB="1990-01-15", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
        GITHUB_TOKEN="", GITHUB_REPO="",  # no-push by default for tests
        WEEK_START="mon",
    )
    base.update(kw)
    return Settings(**base)


class _StubProv:
    """Returns a sequence of canned LLM responses for the digest call(s)."""
    name = "anthropic"
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        text = self._responses.pop(0) if self._responses else ""
        return LlmResponse(
            text=text, model=kwargs["model"], provider=self.name,
            input_tokens=100, output_tokens=200,
        )


def _valid_digest_json(essay: str, whisper: str = "a small week", mark: str = "☲") -> str:
    return json.dumps({"essay": essay, "whisper": whisper, "mark": mark})


async def _insert_text_capture(conn, *, fz_week_idx: int, raw: str,
                                processed: dict | None = None, msg_id: int = 0):
    # Minimal shape for a week-level capture.
    import json as _j
    await conn.execute(
        """
        INSERT INTO captures (
            kind, source, raw, payload, processed, created_at,
            local_date, iso_week_key, fz_week_idx, status, telegram_msg_id
        ) VALUES ('text', 'telegram', ?, NULL, ?, ?, ?, ?, ?, 'processed', ?)
        """,
        (raw, _j.dumps(processed) if processed else None,
         "2026-04-21T12:00:00Z", "2026-04-21", "2026-W16", fz_week_idx, msg_id),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT last_insert_rowid()"
    ) as cur:
        row = await cur.fetchone()
    return int(row[0])


@pytest.mark.asyncio
async def test_digest_system_does_not_include_orchurator_voice(conn):
    """The essay must be the user's voice, not orchurator's — so VOICE_ORCHURATOR
    must NOT appear anywhere in the system blocks sent to the digest model.
    """
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the impediment to action advances action",
    )
    prov = _StubProv([_valid_digest_json(
        "The impediment to action advances action.", whisper="x", mark="☲",
    )])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True
    assert prov.calls, "digest LLM was not called"
    joined = "\n\n".join(prov.calls[0]["system_blocks"])
    assert "orchurator" not in joined.lower()
    # But the quote-only rules SHOULD be present
    assert "quote-only" in joined.lower() or "verbatim" in joined.lower()


@pytest.mark.asyncio
async def test_weekly_digest_stores_row_on_success(conn):
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the impediment to action advances action",
    )
    prov = _StubProv([_valid_digest_json(
        "The impediment to action advances action.", whisper="small", mark="☲",
    )])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    async with conn.execute(
        "SELECT status, mark, whisper, essay FROM weekly WHERE fz_week_idx = ?",
        (1888,),
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "processed"
    assert row["mark"] == "☲"
    assert row["whisper"] == "small"
    assert "impediment" in row["essay"].lower()


@pytest.mark.asyncio
async def test_weekly_digest_is_idempotent_unless_forced(conn):
    await _insert_text_capture(conn, fz_week_idx=1888, raw="x")
    prov = _StubProv([
        _valid_digest_json("x.", mark="a"),
        _valid_digest_json("x.", mark="b"),  # only used if force=True
    ])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    first = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    second = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    forced = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888, force=True,
    )
    assert first is True
    assert second is False
    assert forced is True
    async with conn.execute("SELECT mark FROM weekly WHERE fz_week_idx = ?", (1888,)) as cur:
        row = await cur.fetchone()
    assert row["mark"] == "b"


@pytest.mark.asyncio
async def test_weekly_digest_skips_when_no_captures(conn):
    prov = _StubProv([])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is False
    assert prov.calls == []


@pytest.mark.asyncio
async def test_weekly_mark_is_single_grapheme(conn):
    """If the LLM returns a multi-grapheme mark, our extractor keeps just the
    first grapheme."""
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the line that caught me",
    )
    prov = _StubProv([json.dumps({
        "essay": "The line that caught me.",
        "whisper": "x",
        "mark": "☲🌱",  # two graphemes — should be trimmed
    })])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True
    async with conn.execute("SELECT mark FROM weekly WHERE fz_week_idx = ?", (1888,)) as cur:
        row = await cur.fetchone()
    from bot.digest.validate import is_single_grapheme
    assert is_single_grapheme(row["mark"])


@pytest.mark.asyncio
async def test_weekly_whisper_enforces_240_cap_and_rejects_empty(conn):
    """Whisper that exceeds 240 chars should be rejected by validator and
    trigger the single retry. If the retry also fails, row is marked failed.
    """
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the line that caught me",
    )
    long_whisper = "x" * 500
    prov = _StubProv([
        json.dumps({"essay": "The line that caught me.", "whisper": long_whisper, "mark": "☲"}),
        json.dumps({"essay": "The line that caught me.", "whisper": long_whisper, "mark": "☲"}),
    ])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is False
    async with conn.execute("SELECT status FROM weekly WHERE fz_week_idx = ?", (1888,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_weekly_essay_is_quote_only_retries_once_on_hallucination(conn):
    """If the first essay contains a hallucinated sentence, the validator
    rejects it and a retry is issued. If the retry is clean, we commit.
    """
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the impediment to action advances action",
    )
    hallucinated = json.dumps({
        "essay": "The impediment to action advances action. I also really love pizza.",
        "whisper": "x",
        "mark": "☲",
    })
    clean = json.dumps({
        "essay": "The impediment to action advances action.",
        "whisper": "x",
        "mark": "☲",
    })
    prov = _StubProv([hallucinated, clean])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True
    assert len(prov.calls) == 2  # initial + retry


@pytest.mark.asyncio
async def test_weekly_essay_failed_twice_marks_weekly_failed_and_alerts(conn):
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the only fragment",
    )
    bad = json.dumps({
        "essay": "The only fragment. I also love pizza.",  # hallucinated
        "whisper": "x", "mark": "☲",
    })
    prov = _StubProv([bad, bad])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    with patch("bot.notify.send_alert", AsyncMock()) as alert:
        ok = await weekly.weekly_digest_job(
            conn=conn, settings=_settings(), providers=providers, bot=bot,
            fz_week=1888,
        )
    assert ok is False
    alert.assert_awaited_once()

    async with conn.execute("SELECT status FROM weekly WHERE fz_week_idx = ?", (1888,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_weekly_digest_includes_why_replies_in_corpus(conn):
    """Whys are part of the week's voice and must appear in the corpus for
    the LLM, so the essay can draw from them too."""
    import json as _j
    parent_id = await _insert_text_capture(
        conn, fz_week_idx=1888, raw="https://ex.com",
        msg_id=1,
    )
    await conn.execute(
        """INSERT INTO captures (kind, source, raw, created_at, local_date,
                                 iso_week_key, fz_week_idx, status, parent_id, telegram_msg_id)
           VALUES ('why', 'telegram', 'because the structure caught me',
                   '2026-04-21T12:05:00Z', '2026-04-21', '2026-W16', 1888, 'processed', ?, 2)""",
        (parent_id,),
    )
    await conn.commit()

    prov = _StubProv([json.dumps({
        "essay": "Because the structure caught me.",
        "whisper": "x", "mark": "☲",
    })])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True
    user_msg = prov.calls[0]["messages"][0].content
    assert "structure caught me" in user_msg


@pytest.mark.asyncio
async def test_export_pushes_fz_backup_via_probe_then_push(conn):
    """Regression: fz-ax-backup.json push probes GitHub for the current sha
    right before PUT instead of caching one. This is the file the user may
    also edit (via a local Claude Code digest run), so a cached sha would
    go stale and cause 409 conflicts on the bot's next push.
    """
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the only fragment",
    )
    prov = _StubProv([
        json.dumps({"essay": "The only fragment.", "whisper": "x", "mark": "☲"}),
    ])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()
    settings = _settings(GITHUB_TOKEN="ghp_test", GITHUB_REPO="u/r")

    probe_and_push_calls: list[dict] = []
    async def _fake_auto(**kwargs):
        probe_and_push_calls.append(kwargs)
        return "new-sha"
    with patch("bot.digest.weekly._put_with_auto_sha",
               AsyncMock(side_effect=_fake_auto)):
        ok = await weekly.weekly_digest_job(
            conn=conn, settings=settings, providers=providers, bot=bot,
            fz_week=1888,
        )
    assert ok is True
    # Both digest.md and fz-ax-backup.json go through _put_with_auto_sha.
    paths = {c["path"] for c in probe_and_push_calls}
    assert "fz-ax-backup.json" in paths
    assert any("digest.md" in p for p in paths)


@pytest.mark.asyncio
async def test_export_handler_runs_digest_in_background(conn):
    """Regression: /export used to await weekly_digest_job inline, blocking
    the webhook for ~30s and risking Telegram retry → duplicate runs. The
    handler should ack fast and spawn a background task.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from bot.handlers import export_handler
    from bot.config import Settings

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-15",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
    )
    providers = Providers(_StubProv([]), None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock()
    update.message.chat = MagicMock(); update.message.chat.id = 99
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot = MagicMock(); context.bot.send_message = AsyncMock()
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    # weekly_digest_job should be SCHEDULED, not awaited by the handler itself.
    import asyncio as _asyncio
    with patch(
        "bot.digest.weekly.weekly_digest_job",
        AsyncMock(return_value=True),
    ) as mock_digest:
        await export_handler(update, context)
        # Handler replied immediately; digest was scheduled as a task
        update.message.reply_text.assert_awaited_once_with("running weekly digest...")
        # Drain the background task — still inside the `with` so the patch
        # stays active until the task has a chance to run.
        pending = [t for t in _asyncio.all_tasks() if t is not _asyncio.current_task()]
        for t in pending:
            try:
                await t
            except Exception:
                pass
        mock_digest.assert_awaited_once()


@pytest.mark.asyncio
async def test_weekly_tweet_fires_after_digest(conn, monkeypatch):
    """Regression: a successful weekly digest with X_WEEKLY_ENABLED + OAuth
    creds must trigger the weekly tweet pipeline and persist the text + sent
    time on the `weekly` row.
    """
    from bot import tweet as tweet_mod
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the impediment to action advances action",
    )

    settings = _settings(
        X_WEEKLY_ENABLED=True,
        X_CONSUMER_KEY="ck", X_CONSUMER_SECRET="cs",
        X_ACCESS_TOKEN="at", X_ACCESS_TOKEN_SECRET="ats",
    )

    prov = _StubProv([
        _valid_digest_json(
            "The impediment to action advances action.", whisper="small", mark="☲",
        ),
    ])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    async def _fake_gen(**kwargs):
        return "☲ advances action"
    async def _fake_post(text, *, settings):
        return tweet_mod.TweetResult(id="42", url="https://x.com/i/web/status/42")
    monkeypatch.setattr(tweet_mod, "generate_weekly_tweet", _fake_gen)
    monkeypatch.setattr(tweet_mod, "post_tweet", _fake_post)

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=settings, providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True

    async with conn.execute(
        "SELECT tweet_text, tweet_posted_at FROM weekly WHERE fz_week_idx = ?",
        (1888,),
    ) as cur:
        row = await cur.fetchone()
    assert row["tweet_text"] == "☲ advances action"
    assert row["tweet_posted_at"]


@pytest.mark.asyncio
async def test_weekly_tweet_skipped_when_disabled(conn, monkeypatch):
    """With X_WEEKLY_ENABLED=false, the tweet generator must not be called."""
    from bot import tweet as tweet_mod
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="a quiet fragment",
    )

    settings = _settings()  # X_WEEKLY_ENABLED defaults to False
    prov = _StubProv([_valid_digest_json("A quiet fragment.", "small", "☲")])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    gen_calls = [0]
    async def _gen_spy(**kwargs):
        gen_calls[0] += 1
        return ""
    monkeypatch.setattr(tweet_mod, "generate_weekly_tweet", _gen_spy)

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=settings, providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True
    assert gen_calls[0] == 0


@pytest.mark.asyncio
async def test_setmark_before_scheduler_does_not_block_digest(conn):
    """Regression: /setmark used to insert with status='processed', which
    caused the scheduler's idempotency check to skip the digest entirely
    for that week. The user would get their mark but no essay/whisper.
    """
    import json as _j
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the impediment to action advances action",
    )
    # Simulate /setmark earlier in the week
    await conn.execute(
        """INSERT INTO weekly (fz_week_idx, iso_week_key, mark, marked_at, status)
           VALUES (?, ?, ?, ?, 'pending')""",
        (1888, "2026-W16", "🌱", "2026-04-19T10:00:00Z"),
    )
    await conn.commit()

    prov = _StubProv([_valid_digest_json(
        "The impediment to action advances action.", whisper="small", mark="☲",
    )])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()

    ok = await weekly.weekly_digest_job(
        conn=conn, settings=_settings(), providers=providers, bot=bot,
        fz_week=1888,
    )
    assert ok is True

    async with conn.execute(
        "SELECT status, essay, whisper, mark FROM weekly WHERE fz_week_idx = ?",
        (1888,),
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "processed"
    assert "impediment" in row["essay"].lower()
    assert row["whisper"] == "small"
    # User's mark wins over LLM's
    assert row["mark"] == "🌱"


@pytest.mark.asyncio
async def test_export_stores_returned_sha_on_weekly_row(conn):
    """`weekly.fz_export_sha` captures whatever GitHub returned from the
    probe-then-push cycle for the record (not used as a cache anymore)."""
    await _insert_text_capture(
        conn, fz_week_idx=1888, raw="the only fragment",
    )
    prov = _StubProv([
        json.dumps({"essay": "The only fragment.", "whisper": "x", "mark": "☲"}),
    ])
    providers = Providers(prov, None)
    bot = MagicMock(); bot.send_message = AsyncMock()
    settings = _settings(GITHUB_TOKEN="ghp_test", GITHUB_REPO="u/r")

    with patch("bot.digest.weekly._put_with_auto_sha",
               AsyncMock(return_value="returned-sha")):
        await weekly.weekly_digest_job(
            conn=conn, settings=settings, providers=providers, bot=bot,
            fz_week=1888,
        )
    async with conn.execute(
        "SELECT fz_export_sha FROM weekly WHERE fz_week_idx = 1888",
    ) as cur:
        row = await cur.fetchone()
    assert row["fz_export_sha"] == "returned-sha"
