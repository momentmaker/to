# Sparks fix + daily tweet pipeline

**Date**: 2026-05-03
**Status**: Design — pending user review
**Tracks**: A (sparks fix), B (daily tweet)

---

## Background

Two related problems surfaced.

**Sparks bug.** `sparks.md` in the captures repo
(`~/GitHub/momentmaker/self/sparks.md`) is missing blank lines between
entries from `2026-04-23` onward. GitHub renders the run-on entries as a
single paragraph. The Claude Code Routine specified in
`.claude/routines/daily.md` documents a Python append block that
preserves blank-line separation, but the cloud-running model is not
following it. The instruction-as-prompt is fragile.

**No daily tweet.** The bot has `bot/tweet.py` with
`generate_daily_tweet`, gated by `X_DAILY_ENABLED`, but it fires only
inside the reflection handler (`handlers.py:249`) on today's fragments
plus today's reflection. There is no cron-driven daily tweet, no
ledger of what has been tweeted, no opt-in approval flow, and no way to
combine multiple captures into a single themed tweet.

The user wants:

1. Sparks newline bug fixed and the broken entries backfilled.
2. A daily tweet pipeline that picks captures from the corpus, combines
   2-3 of them on a theme, drafts a tweet in the orchurator voice, sends
   it to the user via Telegram for one-at-a-time approval, posts on
   approval, and remembers what it has tweeted so it never repeats.

The vibe constraint is non-negotiable: the orchurator "never performs
wisdom." Any voice the bot uses publicly must be bounded so it cannot
drift into engagement-bait, advice, or first-person opinion.

---

## Goals

- Fix `sparks.md` and stop the bug from recurring.
- Backfill the missing blank lines for `2026-04-23` through `2026-05-02`.
- Build a daily tweet pipeline that combines captures into themed tweets
  with a bounded orchurator voice and substring-validated quotes.
- Keep the user in the loop with one-at-a-time approval.
- Track which captures have been tweeted so the pipeline self-balances
  and never repeats a capture.
- Default-deny: no capture gets tweeted unless the user explicitly flags
  it as tweetable.

## Non-goals

- Mode rotation (spark / echo / contrarian / question / link-drop). All
  daily tweets in v0 use the stitch shape.
- Reply-to-self threading on echoes. Defer to v1.
- Tweetable opt-in inside the why-flow at ingest time. Defer to v1.
  v0 uses retroactive `/tweetable` commands.
- Public mirror site, RSS feed, or any non-X publishing surface.
- Inverse-engagement tracking. Wild idea, defer indefinitely.
- Auto-posting without user approval.

---

## Track A — Sparks fix

### Root cause

The Routine prompt in `.claude/routines/daily.md` documents a correct
Python append block that normalizes trailing newlines and inserts a
blank line between entries. The cloud-running model has been ignoring
the block, likely appending via shell `echo >>` or `printf`. The
correctness of the file is at the mercy of model adherence to a prompt
that is not enforced by code.

### Approach

Move the spark write server-side into the bot. The bot already has
SQLite access to all captures, the GitHub sync layer, and an
APScheduler. Spark selection and write become a deterministic Python
job. The Routine becomes echo-only (still LLM-driven, since echo
detection is a corpus-wide judgment task that benefits from a model).

### Files affected

- **New**: `bot/sparks.py`
  - `select_spark(conn, local_date, settings) -> str | None`
    - Pulls all captures for `local_date`, ranks candidates by a
      simple heuristic (length 8-200 chars, prefer reflection / why /
      text over scraped article body, prefer captures with a
      `processed.summary` over those without), then asks the configured
      ingest-purpose LLM to pick the single sharpest verbatim line.
    - Validates the picked line is a substring of one capture body via
      the existing normalized-substring check in
      `bot/digest/validate.py`. If validation fails, retry once. If
      retry fails, skip (no spark that day).
  - `append_spark(repo_path, date, line) -> None`
    - Reads `sparks.md`, normalizes trailing newlines to exactly one,
      appends `\n<date> — <line>\n`. Writes back. The leading `\n` on
      every append guarantees a blank line precedes every new entry
      regardless of prior file state. Idempotent: if the same
      `date — line` is already the last entry, no-op.
