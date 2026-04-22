# `to` — daily Routine

A Claude Code Routine (`/schedule`) that runs once a day on Anthropic's cloud infrastructure and writes two artifacts back into your captures repo:

- **`sparks.md`** (repo root) — one line per day, the single sharpest verbatim line from yesterday's captures. Append-only. By end of year you have ~365 lines you can skim in two minutes.
- **`YYYY-wNN/YYYY-MM-DD-echo.md`** — orchurator-voiced observation + verbatim citations, *only when yesterday's captures echo something from your past corpus*. Silent days stay silent (no empty file).

Runs on Anthropic infra, so your laptop can be closed. Uses your Claude Code subscription tokens — no separate API key, ~$0.15–$0.60/month of token allowance for a ~5-minute daily run against a typical corpus.

---

## Setup

### 1. Install the Claude Code GitHub App on the captures-repo account

**This is the gotcha that will bite you if you skip it.** Routines run in Anthropic's cloud VM and reach your captures repo through the Claude Code GitHub App. The App must be installed on the **account that owns the captures repo**, not just the account you log into Claude Code with.

- If your captures repo lives at `myuser/self`, the App must be installed on `myuser`.
- If your Claude Code login and your captures-repo account are the same, you only install once.
- If they're different accounts (very common: a separate "personal" GitHub for captures), you need to log into that account on GitHub and install the App there too.
- If the captures repo is in an org, install the App on the org and grant it access to just that repo.

To install: log into the right GitHub account, open **https://github.com/apps** → search "Claude" → **Install**. Grant it access to only the captures repo (`Only select repositories` → pick `self` or whatever you named it).

Signs you skipped this step: when you run the trigger, you'll get `github_repo_access_denied: GitHub repository access check failed — re-authorize GitHub in settings`.

### 2. Create the trigger

In Claude Code (from anywhere — `/schedule` is global, not repo-scoped), run:

```
/schedule
```

When prompted for the routine content, paste the block labeled **"Routine prompt"** below. Set:

- **Schedule**: `0 12 * * *` (12:00 UTC daily — adjust for your timezone; 12:00 UTC = 07:00 CDT / 06:00 CST)
- **Name**: `to-daily`
- **Description**: `daily echo + sparks for the to commonplace bot`
- **Repo source**: your captures repo (`myuser/self`)
- **Model**: `claude-sonnet-4-6`

### 3. Substitute the per-user values

The Routine prompt below uses three per-user constants. The simplest path is to **bake them directly into the prompt** before pasting — do find+replace on these three tokens:

- `$CAPTURES_REPO` → your `owner/repo` (e.g. `myuser/self`)
- `$CAPTURES_BRANCH` → your branch (e.g. `master` or `main`)
- `$CAPTURES_TZ` → your IANA zone (e.g. `America/Chicago`)

| Constant | Example | What |
|---|---|---|
| `CAPTURES_REPO` | `myuser/self` | Your private captures repo (`owner/repo` format) |
| `CAPTURES_BRANCH` | `master` | Branch the bot writes to |
| `CAPTURES_TZ` | `America/Chicago` | IANA zone for "yesterday" boundary — **must match your bot's `TIMEZONE`** so `local_date` values align |

If your Claude Code cloud environment supports env vars (check the Environments UI — currently flaky in preview), you can also set these as env vars and keep the prompt unchanged. Baking the values in is more reliable today.

### 4. Test once

Before the first scheduled fire, run the Routine manually from the web UI to confirm auth and the code path. The first run on a day with captures should produce:

- A new line at the bottom of `sparks.md`
- Maybe a `YYYY-wNN/YYYY-MM-DD-echo.md` if there's an echo — **but echoes require at least one previous week of captures to rhyme against**, so your very first real run will likely skip the echo file. That's correct behavior, not a bug.

---

## Routine prompt

Paste everything between the `=== BEGIN ===` / `=== END ===` markers below into `/schedule`. Do not edit anything inside — the prompt reads env vars for per-user setup.

