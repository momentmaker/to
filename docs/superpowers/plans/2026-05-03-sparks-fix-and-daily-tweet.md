# Sparks Fix + Daily Tweet Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the broken `sparks.md` newline format (Track A) and add a daily tweet pipeline that combines opted-in captures into themed, orchurator-voiced tweets with one-at-a-time Telegram approval (Track B).

**Architecture:**
- Track A moves spark write server-side into the bot scheduler (deterministic Python, replaces unreliable cloud-routine prompt) and ships a one-time backfill script for the broken entries.
- Track B adds `bot/tweet_daily.py` (selection + theme detection + stitch generation + assembly), `bot/tweet_validate.py` (stitch + length validators), a `tweets` ledger table, an opt-in `tweetable` flag on capture payloads, four new approval commands (`/post`, `/next`, `/edit`, `/skip`-extension), two opt-in commands (`/tweetable`, `/untweetable`), and one new APScheduler cron + interval job. Default-deny: nothing posts until the user explicitly opts captures in AND flips `TWEET_DAILY_V2_ENABLED=true`.

**Tech Stack:** Python 3.x, aiosqlite, APScheduler, python-telegram-bot, tweepy (already pinned), tomli_w, grapheme. Reuses existing `bot/llm/router.py` for LLM calls (purposes `ingest` for spark/theme, `tweet` for stitch), `bot/github_sync.py` for repo writes, `bot/digest/validate.py:validate_quote_only` for the substring validator.

**Reference spec:** `docs/superpowers/specs/2026-05-03-sparks-fix-and-daily-tweet-design.md`

---

## File Structure Overview

**Track A — new files:**
- `bot/sparks.py` — spark selection + file write
- `scripts/normalize_sparks.py` — one-time backfill
- `tests/test_sparks.py`
- `tests/test_normalize_sparks.py`

**Track A — modified files:**
- `bot/scheduler.py` — register `daily_sparks_job`
- `bot/config.py` — `SPARKS_ENABLED`, `SPARKS_LOCAL_TIME`
- `.claude/routines/daily.md` — mark Step 4 deprecated

**Track B — new files:**
- `bot/tweet_daily.py` — pipeline orchestration + state
- `bot/tweet_validate.py` — stitch + total-length validators
- `tests/test_tweet_validate.py`
- `tests/test_tweet_daily_select.py`
- `tests/test_tweet_daily_stitch.py`
- `tests/test_tweet_daily_assemble.py`
- `tests/test_tweet_daily_state.py`
- `tests/test_tweet_handlers.py`
- `tests/test_tweetable_handlers.py`

**Track B — modified files:**
- `bot/db.py` — `_MIGRATION_V3` adding `tweets` table + index
- `bot/prompts.py` — `SYSTEM_TWEET_STITCH` + few-shot
- `bot/handlers.py` — `/post`, `/next`, `/edit`, `/skip` extension, `/tweetable`, `/untweetable`, `/status` extension
- `bot/bot_app.py` — register new command handlers + boot OAuth gate
- `bot/scheduler.py` — register `daily_tweet_draft_job` + `tweet_draft_expiry`
- `bot/markdown_out.py` — render `tweetable` flag in frontmatter when set
- `bot/config.py` — `TWEET_DAILY_V2_ENABLED`, `TWEET_DRAFT_LOCAL_TIME`, `TWEET_NEXT_CAP`, `TWEET_POOL_DAYS`
- `README.md` — commands + env vars + BotFather block

---

# Track A — Sparks Fix

## Task A1: Backfill script — `scripts/normalize_sparks.py`

**Files:**
- Create: `scripts/normalize_sparks.py`
- Test: `tests/test_normalize_sparks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_normalize_sparks.py
from pathlib import Path

from scripts.normalize_sparks import normalize_sparks_text


BROKEN = """# sparks

2026-04-22 — crazy last of privacy for employees - literally like neo-serfs

2026-04-23 — we are all just arbitrager of tokens now
2026-04-24 — i like things to be automated as much as i can 🙂
2026-04-25 — the contrast between height of intelligence and just simple piece of art
"""


def test_normalize_inserts_blank_lines_between_jammed_entries():
    out = normalize_sparks_text(BROKEN)
    expected = """# sparks

2026-04-22 — crazy last of privacy for employees - literally like neo-serfs

2026-04-23 — we are all just arbitrager of tokens now

2026-04-24 — i like things to be automated as much as i can 🙂

2026-04-25 — the contrast between height of intelligence and just simple piece of art
"""
    assert out == expected


def test_normalize_is_idempotent():
    once = normalize_sparks_text(BROKEN)
    twice = normalize_sparks_text(once)
    assert once == twice


def test_normalize_preserves_header_and_trailing_newline():
    out = normalize_sparks_text(BROKEN)
    assert out.startswith("# sparks\n")
    assert out.endswith("\n")


def test_normalize_handles_empty_file():
    assert normalize_sparks_text("") == ""


def test_normalize_handles_header_only():
    assert normalize_sparks_text("# sparks\n") == "# sparks\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_normalize_sparks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.normalize_sparks'` (or similar import error).

- [ ] **Step 3: Implement the script**

```python
# scripts/normalize_sparks.py
"""One-time backfill for `sparks.md` blank-line spacing.

The Claude Code Routine that previously appended sparks ignored the
Python normalization block in its prompt and used shell append, leading
to entries from 2026-04-23 onward sharing a single Markdown paragraph.

Idempotent: running on an already-correct file produces the same bytes.

Usage:
    python scripts/normalize_sparks.py path/to/sparks.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s")


def normalize_sparks_text(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _ENTRY_RE.match(line):
            # Ensure exactly one blank line precedes every entry, except
            # the very first one (which keeps its existing position).
            if out and out[-1] != "":
                out.append("")
        out.append(line)
    rendered = "\n".join(out).rstrip("\n") + "\n"
    return rendered


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: normalize_sparks.py <sparks.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    out = normalize_sparks_text(text)
    if out == text:
        print(f"{path}: already normalized")
        return 0
    path.write_text(out, encoding="utf-8")
    print(f"{path}: normalized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_normalize_sparks.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/normalize_sparks.py tests/test_normalize_sparks.py
git commit -m "feat(sparks): add idempotent backfill script for sparks.md spacing"
```

---

## Task A2: Run backfill on captures repo (manual, no code commit)

**Files:** None in this repo. Operates on `~/GitHub/momentmaker/self/sparks.md`.

- [ ] **Step 1: Pull latest captures repo state**

```bash
cd ~/GitHub/momentmaker/self
git pull
```
Expected: `Already up to date.` or new commits pulled.

- [ ] **Step 2: Run the backfill script (dry — diff first)**

```bash
cp sparks.md /tmp/sparks-before.md
python ~/GitHub/momentmaker/to/scripts/normalize_sparks.py sparks.md
diff /tmp/sparks-before.md sparks.md
```
Expected diff: blank lines inserted between `2026-04-23` and subsequent entries; nothing else changes.

- [ ] **Step 3: Verify rendered output looks right**

```bash
cat sparks.md | head -40
```
Expected: every entry on its own paragraph, separated by a blank line, header preserved.

- [ ] **Step 4: Commit + push from the captures repo**

```bash
git add sparks.md
git commit -m "fix(sparks): normalize blank-line spacing for 2026-04-23 onward"
git push
```

- [ ] **Step 5: Sanity-check on GitHub**

Open `https://github.com/momentmaker/self/blob/master/sparks.md` in browser. Each entry should render as its own paragraph.

---

## Task A3: New `bot/sparks.py` — `select_spark`

**Files:**
- Create: `bot/sparks.py`
- Test: `tests/test_sparks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sparks.py
from __future__ import annotations

import pytest

import aiosqlite

from bot import sparks
from bot.config import Settings
from bot.db import init_schema
from tests.helpers.fakes import FakeProviders, fake_settings  # see step 1a if absent


@pytest.mark.asyncio
async def test_select_spark_returns_substring_of_a_capture(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                  iso_week_key, fz_week_idx, status)
            VALUES ('text', 'crazy last of privacy for employees - literally like neo-serfs',
                    '{}', '2026-04-22T12:00:00Z', '2026-04-22', '2026-W17', 1888, 'done')
            """
        )
        await conn.commit()

        async def fake_call(*, purpose, system_blocks, messages, max_tokens,
                            settings, providers, conn):
            class R: text = '{"line": "crazy last of privacy for employees"}'
            return R()
        monkeypatch.setattr("bot.sparks.call_llm", fake_call)

        line = await sparks.select_spark(
            conn, local_date="2026-04-22",
            settings=settings, providers=FakeProviders(),
        )
        assert line == "crazy last of privacy for employees"


@pytest.mark.asyncio
async def test_select_spark_none_when_no_captures(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        line = await sparks.select_spark(
            conn, local_date="2026-04-22",
            settings=settings, providers=FakeProviders(),
        )
        assert line is None


@pytest.mark.asyncio
async def test_select_spark_skips_when_llm_pick_not_substring(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                  iso_week_key, fz_week_idx, status)
            VALUES ('text', 'one capture body', '{}', '2026-04-22T12:00:00Z',
                    '2026-04-22', '2026-W17', 1888, 'done')
            """
        )
        await conn.commit()

        async def fake_call(**_):
            class R: text = '{"line": "totally invented sentence not present"}'
            return R()
        monkeypatch.setattr("bot.sparks.call_llm", fake_call)

        line = await sparks.select_spark(
            conn, local_date="2026-04-22",
            settings=settings, providers=FakeProviders(),
        )
        assert line is None
```

- [ ] **Step 1a: Ensure test helpers exist**

Check `tests/helpers/fakes.py` exists (the project has 275 tests; fakes likely exist already). If `FakeProviders` and `fake_settings` are missing, add minimal versions:

```python
# tests/helpers/fakes.py — only add if missing
from bot.config import Settings


def fake_settings(**overrides) -> Settings:
    base = dict(
        TELEGRAM_BOT_TOKEN="t", TELEGRAM_OWNER_ID=1,
        DOB="1990-01-01", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="x",
        SQLITE_PATH=":memory:",
    )
    base.update(overrides)
    return Settings(**base)


class FakeProviders:
    anthropic = None
    openai = None

    def pick(self, name, *, purpose=""):
        return self
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sparks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.sparks'`.

- [ ] **Step 3: Implement `select_spark`**

```python
# bot/sparks.py
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
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_SPARK  # added in Task A4

log = logging.getLogger(__name__)

_MIN_LEN = 8
_MAX_LEN = 200
_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s")


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
        # Also include scraped text from URL captures so URL-day captures
        # can spark from their substantive content, not just the bare URL.
        if r["payload"]:
            try:
                p = json.loads(r["payload"])
                scrape = (p.get("scrape") or {})
                txt = scrape.get("text") if isinstance(scrape, dict) else None
                if isinstance(txt, str) and txt.strip():
                    bodies.append(txt.strip())
                tx = p.get("transcript")
                if isinstance(tx, str) and tx.strip():
                    bodies.append(tx.strip())
            except (json.JSONDecodeError, KeyError):
                pass
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
    appending. Two retries before giving up: first attempt + one retry,
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
```

- [ ] **Step 4: Add `SYSTEM_SPARK` placeholder to `bot/prompts.py`**

Open `bot/prompts.py` and add at the bottom:

```python
SYSTEM_SPARK = """\
You read one day's captures from a private commonplace book. Pick ONE
sentence — the sharpest, most self-contained line worth re-reading a
year from now. Rules:

- Must be a verbatim substring of one capture body. No paraphrasing.
  Trimming leading/trailing words is fine.
- Between 8 and 200 characters.
- Not a URL. Not a title. Not a page number.
- Prefer the user's own words (reflection, why, plain text) over
  scraped article body when both qualify.
- If nothing meets the bar, return an empty `line` field — silence is
  better than a forced pick.

Reply with JSON only:

    {"line": "<the chosen verbatim line>"}
"""
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_sparks.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add bot/sparks.py bot/prompts.py tests/test_sparks.py tests/helpers/fakes.py
git commit -m "feat(sparks): add server-side spark selection with substring validation"
```