- **New**: `bot/scheduler.py` registration of `daily_sparks_job`
  - Runs at `SPARKS_LOCAL_TIME` (new env var, default `06:00` local).
  - Computes "yesterday" in `TIMEZONE`, calls `select_spark`, calls
    `append_spark`, commits to GitHub via `bot/github_sync.py`.
- **New**: `scripts/normalize_sparks.py` (one-time backfill)
  - Reads `sparks.md`, parses entries by leading `^\d{4}-\d{2}-\d{2}`
    line-anchor, rewrites the file with exactly one blank line between
    every entry, preserves the `# sparks` header.
  - Run once locally against `~/GitHub/momentmaker/self`. Commit. Push.
  - Idempotent: re-running on an already-correct file is a no-op.
- **Modified**: `.claude/routines/daily.md`
  - Mark Step 4 (spark) as deprecated and superseded by the bot-side
    job. Routine continues to handle Step 5-6 (echo detection + echo
    file write).

### Configuration

| Var | Default | Purpose |
|---|---|---|
| `SPARKS_ENABLED` | `true` | Allow opt-out without code change |
| `SPARKS_LOCAL_TIME` | `06:00` | When the daily job runs |

### Testing

- `tests/test_sparks.py`
  - `select_spark` returns a verbatim substring of one capture body.
  - `select_spark` returns `None` when no candidates qualify.
  - `append_spark` produces correct file shape from each of: empty
    file, header-only file, file ending in single `\n`, file ending in
    multiple `\n`, file already ending with the same entry (no-op).
- `tests/test_normalize_sparks.py`
  - Round-trips the actual broken `sparks.md` content into the corrected
    shape.
  - Idempotent on already-corrected input.

---

## Track B — Daily tweet pipeline (v0)

### User flow

```
09:00 local → bot DMs:

  draft 1/5

  "<orchurator stitch sentence>"

  — "<quote 1>" (2026-04-22)
  — "<quote 2>" (2026-04-15)

  258/280 chars · theme: privacy-asymmetry

  /post   /next   /edit <text>   /skip
```

- `/post` — posts the current draft to X, writes to ledger, clears
  pending state.
- `/next` — discards the current draft, regenerates with a different
  capture pair (and a different theme if available). Capped at 5 regens
  per day. After 5: bot DMs "pool exhausted — /post current, /skip, or
  /edit." `/next` past the cap is a no-op.
- `/edit <text>` — posts the user's text verbatim. Capture ids and
  theme from the most recent draft are recorded in the ledger with an
  `edited: true` flag. Stitch validators do not run on user-edited
  text. The single hard cap that DOES apply is X's 280-grapheme limit;
  text exceeding 280 chars is rejected with `too long: <N>/280` and
  pending state is preserved (user can re-`/edit`).
- `/skip` — clears pending state. No tweet today. No ledger entry.
- **Silent expire** — if the user does not respond by midnight local
  time, pending state is dropped. No nag, no auto-post.

### Selection

A capture is in the eligible pool if and only if all four filters pass:

1. **Kind filter.** Only captures of kind `text`, `url`, `voice`,
   `image`, `pdf`, or `reflection` are eligible. `why` and `highlight`
   rows are excluded — they live as inline children of other captures
   and are not standalone units.
2. **Tweetable opt-in subset.** A capture is eligible only if its
   payload has `tweetable = true` (canonical store is the SQLite
   `payload` JSON column; the markdown frontmatter mirrors it). v0
   ships with no captures flagged. Two new commands populate the pool:
   - `/tweetable last` — flag the most recent capture.
   - `/tweetable <id>` — flag any capture by id.
3. **Un-tweeted.** A capture's id must not appear in any prior tweet's
   `capture_ids` array in the ledger.
4. **Window.** Default to captures with `local_date` within the last
   14 days. If fewer than 2 candidates exist in the window, expand to
   the full corpus (any age).