```
=== BEGIN ===
You are running the `to` daily Routine. Write two artifacts back to the user's
private captures repo. Be silent on ordinary days; the whole point is to honor
the "don't perform wisdom" rule.

## Environment

Read from env vars:
- CAPTURES_REPO      (e.g. "user/self")
- CAPTURES_BRANCH    (e.g. "master")
- CAPTURES_TZ        (IANA zone, e.g. "America/Chicago" — MUST match the bot's TIMEZONE)

The GitHub App (or GITHUB_TOKEN env) gives you read+write on CAPTURES_REPO.

## Step 1 — Clone the captures repo

```bash
git clone --branch "$CAPTURES_BRANCH" "https://github.com/$CAPTURES_REPO.git" captures
cd captures
```

If the repo is empty or has no `YYYY-wNN/` dirs, exit 0 without writing.

## Step 2 — Identify "yesterday"

Using $CAPTURES_TZ, compute yesterday's date (ISO `YYYY-MM-DD`). The Routine
fires in the morning and looks backwards at the complete day just ended.

Compute the ISO week for yesterday — format `YYYY-wNN` (lowercase `w`).

## Step 3 — Load yesterday's captures

Read every `.md` file under `<YYYY-wNN>/` that matches yesterday's date. Each
file has TOML frontmatter between `+++` fences, then a body. Collect:

- kind (text / url / voice / image / pdf / reflection — skip `why` and
  `highlight` since they're inline children of other captures)
- local_date
- title (optional, from `processed.title` or the frontmatter)
- body text

If yesterday has zero captures, exit 0 without writing.

## Step 4 — Pick the spark

From yesterday's captures, pick ONE line — the sharpest, most self-contained,
most re-readable sentence. Rules:

- Must be a verbatim substring of one of the capture bodies. No paraphrasing.
  No invention. Trimming leading/trailing words is fine.
- 8–200 characters. Not a URL. Not a title.
- Prefer the user's own words (reflection, why, text capture) over scraped
  article text when both are available.
- If nothing meets the bar, SKIP this step (no line appended).

Append one line to `sparks.md` at the repo root, format:

```
YYYY-MM-DD — <the line>
```

If `sparks.md` doesn't exist, create it with a `# sparks\n\n` header first.
Append at end, preserving chronological order.

## Step 5 — Detect echoes

For each of yesterday's captures, search the rest of the repo for past
captures that rhyme — not exact duplicates, but genuine thematic or phrasal
echoes. Use:

- Exact-phrase and substring matches via `git grep` (cheapest).
- Recurring tags (read `tags` from frontmatter).
- Conceptual recurrence (your own judgment — but be strict, not suggestible).

Cap at 3 echoes per day. If nothing genuinely rhymes, SKIP Step 6 — do NOT
pad the file with weak connections.

## Step 6 — Write the echo file (conditional)

Only if Step 5 found 1–3 real echoes, write
`<YYYY-wNN>/<YYYY-MM-DD>-echo.md`:

```
+++
kind = "echo"
for_date = "YYYY-MM-DD"
generated_at = "<now ISO UTC, Z suffix>"
+++

<orchurator voice — 1 to 3 short observations>
```

Each observation follows this pattern:

```
[YYYY-MM-DD] _<verbatim line from yesterday>_
[YYYY-MM-DD] _<verbatim line from the past>_
<one-sentence orchurator observation stitching them — short, literal,
bottomless, the way a child asks. No "beautiful", "wonderful", "journey",
or "resonate". No emojis unless the user used them.>
```

The quotes are MANDATORY and verbatim. The observation is orchurator's one
authorial move — keep it to a single short sentence per echo.

## Step 7 — Commit and push

Stage only the files you changed:

```bash
git add sparks.md "<YYYY-wNN>/<YYYY-MM-DD>-echo.md" 2>/dev/null
git diff --cached --quiet && exit 0  # nothing to commit
git -c user.name="to-daily-routine" -c user.email="routine@anthropic.local" \
    commit -m "daily: sparks + echo for YYYY-MM-DD"
git push origin "$CAPTURES_BRANCH"
```

## Rules

- **Silent days are good.** A no-spark, no-echo day produces nothing. Don't
  invent. Don't commit an empty commit. The weekly digest already handles
  the summarizing work; this Routine is for noticing.
- **Verbatim or not at all.** Every quote you write — spark line, echo
  observation citation — must be a verbatim substring of some capture in
  the repo. This mirrors the weekly digest's quote-only contract.
- **Orchurator only in transitions.** The observation that stitches two
  quotes is orchurator's voice. The quoted material stays the user's.
- **Never touch `digest.md`, `fz-ax-backup.json`, or existing capture
  files.** Your write surface is strictly `sparks.md` (append-only) and
  new `<week>/<date>-echo.md` files.
- **Cap at 3 echoes per day.** If more than 3 genuinely rhyme, pick the 3
  sharpest. Dilution is worse than exclusion.

Done. Exit 0 on success, non-zero if anything unrecoverable happened.
=== END ===
```

---

## How it fits the rest of `to`

- `sparks.md` can feed the weekly digest as *one of many* sources (the digest's
  quote-only validator passes fine since every spark is a verbatim user line).
- The echo file is the Oracle pattern made ambient — instead of `/ask`-on-demand
  retrieval, the Routine surfaces connections unprompted. Echo files are just
  more captures in the repo, searchable by the same Oracle.
- Silent-by-default honors the project's "don't perform wisdom" rule. On days
  with 0–2 ordinary captures and no echo, the Routine is a no-op.

## Why not a daily summary

Summarizing the user's day back at them is the inverse of what this project
does. `to`'s soul is the user reading themselves back — weekly essay is
quote-only, the daily prompt is a single orchurator *question*, not a
narration. A daily summary file where Claude describes your day to you
would violate all three. This Routine was reshaped toward **pattern
surfacing**, which the bot can't do on its own (it's single-day scoped) and
which actually earns its tokens.