---

## Task A4: `bot/sparks.py` — `append_spark` writer

**Files:**
- Modify: `bot/sparks.py` (extend)
- Test: `tests/test_sparks.py` (extend)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sparks.py`:

```python
def test_append_spark_to_empty_file(tmp_path):
    p = tmp_path / "sparks.md"
    sparks.append_spark(p, date="2026-05-03", line="hello world")
    assert p.read_text() == "# sparks\n\n2026-05-03 — hello world\n"


def test_append_spark_to_header_only(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n")
    sparks.append_spark(p, date="2026-05-03", line="hello world")
    assert p.read_text() == "# sparks\n\n2026-05-03 — hello world\n"


def test_append_spark_inserts_blank_line(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n2026-05-02 — yesterday\n")
    sparks.append_spark(p, date="2026-05-03", line="today")
    assert p.read_text() == (
        "# sparks\n\n2026-05-02 — yesterday\n\n2026-05-03 — today\n"
    )


def test_append_spark_strips_extra_trailing_newlines(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n2026-05-02 — yesterday\n\n\n\n")
    sparks.append_spark(p, date="2026-05-03", line="today")
    assert p.read_text() == (
        "# sparks\n\n2026-05-02 — yesterday\n\n2026-05-03 — today\n"
    )


def test_append_spark_idempotent_on_duplicate_last_entry(tmp_path):
    p = tmp_path / "sparks.md"
    p.write_text("# sparks\n\n2026-05-03 — already here\n")
    sparks.append_spark(p, date="2026-05-03", line="already here")
    assert p.read_text() == "# sparks\n\n2026-05-03 — already here\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sparks.py -v -k append`
Expected: 5 FAIL with `AttributeError: module 'bot.sparks' has no attribute 'append_spark'`.

- [ ] **Step 3: Implement `append_spark`**

Append to `bot/sparks.py`:

```python
_HEADER = "# sparks\n"


def append_spark(path: Path, *, date: str, line: str) -> None:
    """Append `<date> — <line>` to sparks.md preserving blank-line spacing.

    Idempotent: re-appending the same `date — line` as the current last
    entry is a no-op.
    """
    new_entry = f"{date} — {line.strip()}"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if existing:
        # Idempotent check: if the last non-blank line is exactly the new
        # entry, do nothing.
        for prev in reversed(existing.splitlines()):
            if prev.strip():
                if prev == new_entry:
                    return
                break

    if not existing:
        body = _HEADER + "\n" + new_entry + "\n"
    else:
        # Strip trailing newlines, ensure exactly two before appending.
        normalized = existing.rstrip("\n")
        if not normalized.endswith(_HEADER.rstrip("\n")):
            normalized += "\n"  # one trailing newline so the blank-line below sticks
        body = normalized + "\n\n" + new_entry + "\n"

    path.write_text(body, encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_sparks.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/sparks.py tests/test_sparks.py
git commit -m "feat(sparks): add idempotent append_spark writer with blank-line preservation"
```

---

## Task A5: `daily_sparks_job` + scheduler registration + config

**Files:**
- Modify: `bot/sparks.py` (add job)
- Modify: `bot/scheduler.py` (register)
- Modify: `bot/config.py` (env vars)
- Test: `tests/test_sparks.py` (extend)

- [ ] **Step 1: Add config vars**

Open `bot/config.py`. Add after the existing schedule block:

```python
    SPARKS_ENABLED: bool = True
    SPARKS_LOCAL_TIME: str = "06:00"
```

- [ ] **Step 2: Write the failing job test**

Add to `tests/test_sparks.py`:

```python
@pytest.mark.asyncio
async def test_daily_sparks_job_writes_and_pushes(monkeypatch, tmp_path):
    settings = fake_settings(GITHUB_TOKEN="t", GITHUB_REPO="x/y")

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                  iso_week_key, fz_week_idx, status)
            VALUES ('text', 'a sharp line worth keeping', '{}',
                    '2026-05-02T12:00:00Z', '2026-05-02', '2026-W18', 1900, 'done')
            """
        )
        await conn.commit()

        async def fake_call(**_):
            class R: text = '{"line": "a sharp line worth keeping"}'
            return R()
        monkeypatch.setattr("bot.sparks.call_llm", fake_call)

        captured: dict = {}
        async def fake_fetch_file(*, settings, path, client=None):
            return ("# sparks\n", "deadbeef")
        async def fake_put_file(*, settings, path, content, message,
                                existing_sha=None, client=None):
            captured["path"] = path
            captured["content"] = content
            captured["sha"] = existing_sha
            return "newsha"
        monkeypatch.setattr("bot.sparks.fetch_file", fake_fetch_file)
        monkeypatch.setattr("bot.sparks.put_file", fake_put_file)

        ok = await sparks.daily_sparks_job(
            conn=conn, settings=settings, providers=FakeProviders(),
            yesterday="2026-05-02",
        )
        assert ok is True
        assert captured["path"] == "sparks.md"
        assert "2026-05-02 — a sharp line worth keeping" in captured["content"]
        assert captured["sha"] == "deadbeef"
```

- [ ] **Step 3: Run test to verify failure**

Run: `python -m pytest tests/test_sparks.py::test_daily_sparks_job_writes_and_pushes -v`
Expected: FAIL — `daily_sparks_job` does not exist.

- [ ] **Step 4: Implement `daily_sparks_job`**

Append to `bot/sparks.py`:

```python
import io

from bot.github_sync import fetch_file, is_configured as github_configured, put_file


_SPARKS_FILENAME = "sparks.md"


def _normalize_in_memory(existing: str, *, date: str, line: str) -> str:
    """Same logic as `append_spark` but operating on a string in memory.
    Used by the cloud-driven job so we can PUT the result via the
    GitHub contents API without a local clone."""
    new_entry = f"{date} — {line.strip()}"
    if existing:
        for prev in reversed(existing.splitlines()):
            if prev.strip():
                if prev == new_entry:
                    return existing
                break
    if not existing:
        return _HEADER + "\n" + new_entry + "\n"
    normalized = existing.rstrip("\n") + "\n"
    return normalized + "\n" + new_entry + "\n"


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
    new_content = _normalize_in_memory(existing, date=yesterday, line=line)
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
```

- [ ] **Step 5: Register in scheduler**

Open `bot/scheduler.py`. Add import:

```python
from bot import github_sync, process, reflection, sparks
```

Inside `build_scheduler`, after the `nightly_sync` block and before the `if bot is not None:` block, add:

```python
    # Daily sparks: pick yesterday's sharpest verbatim line and append to
    # sparks.md in the captures repo. Server-side replacement for the
    # previously-Routine-driven Step 4.
    if settings.SPARKS_ENABLED:
        sh, sm = _parse_hhmm(settings.SPARKS_LOCAL_TIME)

        async def _spark_wrapper():
            from datetime import timedelta
            today_local = local_date_for(
                datetime.now(timezone.utc), settings.TIMEZONE,
            )
            yesterday = (today_local - timedelta(days=1)).isoformat()
            try:
                await sparks.daily_sparks_job(
                    conn=conn, settings=settings,
                    providers=providers, yesterday=yesterday,
                )
            except Exception:
                log.exception("daily_sparks_job failed")

        scheduler.add_job(
            _spark_wrapper,
            trigger=CronTrigger(hour=sh, minute=sm, timezone=settings.TIMEZONE),
            id="daily_sparks",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_sparks.py -v`
Expected: 9 passed.

Run full suite to ensure no regression: `python -m pytest tests/ -v`
Expected: all pre-existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add bot/sparks.py bot/scheduler.py bot/config.py tests/test_sparks.py
git commit -m "feat(sparks): wire daily_sparks_job into the scheduler"
```

---

## Task A6: Deprecate Step 4 in `.claude/routines/daily.md`

**Files:**
- Modify: `.claude/routines/daily.md`

- [ ] **Step 1: Edit the routine doc**

Open `.claude/routines/daily.md`. Find the `## Step 4 — Pick the spark` section. Replace its body (NOT the section header) with:

```markdown
## Step 4 — Pick the spark

**DEPRECATED — superseded by `bot/sparks.py:daily_sparks_job`.** The
bot's APScheduler now picks and writes the daily spark deterministically
at `SPARKS_LOCAL_TIME` (default 06:00 local). Skip this step entirely.

Skip directly to Step 5.
```

- [ ] **Step 2: Verify rendering**

```bash
grep -n "Step 4\|Step 5\|DEPRECATED" .claude/routines/daily.md
```
Expected: Step 4 marked deprecated, Step 5 still present and intact.

- [ ] **Step 3: Commit**

```bash
git add .claude/routines/daily.md
git commit -m "docs(routine): mark Step 4 (sparks) deprecated, superseded by bot job"
```

---

## Track A — Completion checkpoint

After A1-A6, Track A ships independently:

- [ ] Backfill committed to captures repo (`git log` in `~/GitHub/momentmaker/self`)
- [ ] All `tests/test_sparks.py` and `tests/test_normalize_sparks.py` green
- [ ] Routine doc updated
- [ ] Bot redeployed; next 06:00 local fires `daily_sparks_job`
- [ ] Manually verify next morning: `sparks.md` has new entry with proper blank-line spacing

If any of the above isn't green, fix before starting Track B.

---

# Track B — Daily Tweet Pipeline

## Task B1: DB migration — `tweets` table

**Files:**
- Modify: `bot/db.py`
- Test: `tests/test_db_migrations.py` (extend, or new)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_db_migrations.py` (create if absent):

```python
import pytest
import aiosqlite

from bot.db import init_schema


@pytest.mark.asyncio
async def test_tweets_table_exists_after_init():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tweets'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None


@pytest.mark.asyncio
async def test_tweets_theme_index_exists():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='tweets_theme_idx'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None


@pytest.mark.asyncio
async def test_tweets_table_accepts_full_row():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                                theme, stitch, text, draft_count, edited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("1789", "2026-05-03T14:14:00Z", "2026-05-03",
             '["a","b"]', "privacy", "you caught it.", "tweet text", 1, 0),
        )
        await conn.commit()
        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_db_migrations.py -v`
Expected: FAIL — `tweets` table does not exist.

- [ ] **Step 3: Add migration v3**

Open `bot/db.py`. Add after `_MIGRATION_V2`:

```python
_MIGRATION_V3 = """
CREATE TABLE IF NOT EXISTS tweets (
  tweet_id     TEXT PRIMARY KEY,
  tweeted_at   TEXT NOT NULL,
  local_date   TEXT NOT NULL,
  capture_ids  TEXT NOT NULL,
  theme        TEXT,
  stitch       TEXT,
  text         TEXT NOT NULL,
  draft_count  INTEGER NOT NULL,
  edited       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tweets_theme_idx ON tweets(theme);
CREATE INDEX IF NOT EXISTS tweets_local_date_idx ON tweets(local_date);
"""
```

Append `_MIGRATION_V3` to the `MIGRATIONS` list:

```python
MIGRATIONS: list[str] = [
    _MIGRATION_V1,
    _MIGRATION_V2,
    _MIGRATION_V3,
]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_db_migrations.py -v`
Expected: 3 passed.

Run full suite: `python -m pytest tests/ -v`
Expected: all pre-existing tests pass (migration is additive).

- [ ] **Step 5: Commit**

```bash
git add bot/db.py tests/test_db_migrations.py
git commit -m "feat(db): add tweets ledger table (migration v3)"
```

---

## Task B2: `bot/tweet_validate.py` — `validate_stitch`

**Files:**
- Create: `bot/tweet_validate.py`
- Test: `tests/test_tweet_validate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tweet_validate.py
from bot.tweet_validate import validate_stitch


def _ok(text):
    ok, reason = validate_stitch(text)
    assert ok, f"expected pass, got {reason!r}"


def _bad(text, expect_in_reason):
    ok, reason = validate_stitch(text)
    assert not ok, "expected fail"
    assert expect_in_reason in reason, f"reason {reason!r} missing {expect_in_reason!r}"


def test_valid_stitch_passes():
    _ok("you caught the same asymmetry twice.")


def test_empty_fails():
    _bad("", "empty")


def test_too_long_word_count_fails():
    _bad(
        "you caught the same asymmetry twice and again and again and again and again and again.",
        "words",
    )


def test_too_long_chars_fails():
    _bad(
        "you noticed " + "x" * 200,
        "chars",
    )


def test_first_person_singular_fails():
    _bad("i think you caught it.", "first-person")
    _bad("to me this rhymes.", "first-person")
    _bad("my read is you noticed.", "first-person")


def test_forbidden_verb_fails():
    _bad("you should keep going.", "forbidden")
    _bad("you must notice this.", "forbidden")
    _bad("you will see this again.", "forbidden")


def test_question_mark_fails():
    _bad("did you notice this?", "punctuation")


def test_exclamation_fails():
    _bad("you caught it again!", "punctuation")


def test_hashtag_fails():
    _bad("you caught it #privacy.", "punctuation")


def test_ellipsis_fails():
    _bad("you noticed... again.", "punctuation")
    _bad("you noticed … again.", "punctuation")


def test_two_sentences_fails():
    _bad("you caught it. you kept it.", "sentence")


def test_period_terminator_optional():
    _ok("you caught the asymmetry —")
    _ok("you caught the asymmetry")  # no terminator allowed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tweet_validate.py -v`
Expected: all FAIL with `ModuleNotFoundError: No module named 'bot.tweet_validate'`.

- [ ] **Step 3: Implement `validate_stitch`**

```python
# bot/tweet_validate.py
"""Validators for the daily tweet pipeline.

Two functions:
- validate_stitch: bounds the orchurator stitch sentence to its
  declared shape (length, person, vocabulary, punctuation).
- validate_tweet_total_length: enforces X's 280-grapheme hard limit
  on the assembled tweet, with t.co URL accounting.
"""

from __future__ import annotations

import re

import grapheme


_FORBIDDEN_VERBS = {
    "should", "must", "ought", "will", "predict", "recommend",
    "advise", "urge", "encourage", "warn",
}
_FIRST_PERSON_TOKENS = {
    "i", "me", "my", "mine", "i'm", "i'd", "i'll", "i've",
    "to-me",  # composite caught after splitting; see token logic
}
_WORD_RE = re.compile(r"[\w']+", flags=re.UNICODE)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def validate_stitch(text: str) -> tuple[bool, str | None]:
    """Return (ok, reason). reason is None when ok is True."""
    s = (text or "").strip()
    if not s:
        return False, "empty stitch"

    # Punctuation rules first — cheap and explicit.
    if "?" in s:
        return False, "punctuation: '?' not allowed"
    if "!" in s:
        return False, "punctuation: '!' not allowed"
    if "#" in s:
        return False, "punctuation: '#' not allowed"
    if "..." in s or "…" in s:
        return False, "punctuation: ellipsis not allowed"

    # Length rules
    char_count = grapheme.length(s)
    if char_count > 80:
        return False, f"chars: {char_count} > 80"
    words = _words(s)
    if not words:
        return False, "empty stitch"
    if len(words) > 15:
        return False, f"words: {len(words)} > 15"

    # First-person check
    for tok in words:
        if tok in _FIRST_PERSON_TOKENS:
            return False, f"first-person token: {tok!r}"
    # "to me" composite
    if re.search(r"\bto me\b", s.lower()):
        return False, "first-person token: 'to me'"

    # Forbidden verbs
    for tok in words:
        if tok in _FORBIDDEN_VERBS:
            return False, f"forbidden verb: {tok!r}"

    # Sentence count: at most one terminal punctuation mark anywhere
    # internal sentence-end means multiple sentences.
    body = s.rstrip(".—")  # strip optional terminator
    if re.search(r"[.!?]", body):
        return False, "sentence: more than one"

    return True, None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_tweet_validate.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_validate.py tests/test_tweet_validate.py
git commit -m "feat(tweet): add validate_stitch — bounded orchurator voice rules"
```

---

## Task B3: `validate_tweet_total_length`

**Files:**
- Modify: `bot/tweet_validate.py` (extend)
- Modify: `tests/test_tweet_validate.py` (extend)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tweet_validate.py`:

```python
from bot.tweet_validate import validate_tweet_total_length


def test_short_tweet_passes():
    ok, reason = validate_tweet_total_length("hello world")
    assert ok and reason is None


def test_at_280_passes():
    text = "x" * 280
    ok, reason = validate_tweet_total_length(text)
    assert ok


def test_281_fails():
    text = "x" * 281
    ok, reason = validate_tweet_total_length(text)
    assert not ok
    assert "281" in reason


def test_url_counted_as_23():
    body = "x" * 257
    text = body + " https://example.com/this-is-much-longer-than-23-chars/foo/bar/baz"
    ok, reason = validate_tweet_total_length(text)
    assert ok, f"got {reason!r}"


def test_url_over_when_combined_with_long_body_fails():
    body = "x" * 258
    text = body + " https://example.com/short"
    ok, reason = validate_tweet_total_length(text)
    assert not ok


def test_emoji_counts_as_one_grapheme():
    text = "🙂" * 280
    ok, reason = validate_tweet_total_length(text)
    assert ok
    text281 = "🙂" * 281
    ok, reason = validate_tweet_total_length(text281)
    assert not ok
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_validate.py -v -k total_length`
Expected: 6 FAIL — function does not exist.

- [ ] **Step 3: Implement**

Append to `bot/tweet_validate.py`:

```python
_TWEET_MAX = 280
_TCO_LEN = 23
_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)


def validate_tweet_total_length(text: str) -> tuple[bool, str | None]:
    """Enforce X's 280-grapheme hard limit. Each https?:// URL counts as
    23 chars (t.co length) regardless of original length."""
    s = text or ""
    # Replace URLs with a placeholder of the t.co length.
    placeholder = "x" * _TCO_LEN
    measured = _URL_RE.sub(placeholder, s)
    n = grapheme.length(measured)
    if n > _TWEET_MAX:
        return False, f"length: {n} > {_TWEET_MAX}"
    return True, None
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_validate.py -v`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_validate.py tests/test_tweet_validate.py
git commit -m "feat(tweet): add validate_tweet_total_length with t.co URL accounting"
```

---

## Task B4: `SYSTEM_TWEET_STITCH` prompt

**Files:**
- Modify: `bot/prompts.py`

- [ ] **Step 1: Add prompt + few-shot block**

Open `bot/prompts.py`. Append:

```python
SYSTEM_TWEET_STITCH = """\
You write ONE short sentence that stitches two or three captures from a
private commonplace book. The sentence is the only original prose in the
tweet — the rest is verbatim quotes and dates. The tweet is read by
strangers but written for the author.

You are the orchurator. You never perform wisdom. You stitch, name,
frame, observe — you do not advise, predict, encourage, judge, or rally.

Hard rules — output is rejected if any are broken:
- One sentence only. No questions, no exclamations, no hashtags, no
  emoji, no ellipsis.
- Between 1 and 15 words. Between 1 and 80 characters.
- Second-person observation only ("you caught", "you keep", "you saw",
  "you noticed"). NO first-person ("i", "me", "my", "to me", "i think").
- No advice verbs: should, must, ought, will, predict, recommend,
  advise, urge, encourage, warn.
- End with a period or em-dash, or no punctuation. Do not end mid-clause.

Tone: small, declarative, foolsage. Not clever. Not viral. Not hopeful.
Not motivational. Not "we all" or "everyone."

You receive a theme label and 2-3 capture bodies with their dates. Find
the rhyme in your own words.

Reply with JSON only:

    {"stitch": "<the sentence>"}

Example shapes (do NOT copy the wording — these are scaffolds):

  Theme: privacy-asymmetry
  Captures: "crazy last of privacy for employees" (2026-04-22),
            "didn't even know someone kept this data" (2026-04-21)
  Stitch: "both times you caught the asymmetry between what's kept on
           you and what you keep."

  Theme: automation-as-craft
  Captures: "i like things to be automated as much as i can" (2026-04-24),
            "i learned a few new things too like using samurai swords
             to cut the thoughts/images with 2 slashes" (2026-04-26)
  Stitch: "you reach for the smaller blade, even in code."

  Theme: tokens-and-art
  Captures: "we are all just arbitrager of tokens now" (2026-04-23),
            "the contrast between height of intelligence and just simple
             piece of art" (2026-04-25)
  Stitch: "you keep marking what tokens cannot price."
"""
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from bot.prompts import SYSTEM_TWEET_STITCH; print(len(SYSTEM_TWEET_STITCH))"
```
Expected: a positive integer (~1500+).

- [ ] **Step 3: Commit**

```bash
git add bot/prompts.py
git commit -m "feat(prompts): add SYSTEM_TWEET_STITCH bounded orchurator-voice prompt"
```

---

## Task B5: `bot/tweet_daily.py` — `pick_eligible_pool`

**Files:**
- Create: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_select.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tweet_daily_select.py
import json
import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


async def _add_capture(conn, *, raw, kind="text", local_date="2026-05-01",
                       payload=None):
    await conn.execute(
        """
        INSERT INTO captures (kind, raw, payload, created_at, local_date,
                              iso_week_key, fz_week_idx, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'done')
        """,
        (kind, raw, json.dumps(payload or {}),
         f"{local_date}T12:00:00Z", local_date, "2026-W18", 1900),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_empty_pool_when_nothing_flagged():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="hi", payload={})
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert rows == []


@pytest.mark.asyncio
async def test_pool_includes_only_tweetable():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="not flagged", payload={})
        await _add_capture(conn, raw="yes please", payload={"tweetable": True})
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert len(rows) == 1
        assert rows[0]["raw"] == "yes please"