Theme detection runs over the eligible pool. The bot calls the
ingest-purpose LLM with the pool's titles + summaries, asks for 1-3
themes that connect 2-3 captures each, returns a structured JSON
response: `[{theme, capture_ids[2-3], rationale}]`. The bot picks the
first theme not present (or least frequently present) in the ledger's
theme histogram. This biases the pipeline toward under-explored themes
over time.

If the LLM cannot find any 2-capture theme, the job exits silently. No
draft sent. No nag.

### Stitch generation

The bot calls `LLM_PROVIDER_TWEET` (default `openai`) with a new system
prompt `SYSTEM_TWEET_STITCH` and a user message containing the picked
captures' verbatim bodies + dates + the theme. The model returns
structured JSON: `{stitch: "<sentence>"}`.

`SYSTEM_TWEET_STITCH` enforces the bounded orchurator voice. It is a
new prompt, not a reuse of `SYSTEM_TWEET_DAILY`. It includes:

- Allowed verbs: stitch, name, frame, observe, notice, mark.
- Forbidden verbs: should, must, ought, will, predict, recommend,
  advise, urge, encourage, warn.
- Forbidden tone words / patterns: "we all," "everyone," "always,"
  "never" (as advice), exclamation points, hashtags, emoji, ellipsis,
  rhetorical questions.
- Person: second-person observation only ("you caught," "you keep,"
  "you saw"). No first-person ("i think," "to me").
- Length: ≤15 words, ≤80 characters.
- Form: one sentence, ends with a period or em-dash.
- 3-5 few-shot examples baked into the prompt source at write time.
  The echo file format (`YYYY-wNN/YYYY-MM-DD-echo.md`) already encodes
  this voice; concrete examples should be hand-picked from existing echo
  files and embedded in `bot/prompts.py` as static strings. The bot does
  not load examples from the captures repo at runtime.

### Validation

Three validators run on every generated draft. All must pass before the
draft is sent to the user.

1. **Quote substring validator.** Each verbatim quote in the draft
   must be a normalized-substring of one capture body. Reuses
   `bot/digest/validate.py:validate_quote_only` with the candidate
   capture bodies as the corpus.
2. **Stitch content validator** (`bot/tweet_validate.py:validate_stitch`):
   - Word count between 1 and 15 (no empty stitches).
   - Char count ≤ 80.
   - No first-person-singular tokens (`i`, `me`, `my`, `mine`, `i'm`,
     `i'd`, `i'll`, `i've`).
   - No forbidden verbs from the prompt list.
   - No `?`, `!`, `#`, emoji, ellipsis (`...` or `…`).
   - Exactly one sentence: optionally terminated by `.` or `—`, no
     internal sentence-ending punctuation.