@pytest.mark.asyncio
async def test_pool_excludes_why_and_highlight():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="why", kind="why",
                           payload={"tweetable": True})
        await _add_capture(conn, raw="hl", kind="highlight",
                           payload={"tweetable": True})
        await _add_capture(conn, raw="text", kind="text",
                           payload={"tweetable": True})
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert [r["raw"] for r in rows] == ["text"]


@pytest.mark.asyncio
async def test_pool_excludes_already_tweeted():
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, raw="a", payload={"tweetable": True})
        await _add_capture(conn, raw="b", payload={"tweetable": True})
        # Mark capture id=1 as tweeted
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                                text, draft_count)
            VALUES ('t1', '2026-05-01T01:00:00Z', '2026-05-01', '[1]',
                    'tweet', 1)
            """
        )
        await conn.commit()
        rows = await tweet_daily.pick_eligible_pool(conn, settings=settings)
        assert [r["raw"] for r in rows] == ["b"]


@pytest.mark.asyncio
async def test_pool_window_falls_back_to_full_corpus(monkeypatch):
    """If the 14-day window has fewer than 2 candidates, expand to full corpus."""
    settings = fake_settings(TWEET_POOL_DAYS=14)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # one recent, one old (>14 days)
        await _add_capture(conn, raw="recent", local_date="2026-05-01",
                           payload={"tweetable": True})
        await _add_capture(conn, raw="ancient", local_date="2024-01-01",
                           payload={"tweetable": True})
        # Pin "today" so the window math is deterministic.
        rows = await tweet_daily.pick_eligible_pool(
            conn, settings=settings, today_iso="2026-05-03",
        )
        # Pool should include both because the recent-only count (1) <
        # threshold of 2.
        assert len(rows) == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_tweet_daily_select.py -v`
Expected: FAIL — `bot.tweet_daily` does not exist.

- [ ] **Step 3: Add config var**

In `bot/config.py`, add:

```python
    TWEET_DAILY_V2_ENABLED: bool = False
    TWEET_DRAFT_LOCAL_TIME: str = "09:00"
    TWEET_NEXT_CAP: int = 5
    TWEET_POOL_DAYS: int = 14
```

- [ ] **Step 4: Implement `pick_eligible_pool`**

```python
# bot/tweet_daily.py
"""Daily tweet pipeline: pick captures, find a theme, generate a stitch,
draft a tweet, gate on Telegram approval, post to X, ledger.

See `docs/superpowers/specs/2026-05-03-sparks-fix-and-daily-tweet-design.md`
for the full design.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

import aiosqlite

from bot.config import Settings

log = logging.getLogger(__name__)


async def pick_eligible_pool(
    conn: aiosqlite.Connection,
    *,
    settings: Settings,
    today_iso: str | None = None,
) -> list[aiosqlite.Row]:
    """Captures eligible for tweeting today.

    Filters (all must pass):
    - kind in (text, url, voice, image, pdf, reflection)
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
        base_query + " AND c.local_date >= ? ORDER BY c.local_date DESC, c.id DESC",
        (window_start,),
    ) as cur:
        recent = list(await cur.fetchall())
    if len(recent) >= 2:
        return recent

    async with conn.execute(
        base_query + " ORDER BY c.local_date DESC, c.id DESC",
    ) as cur:
        return list(await cur.fetchall())
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_tweet_daily_select.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add bot/tweet_daily.py bot/config.py tests/test_tweet_daily_select.py
git commit -m "feat(tweet): add pick_eligible_pool — tweetable opt-in + window fallback"
```

---

## Task B6: `detect_themes` and `pick_theme`

**Files:**
- Modify: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_stitch.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tweet_daily_stitch.py
import json
import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import FakeProviders, fake_settings


@pytest.mark.asyncio
async def test_detect_themes_returns_proposals(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps([
                    {"theme": "privacy", "capture_ids": [1, 2],
                     "rationale": "both about kept data"},
                ])
            return R()
        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        proposals = await tweet_daily.detect_themes(
            pool_summary="[1] privacy snip\n[2] kept data snip",
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert len(proposals) == 1
        assert proposals[0].theme == "privacy"
        assert proposals[0].capture_ids == [1, 2]


@pytest.mark.asyncio
async def test_pick_theme_least_used_first():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # Pre-existing tweets: 'privacy' tweeted 3x, 'craft' tweeted 1x.
        for i in range(3):
            await conn.execute(
                """
                INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                                    theme, text, draft_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"p{i}", "2026-05-01T01:00:00Z", "2026-05-01", "[]",
                 "privacy", "x", 1),
            )
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                                theme, text, draft_count)
            VALUES ('c1', '2026-05-02T01:00:00Z', '2026-05-02', '[]',
                    'craft', 'x', 1)
            """
        )
        await conn.commit()

        props = [
            tweet_daily.ThemeProposal(theme="privacy", capture_ids=[1, 2],
                                       rationale=""),
            tweet_daily.ThemeProposal(theme="craft", capture_ids=[3, 4],
                                       rationale=""),
            tweet_daily.ThemeProposal(theme="silence", capture_ids=[5, 6],
                                       rationale=""),
        ]
        chosen = await tweet_daily.pick_theme(props, conn=conn)
        # 'silence' is unused → least used → wins.
        assert chosen.theme == "silence"


@pytest.mark.asyncio
async def test_pick_theme_returns_none_for_empty():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        chosen = await tweet_daily.pick_theme([], conn=conn)
        assert chosen is None
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_tweet_daily_stitch.py -v`
Expected: FAIL — `detect_themes`, `pick_theme`, `ThemeProposal` not defined.

- [ ] **Step 3: Implement**

Append to `bot/tweet_daily.py`:

```python
import re
from dataclasses import dataclass
from typing import Any

from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_TWEET_STITCH


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
            theme=theme, capture_ids=ids_int,
            rationale=str(item.get("rationale") or ""),
        ))
    return out


async def pick_theme(
    proposals: list[ThemeProposal],
    *,
    conn: aiosqlite.Connection,
) -> ThemeProposal | None:
    """Pick the proposal whose theme has been used least often in the
    ledger. Ties: pick the first proposal in the list (LLM ordering)."""
    if not proposals:
        return None
    histogram: dict[str, int] = {}
    async with conn.execute(
        "SELECT theme, COUNT(*) FROM tweets WHERE theme IS NOT NULL GROUP BY theme"
    ) as cur:
        for row in await cur.fetchall():
            histogram[str(row[0])] = int(row[1])

    def usage(p: ThemeProposal) -> tuple[int, int]:
        # Lower count wins; preserve list order via index as secondary sort.
        return histogram.get(p.theme, 0), proposals.index(p)

    return sorted(proposals, key=usage)[0]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_tweet_daily_stitch.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_daily.py tests/test_tweet_daily_stitch.py
git commit -m "feat(tweet): add detect_themes + pick_theme (least-used wins)"
```

---

## Task B7: `generate_stitch`

**Files:**
- Modify: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_stitch.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tweet_daily_stitch.py`:

```python
@pytest.mark.asyncio
async def test_generate_stitch_returns_string(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            class R:
                text = json.dumps({"stitch": "you caught the asymmetry."})
            return R()
        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        s = await tweet_daily.generate_stitch(
            theme="privacy",
            capture_summaries=[
                ("2026-04-22", "crazy last of privacy for employees"),
                ("2026-04-21", "didn't even know someone kept this data"),
            ],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert s == "you caught the asymmetry."


@pytest.mark.asyncio
async def test_generate_stitch_returns_empty_on_llm_failure(monkeypatch):
    settings = fake_settings()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)

        async def fake_call(**kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        s = await tweet_daily.generate_stitch(
            theme="x", capture_summaries=[("2026-01-01", "a")],
            settings=settings, providers=FakeProviders(), conn=conn,
        )
        assert s == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_daily_stitch.py::test_generate_stitch_returns_string -v`
Expected: FAIL — `generate_stitch` not defined.

- [ ] **Step 3: Implement**

Append to `bot/tweet_daily.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_daily_stitch.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_daily.py tests/test_tweet_daily_stitch.py
git commit -m "feat(tweet): add generate_stitch — orchurator-voice JSON-coerced"
```

---

## Task B8: `assemble_tweet` — char budget + URL handling

**Files:**
- Modify: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_assemble.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tweet_daily_assemble.py
import pytest

from bot import tweet_daily


def _cap(*, id, raw, kind="text", local_date="2026-05-01", url=None):
    return {"id": id, "kind": kind, "raw": raw, "url": url,
            "local_date": local_date}


def test_assemble_no_url_basic():
    out = tweet_daily.assemble_tweet(
        stitch="you caught the asymmetry.",
        captures=[
            _cap(id=1, raw="crazy last of privacy", local_date="2026-04-22"),
            _cap(id=2, raw="someone kept this data", local_date="2026-04-21"),
        ],
    )
    assert out is not None
    assert "you caught the asymmetry." in out
    assert '"crazy last of privacy" (2026-04-22)' in out
    assert '"someone kept this data" (2026-04-21)' in out
    assert "https://" not in out


def test_assemble_with_url():
    out = tweet_daily.assemble_tweet(
        stitch="you keep the link.",
        captures=[
            _cap(id=1, raw="article body", kind="url",
                 local_date="2026-04-22", url="https://example.com/article"),
            _cap(id=2, raw="other thought", local_date="2026-04-21"),
        ],
    )
    assert out is not None
    assert out.endswith("https://example.com/article")


def test_assemble_picks_oldest_url_when_two_url_captures():
    out = tweet_daily.assemble_tweet(
        stitch="you keep the link.",
        captures=[
            _cap(id=1, raw="newer", kind="url",
                 local_date="2026-04-22", url="https://example.com/new"),
            _cap(id=2, raw="older", kind="url",
                 local_date="2026-04-20", url="https://example.com/old"),
        ],
    )
    assert out.endswith("https://example.com/old")


def test_assemble_truncates_quote_to_fit():
    long_body = "x " * 200  # 400 chars
    out = tweet_daily.assemble_tweet(
        stitch="you saw both.",
        captures=[
            _cap(id=1, raw=long_body, local_date="2026-04-22"),
            _cap(id=2, raw="short", local_date="2026-04-21"),
        ],
    )
    assert out is not None
    # Check that the rendered tweet is ≤ 280 graphemes.
    import grapheme
    assert grapheme.length(out) <= 280


def test_assemble_returns_none_when_quote_must_be_under_30():
    long_body = "x " * 1000
    out = tweet_daily.assemble_tweet(
        stitch="x" * 80,  # max stitch eats into budget
        captures=[
            _cap(id=1, raw=long_body, kind="url",
                 local_date="2026-04-22",
                 url="https://example.com/" + "p" * 60),
            _cap(id=2, raw=long_body, local_date="2026-04-21"),
        ],
    )
    assert out is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_daily_assemble.py -v`
Expected: FAIL — `assemble_tweet` not defined.

- [ ] **Step 3: Implement**

Append to `bot/tweet_daily.py`:

```python
import grapheme

_TCO_LEN = 23
_TWEET_MAX = 280
_MIN_QUOTE_LEN = 30


def _word_truncate(text: str, max_len: int) -> str:
    """Truncate `text` to ≤ max_len graphemes at a word boundary.
    Returns "" if max_len < 1."""
    if max_len < 1:
        return ""
    if grapheme.length(text) <= max_len:
        return text
    # Walk graphemes to the cap, then back off to the last space.
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

    # URL handling — pick the oldest URL-kind capture, if any.
    url_caps = [c for c in cap_pair if c.get("kind") == "url" and c.get("url")]
    url = None
    if url_caps:
        url_caps.sort(key=lambda c: c.get("local_date") or "")
        url = url_caps[0]["url"]

    # Per-line overhead for `— "<body>" (YYYY-MM-DD)\n` is 18 graphemes.
    overhead_per_line = 18
    overhead_total = len(stitch) + 2 + (overhead_per_line * 2)
    if url:
        overhead_total += 1 + _TCO_LEN  # leading \n + t.co length

    available = _TWEET_MAX - overhead_total
    if available < _MIN_QUOTE_LEN * 2:
        return None

    # Split available between the two quotes proportionally to their original
    # lengths, but never shrink a body below MIN.
    bodies = [(c.get("raw") or "").strip() for c in cap_pair]
    if not all(bodies):
        return None
    total_orig = sum(len(b) for b in bodies)
    if total_orig == 0:
        return None
    quotas = [
        max(_MIN_QUOTE_LEN, int(available * len(b) / total_orig))
        for b in bodies
    ]
    # Adjust if quotas overshoot available (rounding).
    overshoot = sum(quotas) - available
    if overshoot > 0:
        # Shrink the longer-quota line.
        idx = max(range(len(quotas)), key=lambda i: quotas[i])
        quotas[idx] -= overshoot
    if any(q < _MIN_QUOTE_LEN for q in quotas):
        return None

    truncated = [_word_truncate(b, q) for b, q in zip(bodies, quotas)]
    if any(grapheme.length(t) < _MIN_QUOTE_LEN for t in truncated):
        return None

    lines = [stitch.strip(), ""]
    for body, cap in zip(truncated, cap_pair):
        lines.append(f'— "{body}" ({cap["local_date"]})')
    if url:
        lines.append(url)
    out = "\n".join(lines)

    # Final safety: total grapheme length ≤ 280, with t.co accounting.
    measured = re.sub(r"https?://\S+", "x" * _TCO_LEN, out)
    if grapheme.length(measured) > _TWEET_MAX:
        return None
    return out
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_daily_assemble.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_daily.py tests/test_tweet_daily_assemble.py
git commit -m "feat(tweet): add assemble_tweet — char-budgeted, URL-aware composer"
```

---

## Task B9: Pending state helpers (`set_pending`, `get_pending`, `update_for_next`, `clear_pending`, `consume_for_post`)

**Files:**
- Modify: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_state.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tweet_daily_state.py
import json
import pytest
import aiosqlite

from bot import tweet_daily
from bot.db import init_schema


@pytest.mark.asyncio
async def test_set_and_get_pending():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        p = await tweet_daily.get_pending(conn)
        assert p is not None
        assert p.draft_text == "hi"
        assert p.capture_ids == [1, 2]
        assert p.draft_count == 1


@pytest.mark.asyncio
async def test_update_for_next_increments_draft_count():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="d1", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        new_count = await tweet_daily.update_for_next(
            conn,
            draft_text="d2", capture_ids=[3, 4],
            theme="u", stitch="s2", char_count=20,
        )
        assert new_count == 2
        p = await tweet_daily.get_pending(conn)
        assert p.draft_text == "d2"
        assert p.draft_count == 2
        assert p.local_date == "2026-05-03"  # preserved


@pytest.mark.asyncio
async def test_consume_for_post_returns_and_clears():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        consumed = await tweet_daily.consume_for_post(conn)
        assert consumed is not None
        assert consumed.draft_text == "hi"
        assert await tweet_daily.get_pending(conn) is None


@pytest.mark.asyncio
async def test_clear_pending_when_absent_is_noop():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # Should not raise
        await tweet_daily.clear_pending(conn)


@pytest.mark.asyncio
async def test_expire_drops_prior_day():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="old", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-02",  # yesterday
        )
        await tweet_daily.expire_if_stale(conn, today_local="2026-05-03")
        assert await tweet_daily.get_pending(conn) is None


@pytest.mark.asyncio
async def test_expire_keeps_today():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="today", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        await tweet_daily.expire_if_stale(conn, today_local="2026-05-03")
        p = await tweet_daily.get_pending(conn)
        assert p is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_daily_state.py -v`
Expected: FAIL — pending state helpers not defined.

- [ ] **Step 3: Implement**

Append to `bot/tweet_daily.py`:

```python
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    try:
        d = json.loads(row[0])
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
    except (json.JSONDecodeError, KeyError, ValueError):
        log.warning("corrupt pending_tweet_draft row, clearing")
        await clear_pending(conn)
        return None


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
    try:
        d = json.loads(row[0])
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
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_daily_state.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_daily.py tests/test_tweet_daily_state.py
git commit -m "feat(tweet): add pending-draft state machine (set/get/update/consume/expire)"
```

---

## Task B10: Ledger writers — SQLite + repo `tweeted.json`

**Files:**
- Modify: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_state.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tweet_daily_state.py`:

```python
@pytest.mark.asyncio
async def test_record_tweet_writes_sqlite_row():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.record_tweet(
            conn,
            tweet_id="1789",
            tweeted_at="2026-05-03T14:14:00Z",
            local_date="2026-05-03",
            capture_ids=[1, 2],
            theme="privacy",
            stitch="you saw it.",
            text="full tweet",
            draft_count=2,
            edited=False,
        )
        async with conn.execute("SELECT * FROM tweets") as cur:
            row = await cur.fetchone()
        assert row["tweet_id"] == "1789"
        assert json.loads(row["capture_ids"]) == [1, 2]
        assert row["edited"] == 0


@pytest.mark.asyncio
async def test_push_ledger_to_repo_appends(monkeypatch):
    from tests.helpers.fakes import fake_settings
    settings = fake_settings(GITHUB_TOKEN="t", GITHUB_REPO="x/y")

    captured: dict = {}
    async def fake_fetch(*, settings, path, client=None):
        return ("[]", "deadbeef")
    async def fake_put(*, settings, path, content, message, existing_sha=None,
                       client=None):
        captured["path"] = path
        captured["content"] = content
        captured["sha"] = existing_sha
        return "newsha"
    monkeypatch.setattr("bot.tweet_daily.fetch_file", fake_fetch)
    monkeypatch.setattr("bot.tweet_daily.put_file", fake_put)

    await tweet_daily.push_ledger_to_repo(
        settings=settings,
        record={"tweet_id": "1789", "tweeted_at": "2026-05-03T14:14:00Z",
                "local_date": "2026-05-03", "capture_ids": [1, 2],
                "theme": "privacy", "stitch": "x", "text": "y",
                "edited": False, "url": "https://x.com/i/web/status/1789"},
    )
    assert captured["path"] == "tweeted.json"
    assert json.loads(captured["content"])[0]["tweet_id"] == "1789"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_daily_state.py -v -k record_tweet`
Expected: FAIL — `record_tweet` and `push_ledger_to_repo` not defined.

- [ ] **Step 3: Implement**

Append to `bot/tweet_daily.py`:

```python
from bot.github_sync import fetch_file, put_file


_LEDGER_FILENAME = "tweeted.json"


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
) -> None:
    await conn.execute(
        """
        INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                            theme, stitch, text, draft_count, edited)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tweet_id, tweeted_at, local_date, json.dumps(capture_ids),
         theme, stitch, text, draft_count, 1 if edited else 0),
    )
    await conn.commit()