3. **Total tweet length validator**
   (`bot/tweet_validate.py:validate_tweet_total_length`):
   - Total tweet character count ≤ 280 (X's hard limit), measured in
     graphemes via the existing `grapheme` library, with t.co URLs
     counted as 23 chars each regardless of original URL length.

If either validator fails, the bot retries generation up to 3 times
internally with the same captures + theme. If all 3 fail, that
capture+theme combination is abandoned and the bot picks the next theme
proposal (different captures). If no proposal succeeds:

- Cron-driven path (scheduled 09:00 fire): silent drop. No DM. Logs
  only.
- `/next`-driven path (user is awaiting reply): bot DMs `couldn't
  generate a draft — try /next again or /skip.` The `/next` count is
  not consumed when the bot fails to produce a valid draft; only
  successful drafts increment `draft_count`.

### Tweet format

```
<stitch sentence>

— "<quote 1>" (YYYY-MM-DD)
— "<quote 2>" (YYYY-MM-DD)
[<url>]
```

- The URL line appears only when at least one of the stitched captures
  is a URL-kind capture. Pick the URL of the *first* such capture by
  `local_date` (oldest among the picked pair).
- Dates are always included. They give the reader temporal context and
  signal "this is from a notebook, not a hot take."
- Quotes are wrapped in straight double-quotes. Em-dash bullets prefix
  each quote so the reader visually separates orchurator (no prefix)
  from corpus (prefixed).

### Char budget

X tweet limit = 280 chars (graphemes). t.co URL = 23 chars
regardless of original URL length. The stitch validator enforces both
≤15 words AND ≤80 chars on the stitch sentence; the tighter constraint
wins at runtime.

Per-quote-line overhead, where `<body>` is the quote text:

```
— "<body>" (YYYY-MM-DD)\n
```

| Element | Chars |
|---|---|
| `— ` (em-dash + space) | 2 |
| `"` open + `"` close | 2 |
| ` ` separator before paren | 1 |
| `(YYYY-MM-DD)` | 12 |
| `\n` | 1 |
| **Per-line overhead (no body)** | **18** |

Total tweet budget:

| Component | Budget |
|---|---|
| Stitch sentence | ≤80 |
| Blank line after stitch (`\n\n`) | 2 |
| Two quote-line overheads (18 × 2) | 36 |
| URL line, optional (`\n` + 23-char t.co) | 0 or 24 |
| **Available for two quote bodies combined** | 280 − 80 − 2 − 36 − (0 or 24) = **162 or 138** |

The selection step trims quotes to fit by truncating from the right at
a word boundary if needed. If a quote would need to be cut below 30
characters to fit, that capture is rejected from the pair and the LLM
is re-asked for a different pair.

### Approval state machine

State lives in the existing `kv` table, key `pending_tweet_draft`,
value JSON:

```json
{
  "draft_text": "<full tweet text>",
  "capture_ids": ["a1b2", "c3d4"],
  "theme": "privacy-asymmetry",
  "stitch": "both times you caught the asymmetry...",
  "draft_count": 1,
  "char_count": 258,
  "created_at": "2026-05-03T09:00:00Z"
}
```

- `/post`, `/edit`, `/skip` use atomic `DELETE ... RETURNING` on the
  `pending_tweet_draft` row (same pattern as `bot/why.py` and
  `bot/reflection.py`). Concurrent commands race-safe.
- `/next` uses an atomic `UPDATE ... RETURNING` on the same row,
  replacing `draft_text`, `capture_ids`, `theme`, `stitch`,
  `char_count`, and incrementing `draft_count`. The row's
  `created_at` is preserved so midnight expiry still applies to the
  original day's session.

Midnight expiry runs in the existing `process_pending` 60-second loop
(small modification — adds one query). Drops drafts where `created_at`
is from a prior local day. Counter reset on the new day is implicit:
expiry deletes the row, the next 09:00 fire creates a fresh row with
`draft_count = 1`.

### Ledger

Source of truth: SQLite. Durable mirror: JSON file in captures repo.

**SQLite** — new table via migration:

```sql
CREATE TABLE tweets (
  tweet_id TEXT PRIMARY KEY,
  tweeted_at TEXT NOT NULL,           -- ISO UTC
  local_date TEXT NOT NULL,           -- YYYY-MM-DD in TIMEZONE
  capture_ids TEXT NOT NULL,          -- JSON array
  theme TEXT,                         -- nullable for /edit-only entries
  stitch TEXT,                        -- nullable for /edit-only entries
  text TEXT NOT NULL,                 -- final tweet text as posted
  draft_count INTEGER NOT NULL,
  edited INTEGER NOT NULL DEFAULT 0   -- bool
);

CREATE INDEX tweets_theme_idx ON tweets(theme);
```

**Repo file** — `tweeted.json` at captures repo root, append-only:

```json
[
  {
    "tweet_id": "1789...",
    "url": "https://x.com/i/web/status/1789...",
    "tweeted_at": "2026-05-03T14:14:00Z",
    "local_date": "2026-05-03",
    "capture_ids": ["a1b2", "c3d4"],
    "theme": "privacy-asymmetry",
    "stitch": "both times you caught the asymmetry between what's kept on you and what you keep.",
    "text": "<full tweet text>",
    "edited": false
  }
]
```

Written via `bot/github_sync.py` after successful X post. Failure to
push the ledger to GitHub does not roll back the tweet (the SQLite row
is canonical) but does log a warning to dhyama.

### Tweetable opt-in (v0)

Two new commands in `bot/handlers.py`:

- `/tweetable last` — sets `tweetable = true` in the most recent
  capture's payload JSON AND immediately re-syncs that capture's
  markdown file to GitHub via `bot/github_sync.py` so the repo
  frontmatter stays in lockstep with SQLite.
- `/tweetable <id>` — same, by id.
- `/untweetable last` and `/untweetable <id>` — inverse, for mistakes.
  Also re-syncs the affected capture's md file.

Tweetable flag lives in `payload.tweetable` (boolean). The selection
query filters on `JSON_EXTRACT(payload, '$.tweetable') = 1`.

This is intentionally manual in v0. Default-deny means a fresh deploy
posts nothing until the user has opted some captures in. Safe.

v1 candidate: extend the why-flow on URL captures with a "tweet later?"
follow-up prompt.

### Configuration

| Var | Default | Purpose |
|---|---|---|
| `TWEET_DAILY_V2_ENABLED` | `false` | Master switch for the v0 pipeline |
| `TWEET_DRAFT_LOCAL_TIME` | `09:00` | When the daily draft DM fires |
| `TWEET_NEXT_CAP` | `5` | Max `/next` regens per day |
| `TWEET_POOL_DAYS` | `14` | Recency window before falling back to full corpus |

`X_DAILY_ENABLED` (the existing reflection-triggered daily) is
deliberately NOT removed in this design. It and the new pipeline can
coexist — a future cleanup PR can deprecate one. v0 ships as additive.

**Boot-time validation.** `bot/bot_app.py`'s config validator must
check: if `TWEET_DAILY_V2_ENABLED=true` AND
`bot/tweet.py:_oauth_configured(settings)` returns false, log a warning
to dhyama (`tweet_v2: enabled but OAuth not configured — disabling
auto-fire`) and treat the flag as false at runtime. Drafts that cannot
be posted should never be generated.

### Files affected

- **New**: `bot/sparks.py` (Track A; mentioned here for parity)
- **New**: `bot/tweet_daily.py`
  - `pick_eligible_pool(conn, settings) -> list[Capture]`
  - `detect_themes(pool, providers) -> list[ThemeProposal]`
  - `pick_theme(theme_proposals, ledger_histogram) -> ThemeProposal | None`
  - `generate_stitch(captures, theme, providers) -> str`
  - `assemble_tweet(stitch, captures) -> str`
  - `daily_tweet_draft_job(settings, providers, conn, bot)`
- **New**: `bot/tweet_validate.py`
  - `validate_stitch(text) -> ValidationResult`
  - `validate_tweet_total_length(text) -> ValidationResult`
- **Modified**: `bot/prompts.py` — add `SYSTEM_TWEET_STITCH` with
  few-shot examples drawn from existing echo files.
- **Modified**: `bot/handlers.py`
  - `/post`, `/next`, `/edit`, `/skip` handlers (route to
    `pending_tweet_draft` state).
  - `/tweetable`, `/untweetable` handlers (mutate payload + re-sync md
    via `bot/github_sync.py`).
  - `/status` extended with tweet pipeline state (today's draft?
    regen count? ledger total?).
- **Modified**: `bot/scheduler.py`
  - `daily_sparks_job` registration (Track A).
  - `daily_tweet_draft_job` registration (Track B).
  - Pending-tweet expiry handled inside existing `process_pending`.
- **Modified**: `bot/db.py` — migration adding `tweets` table + indices.
  Bumps `PRAGMA user_version`.
- **Modified**: `bot/config.py` — new env vars.
- **Modified**: `bot/bot_app.py` — register new command handlers AND
  add the boot-time OAuth validation gate (see Configuration above).
- **Modified**: `bot/markdown_out.py` — render `tweetable` flag in
  frontmatter when set.
- **Modified**: `README.md` — add `/tweetable`, `/untweetable`,
  `/post`, `/next`, `/edit`, `/skip` to the command table; add new env
  vars; note the BotFather `/setcommands` block needs the same
  additions.

### Testing

- `tests/test_tweet_daily_select.py`
  - Empty pool → no draft.
  - Pool of 1 → no draft (cannot stitch).
  - Pool of 5+ → at least one theme proposed.
  - Already-tweeted captures excluded.
  - Window expansion when recent pool too small.
- `tests/test_tweet_stitch_validate.py`
  - Stitch with forbidden verb → fail.
  - Stitch with `i think` → fail.
  - Stitch >15 words → fail.
  - Stitch with `?` or `!` → fail.
  - Valid stitch → pass.
- `tests/test_tweet_assemble.py`
  - Format with URL kind → URL line present.
  - Format without URL kind → no URL line.
  - Quote truncation at word boundary when over budget.
  - Reject pair when truncation would go below 30 chars.
- `tests/test_tweet_handlers.py`
  - `/post` posts and writes ledger.
  - `/next` decrements regen counter, regenerates.
  - `/next` past cap → no-op + message.
  - `/edit <text>` posts user text, marks `edited=true`.
  - `/skip` clears pending state.
  - Midnight expire drops stale draft.
- `tests/test_tweetable_handlers.py`
  - `/tweetable last` sets flag on most recent capture.
  - `/tweetable <id>` sets flag by id.
  - `/untweetable` clears flag.
  - Selection respects flag.

---

## Migration

Order matters.

1. **Sparks fix lands first.** Backfill script run, normalized
   `sparks.md` committed and pushed. New bot job deployed. Routine
   updated to skip Step 4. Verify next morning's spark appears with
   correct blank-line spacing.
2. **Tweet pipeline lands second**, gated behind
   `TWEET_DAILY_V2_ENABLED=false` by default. No surprise tweets.
3. **User opt-ins**. User runs `/tweetable last` (or older) on captures
   they want in the pool. Without opt-ins the pipeline runs but finds an
   empty pool and exits silently.
4. **User flips `TWEET_DAILY_V2_ENABLED=true`** when ready. Next 09:00
   local fires the first draft.

No data migrations required for existing captures. The `payload`
column on `captures` is already a JSON blob; adding `tweetable` is
schema-free.

The new `tweets` table requires a migration entry in `bot/db.py`'s
`MIGRATIONS` list and a `PRAGMA user_version` bump. Standard pattern,
already established.

---

## Out of scope (v1 candidates)

- Reply-to-self chains for echo tweets.
- Tweetable opt-in inside the why-flow at URL ingest.
- Multi-tweet threads (long-form stitch, tree of related captures).
- Image attachment for image-kind captures (vision OCR + photo).
- Theme histogram balancing as a configurable strategy (currently
  picks least-frequent theme; could be configurable).
- Inverse-engagement tracking.
- Public mirror site / RSS feed.
- Auto-post mode (no approval).

---

## Open questions for review

1. **Spark generation provider.** Track A uses
   `LLM_PROVIDER_INGEST` for spark selection. Alternative: a new
   `LLM_PROVIDER_SPARK` env var. Default-route to ingest is cheaper
   and follows the principle of "no new env var unless the user asked
   for it." Confirm or override.
2. **Stitch tweet provider.** Track B uses the existing
   `LLM_PROVIDER_TWEET` (default `openai`). The orchurator persona is
   thicker on Anthropic models historically. Worth defaulting `tweet`
   purpose to `anthropic`? Defer this until we see drafts.
3. **`/edit` validation policy.** v0 deliberately runs no validators on
   user-edited text — the user's word is final. Confirm.
4. **Backfill scope.** The backfill script normalizes `sparks.md`
   formatting only — fixes blank-line spacing between existing entries.
   It does NOT retroactively run `select_spark` against past days that
   have no entry in the file (e.g. days the routine skipped or never
   ran on). Confirm scope-as-stated; alternative is to add a
   `--backfill-from YYYY-MM-DD` mode to the new bot job that walks
   missing days once.
5. **Tweetable default.** v0 defaults to `tweetable = false` (omitted
   field is treated as false). Confirm; the alternative is
   `tweetable = true` by default with `/untweetable` to opt out, which
   is much riskier for privacy.

---