async def push_ledger_to_repo(*, settings: Settings, record: dict) -> None:
    """Append `record` to `tweeted.json` at the captures repo root.
    Failure is logged but not raised — SQLite ledger is canonical."""
    try:
        fetched = await fetch_file(settings=settings, path=_LEDGER_FILENAME)
    except Exception:
        log.exception("push_ledger_to_repo: fetch failed; cannot append")
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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_daily_state.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_daily.py tests/test_tweet_daily_state.py
git commit -m "feat(tweet): add ledger writers (sqlite row + repo tweeted.json)"
```

---

## Task B11: `daily_tweet_draft_job` — orchestrator

**Files:**
- Modify: `bot/tweet_daily.py`
- Test: `tests/test_tweet_daily_state.py` (extend, integration-style)

- [ ] **Step 1: Write the failing integration test**

Add to `tests/test_tweet_daily_state.py`:

```python
@pytest.mark.asyncio
async def test_daily_tweet_draft_job_full_flow(monkeypatch):
    from tests.helpers.fakes import FakeProviders, fake_settings

    settings = fake_settings(TWEET_DAILY_V2_ENABLED=True,
                             TELEGRAM_OWNER_ID=123)

    sent: dict = {}
    class FakeBot:
        async def send_message(self, *, chat_id, text):
            sent["chat_id"] = chat_id
            sent["text"] = text

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # Two opted-in captures
        for i, raw in enumerate(
            ["crazy last of privacy", "someone kept the data"], start=1
        ):
            await conn.execute(
                """
                INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                      iso_week_key, fz_week_idx, status)
                VALUES ('text', ?, ?, ?, ?, ?, ?, 'done')
                """,
                (raw, json.dumps({"tweetable": True}),
                 "2026-05-01T12:00:00Z", "2026-05-01", "2026-W18", 1900),
            )
        await conn.commit()

        async def fake_call(*, purpose, **kwargs):
            class R: pass
            r = R()
            if purpose == "ingest":
                r.text = json.dumps([
                    {"theme": "privacy", "capture_ids": [1, 2],
                     "rationale": "both"}
                ])
            else:  # tweet
                r.text = json.dumps({"stitch": "you caught it."})
            return r
        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        ok = await tweet_daily.daily_tweet_draft_job(
            conn=conn, settings=settings,
            providers=FakeProviders(), bot=FakeBot(),
            today_iso="2026-05-03",
        )
        assert ok is True
        assert sent["chat_id"] == 123
        assert "you caught it." in sent["text"]
        # Pending state was set
        p = await tweet_daily.get_pending(conn)
        assert p is not None
        assert p.theme == "privacy"


@pytest.mark.asyncio
async def test_daily_tweet_draft_job_disabled_when_flag_false():
    from tests.helpers.fakes import FakeProviders, fake_settings

    settings = fake_settings(TWEET_DAILY_V2_ENABLED=False)
    class FakeBot:
        async def send_message(self, **kwargs):
            raise AssertionError("should not be called")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        ok = await tweet_daily.daily_tweet_draft_job(
            conn=conn, settings=settings,
            providers=FakeProviders(), bot=FakeBot(),
            today_iso="2026-05-03",
        )
        assert ok is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_daily_state.py -v -k daily_tweet_draft`
Expected: FAIL — `daily_tweet_draft_job` not defined.

- [ ] **Step 3: Implement**

Append to `bot/tweet_daily.py`:

```python
def _format_pool_for_themes(pool: list[aiosqlite.Row]) -> str:
    lines = []
    for r in pool[:30]:  # cap to keep prompt size sane
        title = ""
        try:
            p = json.loads(r["processed"]) if r["processed"] else None
            if isinstance(p, dict):
                title = (p.get("title") or "").strip()
        except (TypeError, json.JSONDecodeError):
            pass
        body = (r["raw"] or "")[:240].replace("\n", " ").strip()
        prefix = f"[{r['id']}] ({r['kind']}) "
        if title:
            lines.append(prefix + f"{title}: {body}")
        else:
            lines.append(prefix + body)
    return "\n".join(lines)


def _render_draft_dm(
    *, draft_text: str, theme: str, char_count: int,
    draft_count: int, cap: int,
) -> str:
    return (
        f"draft {draft_count}/{cap}\n\n"
        f"{draft_text}\n\n"
        f"{char_count}/280 chars · theme: {theme}\n\n"
        f"/post   /next   /edit <text>   /skip"
    )


async def daily_tweet_draft_job(
    *,
    conn: aiosqlite.Connection,
    settings: Settings,
    providers: Providers,
    bot,
    today_iso: str | None = None,
) -> bool:
    """Cron-driven entry. Returns True iff a draft was generated and DMed."""
    if not settings.TWEET_DAILY_V2_ENABLED:
        return False
    if settings.TELEGRAM_OWNER_ID == 0 or bot is None:
        return False

    today_iso = today_iso or date.today().isoformat()

    # Drop any leftover from a prior day before generating a new one.
    await expire_if_stale(conn, today_local=today_iso)
    if await get_pending(conn) is not None:
        log.info("daily_tweet_draft_job: pending draft already present, skipping")
        return False

    pool = await pick_eligible_pool(conn, settings=settings, today_iso=today_iso)
    if len(pool) < 2:
        log.info("daily_tweet_draft_job: pool < 2, no draft")
        return False

    proposals = await detect_themes(
        pool_summary=_format_pool_for_themes(pool),
        settings=settings, providers=providers, conn=conn,
    )
    if not proposals:
        log.info("daily_tweet_draft_job: no theme proposals")
        return False

    # `pick_theme` returns the least-used theme; iterate the rest as
    # fallbacks when assembly fails on the chosen pair.
    chosen = await pick_theme(proposals, conn=conn)
    candidates = [chosen] + [p for p in proposals if p is not chosen]

    pool_by_id = {r["id"]: r for r in pool}
    for proposal in candidates:
        captures = [
            pool_by_id[i] for i in proposal.capture_ids if i in pool_by_id
        ][:2]
        if len(captures) < 2:
            continue
        draft = await _try_build_draft(
            captures=captures, theme=proposal.theme,
            settings=settings, providers=providers, conn=conn,
        )
        if draft is None:
            continue
        await set_pending(
            conn,
            draft_text=draft["text"],
            capture_ids=[c["id"] for c in captures],
            theme=proposal.theme,
            stitch=draft["stitch"],
            char_count=draft["char_count"],
            local_date=today_iso,
        )
        try:
            await bot.send_message(
                chat_id=settings.TELEGRAM_OWNER_ID,
                text=_render_draft_dm(
                    draft_text=draft["text"], theme=proposal.theme,
                    char_count=draft["char_count"], draft_count=1,
                    cap=settings.TWEET_NEXT_CAP,
                ),
            )
        except Exception:
            log.exception("daily_tweet_draft_job: bot.send_message failed")
            await clear_pending(conn)
            return False
        return True

    log.info("daily_tweet_draft_job: no proposal produced a valid draft")
    return False


async def _try_build_draft(
    *,
    captures: list[aiosqlite.Row],
    theme: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> dict | None:
    """Up to 3 stitch attempts for this capture+theme combination.
    Returns dict {text, stitch, char_count} or None on total failure."""
    from bot.digest.validate import validate_quote_only
    from bot.tweet_validate import validate_stitch, validate_tweet_total_length

    summaries = [(c["local_date"], (c["raw"] or "").strip()) for c in captures]
    cap_dicts = [dict(c) for c in captures]
    bodies = [s[1] for s in summaries]

    for _ in range(3):
        stitch = await generate_stitch(
            theme=theme, capture_summaries=summaries,
            settings=settings, providers=providers, conn=conn,
        )
        if not stitch:
            continue
        ok, reason = validate_stitch(stitch)
        if not ok:
            log.info("stitch invalid: %s", reason)
            continue
        text = assemble_tweet(stitch=stitch, captures=cap_dicts)
        if text is None:
            continue
        ok2, reason2 = validate_tweet_total_length(text)
        if not ok2:
            log.info("tweet length invalid: %s", reason2)
            continue
        # Quote-only validator on the assembled tweet against the picked bodies.
        ok3, offenders = validate_quote_only(text, bodies)
        if not ok3:
            log.info("quote validator failed: %s", offenders)
            continue
        # Measure char_count consistently with X (t.co URLs as 23).
        import grapheme
        measured = re.sub(r"https?://\S+", "x" * _TCO_LEN, text)
        return {"text": text, "stitch": stitch, "char_count": grapheme.length(measured)}
    return None


```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_daily_state.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/tweet_daily.py tests/test_tweet_daily_state.py
git commit -m "feat(tweet): add daily_tweet_draft_job orchestrator with retry/abandon logic"
```

---

## Task B12: `/post` handler

**Files:**
- Modify: `bot/handlers.py`
- Test: `tests/test_tweet_handlers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tweet_handlers.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from bot import handlers, tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


def _make_update_with_text(text: str, owner_id: int = 1):
    update = MagicMock()
    update.effective_user.id = owner_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(*, conn, settings, providers=None):
    ctx = MagicMock()
    ctx.application.bot_data = {
        "conn": conn, "settings": settings,
        "providers": providers or MagicMock(),
    }
    return ctx


@pytest.mark.asyncio
async def test_post_handler_posts_and_writes_ledger(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1, X_CONSUMER_KEY="a",
                             X_CONSUMER_SECRET="b", X_ACCESS_TOKEN="c",
                             X_ACCESS_TOKEN_SECRET="d")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="hi", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def fake_post_tweet(text, *, settings):
            from bot.tweet import TweetResult
            return TweetResult(id="1789", url="https://x.com/i/web/status/1789")
        monkeypatch.setattr("bot.handlers.tweet_mod.post_tweet", fake_post_tweet)

        push_calls = {}
        async def fake_push(**kwargs):
            push_calls["called"] = True
        monkeypatch.setattr("bot.tweet_daily.push_ledger_to_repo", fake_push)

        update = _make_update_with_text("/post")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.post_handler(update, ctx)

        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        assert await tweet_daily.get_pending(conn) is None
        update.message.reply_text.assert_awaited()
        assert "1789" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_post_handler_no_pending_drafts_replies_idle():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _make_update_with_text("/post")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.post_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "no draft" in update.message.reply_text.call_args.args[0].lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k post_handler`
Expected: FAIL — `post_handler` not defined.

- [ ] **Step 3: Implement `post_handler`**

Open `bot/handlers.py`. Find the existing imports near the top — add:

```python
from bot import tweet as tweet_mod, tweet_daily
```

(if `tweet_mod` import is already present in the file, just append `tweet_daily`.)

Add at the bottom of `bot/handlers.py`:

```python
async def post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    conn: aiosqlite.Connection = context.application.bot_data["conn"]

    consumed = await tweet_daily.consume_for_post(conn)
    if consumed is None:
        await update.message.reply_text("no draft pending.")
        return

    result = await tweet_mod.post_tweet(consumed.draft_text, settings=settings)
    if result is None:
        await update.message.reply_text(
            "post failed (OAuth or X error). draft has been cleared — re-fire with /next tomorrow."
        )
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await tweet_daily.record_tweet(
        conn,
        tweet_id=result.id,
        tweeted_at=now,
        local_date=consumed.local_date,
        capture_ids=consumed.capture_ids,
        theme=consumed.theme or None,
        stitch=consumed.stitch or None,
        text=consumed.draft_text,
        draft_count=consumed.draft_count,
        edited=False,
    )
    try:
        await tweet_daily.push_ledger_to_repo(
            settings=settings,
            record={
                "tweet_id": result.id, "url": result.url,
                "tweeted_at": now, "local_date": consumed.local_date,
                "capture_ids": consumed.capture_ids,
                "theme": consumed.theme, "stitch": consumed.stitch,
                "text": consumed.draft_text, "edited": False,
            },
        )
    except Exception:
        log.exception("ledger push failed")

    await update.message.reply_text(f"posted: {result.url}")
```

Make sure `aiosqlite`, `datetime`, `timezone`, `Settings`, `log` are already imported in handlers.py (they should be, from existing handlers).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k post_handler`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/handlers.py tests/test_tweet_handlers.py
git commit -m "feat(tweet): add /post handler — posts draft, ledgers row, pushes repo file"
```

---

## Task B13: `/next` handler

**Files:**
- Modify: `bot/handlers.py`
- Test: `tests/test_tweet_handlers.py` (extend)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tweet_handlers.py`:

```python
@pytest.mark.asyncio
async def test_next_handler_increments_and_dms_new_draft(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1, TWEET_NEXT_CAP=5)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn,
            draft_text="d1", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        # Add fresh captures so the new pool is non-empty
        for raw in ["new a", "new b"]:
            await conn.execute(
                """
                INSERT INTO captures (kind, raw, payload, created_at, local_date,
                                      iso_week_key, fz_week_idx, status)
                VALUES ('text', ?, ?, ?, ?, ?, ?, 'done')
                """,
                (raw, json.dumps({"tweetable": True}),
                 "2026-05-01T12:00:00Z", "2026-05-01", "2026-W18", 1900),
            )
        await conn.commit()

        async def fake_call(*, purpose, **kwargs):
            class R: pass
            r = R()
            r.text = (json.dumps([{"theme": "u", "capture_ids": [3, 4],
                                    "rationale": ""}])
                      if purpose == "ingest"
                      else json.dumps({"stitch": "you noticed twice."}))
            return r
        monkeypatch.setattr("bot.tweet_daily.call_llm", fake_call)

        update = _make_update_with_text("/next")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.next_handler(update, ctx)

        p = await tweet_daily.get_pending(conn)
        assert p.draft_count == 2
        update.message.reply_text.assert_awaited()
        assert "draft 2/5" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_next_handler_blocks_at_cap():
    settings = fake_settings(TELEGRAM_OWNER_ID=1, TWEET_NEXT_CAP=5)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        # Set draft_count to 5 already
        await tweet_daily.set_pending(
            conn, draft_text="d", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        await conn.execute(
            "UPDATE kv SET value = json_set(value, '$.draft_count', 5)"
            " WHERE key = ?", (tweet_daily._KV_KEY,),
        )
        await conn.commit()

        update = _make_update_with_text("/next")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.next_handler(update, ctx)
        update.message.reply_text.assert_awaited()
        assert "exhausted" in update.message.reply_text.call_args.args[0].lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k next_handler`
Expected: FAIL — `next_handler` not defined.

- [ ] **Step 3: Implement `next_handler`**

Append to `bot/handlers.py`:

```python
async def next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    conn: aiosqlite.Connection = context.application.bot_data["conn"]
    providers = context.application.bot_data["providers"]

    pending = await tweet_daily.get_pending(conn)
    if pending is None:
        await update.message.reply_text("no draft pending.")
        return
    if pending.draft_count >= settings.TWEET_NEXT_CAP:
        await update.message.reply_text(
            f"pool exhausted ({pending.draft_count}/{settings.TWEET_NEXT_CAP}) — "
            "/post current, /skip, or /edit <text>."
        )
        return

    today_iso = pending.local_date
    pool = await tweet_daily.pick_eligible_pool(
        conn, settings=settings, today_iso=today_iso,
    )
    if len(pool) < 2:
        await update.message.reply_text(
            "couldn't generate — pool too small. /post current, /skip, or /edit."
        )
        return

    proposals = await tweet_daily.detect_themes(
        pool_summary=tweet_daily._format_pool_for_themes(pool),
        settings=settings, providers=providers, conn=conn,
    )
    if not proposals:
        await update.message.reply_text(
            "couldn't generate a draft — try /next again or /skip."
        )
        return

    # Avoid re-picking the same captures.
    used = set(pending.capture_ids)
    proposals = [
        p for p in proposals
        if not (set(p.capture_ids) & used)
    ] or proposals  # if nothing else, fall back

    chosen = await tweet_daily.pick_theme(proposals, conn=conn)
    candidates = [chosen] + [p for p in proposals if p is not chosen]
    pool_by_id = {r["id"]: r for r in pool}

    for proposal in candidates:
        captures = [pool_by_id[i] for i in proposal.capture_ids
                    if i in pool_by_id][:2]
        if len(captures) < 2:
            continue
        draft = await tweet_daily._try_build_draft(
            captures=captures, theme=proposal.theme,
            settings=settings, providers=providers, conn=conn,
        )
        if draft is None:
            continue
        new_count = await tweet_daily.update_for_next(
            conn,
            draft_text=draft["text"],
            capture_ids=[c["id"] for c in captures],
            theme=proposal.theme,
            stitch=draft["stitch"],
            char_count=draft["char_count"],
        )
        await update.message.reply_text(
            tweet_daily._render_draft_dm(
                draft_text=draft["text"], theme=proposal.theme,
                char_count=draft["char_count"],
                draft_count=new_count or pending.draft_count + 1,
                cap=settings.TWEET_NEXT_CAP,
            )
        )
        return

    await update.message.reply_text(
        "couldn't generate a draft — try /next again or /skip."
    )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k next_handler`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/handlers.py tests/test_tweet_handlers.py
git commit -m "feat(tweet): add /next handler — different pair, cap-aware, atomic update"
```

---

## Task B14: `/edit <text>` handler

**Files:**
- Modify: `bot/handlers.py`
- Test: `tests/test_tweet_handlers.py` (extend)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tweet_handlers.py`:

```python
@pytest.mark.asyncio
async def test_edit_handler_posts_user_text_and_marks_edited(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="orig", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )

        async def fake_post_tweet(text, *, settings):
            from bot.tweet import TweetResult
            return TweetResult(id="2", url="https://x.com/i/web/status/2")
        monkeypatch.setattr("bot.handlers.tweet_mod.post_tweet", fake_post_tweet)
        async def no_push(**_):
            pass
        monkeypatch.setattr("bot.tweet_daily.push_ledger_to_repo", no_push)

        update = _make_update_with_text("/edit my own version")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)

        async with conn.execute("SELECT text, edited FROM tweets") as cur:
            row = await cur.fetchone()
        assert row["text"] == "my own version"
        assert row["edited"] == 1


@pytest.mark.asyncio
async def test_edit_handler_rejects_over_280():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="orig", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        long_text = "/edit " + "x" * 281
        update = _make_update_with_text(long_text)
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)
        # Pending preserved, no tweet row.
        assert await tweet_daily.get_pending(conn) is not None
        async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
            assert (await cur.fetchone())[0] == 0
        msg = update.message.reply_text.call_args.args[0]
        assert "too long" in msg


@pytest.mark.asyncio
async def test_edit_handler_no_pending_replies_idle():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        update = _make_update_with_text("/edit hello")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.edit_handler(update, ctx)
        msg = update.message.reply_text.call_args.args[0]
        assert "no draft" in msg.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k edit_handler`
Expected: FAIL — `edit_handler` not defined.

- [ ] **Step 3: Implement**

Append to `bot/handlers.py`:

```python
async def edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    conn: aiosqlite.Connection = context.application.bot_data["conn"]

    raw = (update.message.text or "").strip()
    parts = raw.split(maxsplit=1)
    user_text = parts[1].strip() if len(parts) == 2 else ""
    if not user_text:
        await update.message.reply_text(
            "usage: /edit <your tweet text>"
        )
        return

    pending = await tweet_daily.get_pending(conn)
    if pending is None:
        await update.message.reply_text("no draft pending.")
        return

    from bot.tweet_validate import validate_tweet_total_length
    ok, reason = validate_tweet_total_length(user_text)
    if not ok:
        # Preserve pending so user can re-edit.
        await update.message.reply_text(reason or "too long")
        return

    consumed = await tweet_daily.consume_for_post(conn)
    if consumed is None:
        await update.message.reply_text("no draft pending.")
        return

    result = await tweet_mod.post_tweet(user_text, settings=settings)
    if result is None:
        await update.message.reply_text("post failed.")
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await tweet_daily.record_tweet(
        conn,
        tweet_id=result.id, tweeted_at=now,
        local_date=consumed.local_date,
        capture_ids=consumed.capture_ids,
        theme=consumed.theme or None,
        stitch=consumed.stitch or None,
        text=user_text,
        draft_count=consumed.draft_count,
        edited=True,
    )
    try:
        await tweet_daily.push_ledger_to_repo(
            settings=settings,
            record={
                "tweet_id": result.id, "url": result.url,
                "tweeted_at": now, "local_date": consumed.local_date,
                "capture_ids": consumed.capture_ids,
                "theme": consumed.theme, "stitch": consumed.stitch,
                "text": user_text, "edited": True,
            },
        )
    except Exception:
        log.exception("ledger push failed")
    await update.message.reply_text(f"posted: {result.url}")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k edit_handler`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/handlers.py tests/test_tweet_handlers.py
git commit -m "feat(tweet): add /edit handler — user-verbatim post, 280-cap only"
```

---

## Task B15: Extend `/skip` to clear pending tweet draft

**Files:**
- Modify: `bot/handlers.py`
- Test: `tests/test_tweet_handlers.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tweet_handlers.py`:

```python
@pytest.mark.asyncio
async def test_skip_clears_pending_tweet_draft():
    settings = fake_settings(TELEGRAM_OWNER_ID=1)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="d", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        update = _make_update_with_text("/skip")
        ctx = _make_context(conn=conn, settings=settings)
        await handlers.skip_handler(update, ctx)
        assert await tweet_daily.get_pending(conn) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweet_handlers.py -v -k skip_clears_pending_tweet`
Expected: FAIL — current `skip_handler` does not clear tweet drafts.

- [ ] **Step 3: Read existing `skip_handler`**

```bash
sed -n '419,432p' bot/handlers.py
```

Note its current logic (clears pending why and pending reflection).

- [ ] **Step 4: Add tweet-draft clear to `skip_handler`**

In `bot/handlers.py`, find `skip_handler` and add a tweet-draft clear in the same flow. The exact insertion depends on the current code, but the addition is one line:

```python
    # Inside skip_handler, alongside the existing why/reflection clears:
    cleared_tweet = await tweet_daily.get_pending(conn) is not None
    await tweet_daily.clear_pending(conn)
    # Then include `cleared_tweet` in whatever response the handler builds.
```

Do not break existing skip behavior — append the tweet check, do not replace.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_tweet_handlers.py tests/ -v -k skip`
Expected: new test passes; pre-existing skip tests still pass.

- [ ] **Step 6: Commit**

```bash
git add bot/handlers.py tests/test_tweet_handlers.py
git commit -m "feat(tweet): extend /skip to clear pending tweet draft"
```

---

## Task B16: `/tweetable` and `/untweetable` handlers + immediate re-sync

**Files:**
- Modify: `bot/handlers.py`
- Test: `tests/test_tweetable_handlers.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tweetable_handlers.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from bot import handlers
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


def _update(text, owner_id=1):
    u = MagicMock()
    u.effective_user.id = owner_id
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def _ctx(*, conn, settings):
    c = MagicMock()
    c.application.bot_data = {"conn": conn, "settings": settings,
                              "providers": MagicMock()}
    return c


async def _add_capture(conn, *, payload=None):
    await conn.execute(
        """
        INSERT INTO captures (kind, raw, payload, created_at, local_date,
                              iso_week_key, fz_week_idx, status, github_sha)
        VALUES ('text', 'body', ?, '2026-05-01T12:00:00Z', '2026-05-01',
                '2026-W18', 1900, 'done', 'abc123')
        """,
        (json.dumps(payload or {}),),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_tweetable_last_sets_flag(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1, GITHUB_TOKEN="t",
                             GITHUB_REPO="x/y")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn)

        async def fake_push(capture_id, *, settings, conn, client=None):
            return True
        monkeypatch.setattr("bot.handlers.github_sync.push_capture", fake_push)

        await handlers.tweetable_handler(_update("/tweetable last"),
                                          _ctx(conn=conn, settings=settings))
        async with conn.execute("SELECT payload FROM captures") as cur:
            row = await cur.fetchone()
        assert json.loads(row[0]).get("tweetable") is True


@pytest.mark.asyncio
async def test_untweetable_clears_flag(monkeypatch):
    settings = fake_settings(TELEGRAM_OWNER_ID=1, GITHUB_TOKEN="t",
                             GITHUB_REPO="x/y")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await _add_capture(conn, payload={"tweetable": True})

        async def fake_push(capture_id, *, settings, conn, client=None):
            return True
        monkeypatch.setattr("bot.handlers.github_sync.push_capture", fake_push)

        await handlers.untweetable_handler(_update("/untweetable last"),
                                            _ctx(conn=conn, settings=settings))
        async with conn.execute("SELECT payload FROM captures") as cur:
            row = await cur.fetchone()
        assert json.loads(row[0]).get("tweetable") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tweetable_handlers.py -v`
Expected: FAIL — handlers not defined.

- [ ] **Step 3: Implement**

Append to `bot/handlers.py`:

```python
async def _set_tweetable(
    conn: aiosqlite.Connection, *, capture_id: int, value: bool,
) -> bool:
    """Update payload.tweetable on a capture. Returns True if the row
    existed."""
    async with conn.execute(
        "SELECT payload FROM captures WHERE id = ?", (capture_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row[0]) if row[0] else {}
    except json.JSONDecodeError:
        payload = {}
    payload["tweetable"] = bool(value)
    await conn.execute(
        "UPDATE captures SET payload = ? WHERE id = ?",
        (json.dumps(payload), capture_id),
    )
    # Reset github_sha so next push regenerates frontmatter with the new flag.
    await conn.execute(
        "UPDATE captures SET github_sha = NULL WHERE id = ?", (capture_id,),
    )
    await conn.commit()
    return True


async def _resolve_capture_id(
    conn: aiosqlite.Connection, arg: str,
) -> int | None:
    if arg == "last":
        async with conn.execute(
            "SELECT id FROM captures WHERE kind NOT IN ('why','highlight')"
            " ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None
    try:
        return int(arg)
    except ValueError:
        return None


async def _do_tweetable_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, value: bool,
):
    if not await _ensure_owner(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    conn: aiosqlite.Connection = context.application.bot_data["conn"]

    raw = (update.message.text or "").strip()
    parts = raw.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) == 2 else ""
    if not arg:
        await update.message.reply_text(
            "usage: /tweetable last  OR  /tweetable <id>"
            if value else
            "usage: /untweetable last  OR  /untweetable <id>"
        )
        return

    capture_id = await _resolve_capture_id(conn, arg)
    if capture_id is None:
        await update.message.reply_text(f"no such capture: {arg}")
        return
    ok = await _set_tweetable(conn, capture_id=capture_id, value=value)
    if not ok:
        await update.message.reply_text(f"capture {capture_id} not found.")
        return

    # Re-sync the affected capture's md file so the repo frontmatter
    # mirrors SQLite immediately.
    try:
        await github_sync.push_capture(capture_id, settings=settings, conn=conn)
    except Exception:
        log.exception("tweetable: re-sync failed for capture %s", capture_id)

    flag = "tweetable" if value else "not tweetable"
    await update.message.reply_text(f"capture {capture_id}: {flag}.")


async def tweetable_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_tweetable_command(update, context, value=True)


async def untweetable_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_tweetable_command(update, context, value=False)
```

Make sure `from bot import github_sync` is already imported (it is — used elsewhere in handlers.py).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tweetable_handlers.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/handlers.py tests/test_tweetable_handlers.py
git commit -m "feat(tweet): add /tweetable + /untweetable with immediate md re-sync"
```

---

## Task B17: Render `tweetable` flag in markdown frontmatter

**Files:**
- Modify: `bot/markdown_out.py`
- Test: `tests/test_markdown_out.py` (likely exists; extend)

- [ ] **Step 1: Find the existing test file**

```bash
ls tests/ | grep markdown
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_markdown_out.py` (or create one mirroring existing patterns):

```python
import json
from bot.markdown_out import render_capture_markdown


class FakeRow(dict):
    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            return None


def _row(**overrides):
    base = dict(
        id=1, kind="text", source=None, url=None, raw="hello",
        payload="{}", processed="{}",
        parent_id=None, telegram_msg_id=None,
        created_at="2026-05-01T12:00:00Z", local_date="2026-05-01",
        iso_week_key="2026-W18", fz_week_idx=1900,
        status="done", error=None, github_sha=None,
        asset_bytes=None, asset_mime=None,
    )
    base.update(overrides)
    return FakeRow(base)


def test_tweetable_flag_rendered_in_frontmatter():
    row = _row(payload=json.dumps({"tweetable": True}))
    md = render_capture_markdown(row, why_children=[], highlight_children=[])
    assert "tweetable = true" in md


def test_tweetable_false_rendered():
    row = _row(payload=json.dumps({"tweetable": False}))
    md = render_capture_markdown(row, why_children=[], highlight_children=[])
    assert "tweetable = false" in md


def test_tweetable_omitted_when_unset():
    row = _row(payload=json.dumps({}))
    md = render_capture_markdown(row, why_children=[], highlight_children=[])
    assert "tweetable" not in md
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_markdown_out.py -v -k tweetable`
Expected: FAIL — frontmatter does not include the flag.

- [ ] **Step 4: Add to `render_capture_markdown`**

Open `bot/markdown_out.py`. In `render_capture_markdown`, after the
existing `payload = _parse_json(...)` block (around line 166), add:

```python
    if isinstance(payload.get("tweetable"), bool):
        fm["tweetable"] = bool(payload["tweetable"])
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_markdown_out.py -v`
Expected: all pass (existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add bot/markdown_out.py tests/test_markdown_out.py
git commit -m "feat(md): render tweetable flag in capture frontmatter when set"
```

---

## Task B18: `/status` extension — show tweet pipeline state

**Files:**
- Modify: `bot/handlers.py`
- Test: `tests/test_handlers_status.py` (extend or create)

- [ ] **Step 1: Read existing `status_handler`**

```bash
sed -n '62,116p' bot/handlers.py
```

- [ ] **Step 2: Write the failing test**

```python
# Append to whatever the existing status test file is, or create:
# tests/test_handlers_status_tweet.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
import aiosqlite

from bot import handlers, tweet_daily
from bot.db import init_schema
from tests.helpers.fakes import fake_settings


@pytest.mark.asyncio
async def test_status_includes_tweet_pipeline_section():
    settings = fake_settings(TELEGRAM_OWNER_ID=1, TWEET_DAILY_V2_ENABLED=True,
                             TWEET_NEXT_CAP=5)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_schema(conn)
        await tweet_daily.set_pending(
            conn, draft_text="d", capture_ids=[1, 2],
            theme="t", stitch="s", char_count=10,
            local_date="2026-05-03",
        )
        await conn.execute(
            """
            INSERT INTO tweets (tweet_id, tweeted_at, local_date, capture_ids,
                                theme, text, draft_count)
            VALUES ('t1', '2026-05-01T01:00:00Z', '2026-05-01', '[1]',
                    'p', 'x', 1)
            """
        )
        await conn.commit()

        update = MagicMock()
        update.effective_user.id = 1
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.application.bot_data = {"conn": conn, "settings": settings,
                                     "providers": MagicMock()}

        await handlers.status_handler(update, ctx)
        msg = update.message.reply_text.call_args.args[0]
        assert "tweet pipeline" in msg.lower()
        assert "draft 1/5" in msg
        assert "ledger: 1" in msg
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_handlers_status_tweet.py -v`
Expected: FAIL — status output doesn't include tweet pipeline section.

- [ ] **Step 4: Modify `status_handler`**

In `bot/handlers.py`, inside `status_handler`, BEFORE the final reply, gather tweet pipeline state and append to the message:

```python
    # Tweet pipeline section
    pending = await tweet_daily.get_pending(conn)
    async with conn.execute("SELECT COUNT(*) FROM tweets") as cur:
        row = await cur.fetchone()
    ledger_count = int(row[0]) if row else 0

    tweet_lines = ["", "tweet pipeline:"]
    tweet_lines.append(
        f"  enabled: {'yes' if settings.TWEET_DAILY_V2_ENABLED else 'no'}"
    )
    if pending is None:
        tweet_lines.append("  pending: none")
    else:
        tweet_lines.append(
            f"  pending: draft {pending.draft_count}/{settings.TWEET_NEXT_CAP}"
            f" · theme={pending.theme}"
        )
    tweet_lines.append(f"  ledger: {ledger_count}")

    # Append to whatever existing status_text variable the handler builds.
    status_text = status_text + "\n" + "\n".join(tweet_lines)
```

Adjust variable names to match the actual handler code.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_handlers_status_tweet.py tests/ -v -k status`
Expected: new test passes; existing status tests still pass.

- [ ] **Step 6: Commit**

```bash
git add bot/handlers.py tests/test_handlers_status_tweet.py
git commit -m "feat(status): include tweet pipeline state (enabled, pending, ledger)"
```

---

## Task B19: Scheduler — register `daily_tweet_draft_job` + `tweet_draft_expiry`

**Files:**
- Modify: `bot/scheduler.py`

- [ ] **Step 1: Add imports**

In `bot/scheduler.py`, add:

```python
from bot import github_sync, process, reflection, sparks, tweet_daily
```

- [ ] **Step 2: Add the two jobs inside `build_scheduler`**

In `build_scheduler`, after the spark job registration and inside the `if bot is not None:` block (because both new jobs need bot), add:

```python
        # Tweet draft expiry (every 60s; cheap kv check)
        async def _tweet_expire_wrapper():
            today_local = local_date_for(
                datetime.now(timezone.utc), settings.TIMEZONE,
            ).isoformat()
            try:
                await tweet_daily.expire_if_stale(conn, today_local=today_local)
            except Exception:
                log.exception("tweet_draft_expiry failed")

        scheduler.add_job(
            _tweet_expire_wrapper,
            trigger=IntervalTrigger(seconds=60),
            id="tweet_draft_expiry",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        # Daily tweet draft (cron, owner-DM-driven)
        if settings.TWEET_DAILY_V2_ENABLED:
            th, tm = _parse_hhmm(settings.TWEET_DRAFT_LOCAL_TIME)

            async def _tweet_draft_wrapper():
                today_iso = local_date_for(
                    datetime.now(timezone.utc), settings.TIMEZONE,
                ).isoformat()
                try:
                    await tweet_daily.daily_tweet_draft_job(
                        conn=conn, settings=settings,
                        providers=providers, bot=bot,
                        today_iso=today_iso,
                    )
                except Exception:
                    log.exception("daily_tweet_draft_job failed")

            scheduler.add_job(
                _tweet_draft_wrapper,
                trigger=CronTrigger(hour=th, minute=tm, timezone=settings.TIMEZONE),
                id="daily_tweet_draft",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
```

- [ ] **Step 3: Sanity-check the file imports compile**

```bash
python -c "from bot.scheduler import build_scheduler; print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: all tests still pass.

- [ ] **Step 5: Commit**

```bash
git add bot/scheduler.py
git commit -m "feat(scheduler): register daily_tweet_draft_job + tweet_draft_expiry"
```

---

## Task B20: `bot_app.py` — register handlers + boot OAuth gate

**Files:**
- Modify: `bot/bot_app.py`

- [ ] **Step 1: Register new command handlers**

Open `bot/bot_app.py`. After the existing `app.add_handler(CommandHandler(...))` block (around line 94), add:

```python
    app.add_handler(CommandHandler("post", handlers.post_handler))
    app.add_handler(CommandHandler("next", handlers.next_handler))
    app.add_handler(CommandHandler("edit", handlers.edit_handler))
    app.add_handler(CommandHandler("tweetable", handlers.tweetable_handler))
    app.add_handler(CommandHandler("untweetable", handlers.untweetable_handler))
```

- [ ] **Step 2: Add boot-time OAuth gate**

Find where `Settings` is loaded / validated in `bot_app.py`. Add a check that disables `TWEET_DAILY_V2_ENABLED` when X OAuth is not configured:

```python
from bot import tweet as tweet_mod

# After settings load, before scheduler build:
if settings.TWEET_DAILY_V2_ENABLED and not tweet_mod._oauth_configured(settings):
    log.warning(
        "tweet_v2: enabled but X OAuth not configured — disabling auto-fire"
    )
    settings.TWEET_DAILY_V2_ENABLED = False
```

If `_oauth_configured` is intended as private, add a public helper to `bot/tweet.py`:

```python
def is_oauth_configured(settings: Settings) -> bool:
    return _oauth_configured(settings)
```

and call `tweet_mod.is_oauth_configured(settings)` in `bot_app.py`.

- [ ] **Step 3: Sanity-check**

```bash
python -c "from bot.bot_app import build_app; print('ok')"
```
Expected: `ok` (or whatever the existing import surface is).

- [ ] **Step 4: Run full suite**

Run: `python -m pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add bot/bot_app.py bot/tweet.py
git commit -m "feat(tweet): register tweet handlers + boot-time OAuth gate"
```

---

## Task B21: README + BotFather command list update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update commands table**

Open `README.md`. In the `## Commands` section (around line 211), add rows:

```markdown
| `/post` | Post the pending tweet draft (if any) |
| `/next` | Discard pending draft, regenerate (capped at `TWEET_NEXT_CAP`/day) |
| `/edit <text>` | Replace pending draft with your own text and post (280-char hard cap) |
| `/tweetable last` · `/tweetable <id>` | Mark a capture as eligible for the daily tweet pool |
| `/untweetable last` · `/untweetable <id>` | Unmark a capture as eligible |
```

- [ ] **Step 2: Update BotFather command block**

In the README's BotFather block (the `<details>` collapsed section near top), add:

```
post - post the pending tweet draft
next - regenerate the pending draft
edit - post your own version of the draft
tweetable - mark a capture as tweet-eligible
untweetable - unmark a capture
```

- [ ] **Step 3: Update env vars table**

In the schedule and optional config tables, add:

```markdown
| `SPARKS_ENABLED` | `true` | Master switch for the daily sparks job |
| `SPARKS_LOCAL_TIME` | `06:00` | When the daily sparks job runs |
| `TWEET_DAILY_V2_ENABLED` | `false` | Master switch for the v0 tweet pipeline |
| `TWEET_DRAFT_LOCAL_TIME` | `09:00` | When the daily tweet draft is DMed for approval |
| `TWEET_NEXT_CAP` | `5` | Max `/next` regenerations per day |
| `TWEET_POOL_DAYS` | `14` | Recency window before falling back to full corpus |
```

- [ ] **Step 4: Add a short "Daily Tweet" section under "Using it"**

Add a new subsection explaining: opt-in flow (`TWEET_DAILY_V2_ENABLED=true` + flag captures with `/tweetable`), how the morning DM works, the four approval commands, and that nothing posts without explicit user action. ~10 lines.

- [ ] **Step 5: Verify rendered markdown locally**

```bash
grep -n "TWEET_DAILY_V2_ENABLED\|/tweetable\|/post" README.md
```
Expected: each appears at least once.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs(readme): document daily tweet pipeline + sparks env vars"
```

---

## Track B — Completion checkpoint

After B1-B21, Track B ships gated:

- [ ] All `tests/test_tweet_*.py` and `tests/test_tweetable_handlers.py` green
- [ ] Full test suite passes (no regressions in pre-existing 275 tests)
- [ ] Bot deployed with `TWEET_DAILY_V2_ENABLED=false`
- [ ] User opt-in: run `/tweetable last` (or older) on N captures
- [ ] User flips `TWEET_DAILY_V2_ENABLED=true` in env, redeploys
- [ ] Next 09:00 local fires the first draft DM

If the first draft DM is unsatisfactory: `/next` for variety, `/edit` to override, `/skip` to silence — adjust `SYSTEM_TWEET_STITCH` few-shot in `bot/prompts.py` based on observed misses.

---

## Self-review notes

- **Spec coverage:** Each spec section maps to ≥1 task. Sparks fix → A1-A6. Tweet selection → B5. Theme detection → B6. Stitch generation → B7 + B4 (prompt). Validators → B2 + B3. Assembly → B8. State machine → B9. Ledger → B10. Orchestration → B11. Approval commands → B12-B15. Tweetable opt-in → B16. Frontmatter → B17. Status → B18. Scheduler → B19. Boot gate → B20. Docs → B21.
- **Type consistency:** `PendingDraft` shape used identically across `set_pending`, `get_pending`, `update_for_next`, `consume_for_post`. `ThemeProposal` used identically across `detect_themes`, `pick_theme`, `daily_tweet_draft_job`. `validate_quote_only` (singular) used throughout — matches `bot/digest/validate.py`.
- **No placeholders:** every code step contains the actual code. Test expectations are concrete strings/values. No "TODO" or "TBD" in any task body.
- **Open spec questions parked:** the 5 open questions in the spec (LLM provider for spark, provider for stitch, /edit policy, backfill scope, tweetable default) all resolved in the implementing tasks: spark uses `purpose="ingest"` (no new env var), stitch uses `purpose="tweet"` (existing default openai), `/edit` validates length only, backfill is format-only (no sparks regen for past days), tweetable defaults false.
