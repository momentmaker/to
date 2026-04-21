---
description: Generate this week's digest locally, in-conversation (no API key needed)
argument-hint: "[YYYY-wNN]"
---

# /weekly — the Sunday-morning ritual

You are generating this week's anthology digest using your own in-conversation capability. **Do NOT call the Anthropic API.** The whole point of this command is to use Claude Code instead of the user's API key.

This is the single most important moment in the project — one short essay, every week, composed entirely of the user's own words. Be careful. Be strict. Be quiet.

## Step 0 — Locate the captures repo

Check your memory system for a reference entry pointing to the user's **captures repo path** (the repo that holds `YYYY-wNN/` directories and `fz-ax-backup.json`). This is a per-user path; it is NOT hard-coded in this command.

- If you find it in memory, use it.
- If not, ask the user once: *"where is your `to` captures repo? (e.g. `~/GitHub/self`)"*. Then **save it as a reference memory** so you never ask again. Name: `to_captures_repo`. Description: `Path to the user's private captures repo (the YYYY-wNN/ archive for the to commonplace bot)`.

Also verify you can import the validator. From this (`to`) repo, `bot/digest/validate.py` defines `validate_quote_only`, `is_single_grapheme`, `whisper_ok`. You will call it via a Python subprocess in Step 5.

## Step 1 — Sync the captures repo

```bash
cd <captures-repo> && git pull --ff-only
```

If the pull fails (merge conflict, divergent history), stop and ask the user.

## Step 2 — Pick the target week

- If the user passed `$ARGUMENTS` (e.g. `2026-w17`), use that.
- Otherwise, list `YYYY-wNN/` dirs and pick the most recent one that does NOT already contain a `digest.md`. If the most recent week already has a digest, ask the user whether to regenerate it or pick a different week.

## Step 3 — Load the week's captures

Read every `*.md` in `<captures-repo>/<week>/` **except** `digest.md`. Each file has TOML frontmatter between `+++` fences, then a body:

```
+++
id = 123
kind = "text"      # text | url | image | voice | reflection
local_date = "2026-04-21"
title = "..."
tags = ["..."]
...
+++

<body — the user's words, scraped article text, OCR, transcript, or "why?" reply>
```

Parse the frontmatter and keep both `{kind, local_date, title}` and the body for each file.

Show the user a brief summary: file count, breakdown by kind, a few title previews. Keep it under 10 lines.

## Step 4 — Draft the digest

Compose three things. **Do NOT use the orchurator voice here.** This is the user reading themselves back, not the bot speaking.

### essay

- 3–8 short paragraphs, ≤1200 words total.
- **Every sentence must be a verbatim or near-verbatim substring of one of the fragment bodies.** Near-verbatim = identical content, case-insensitive, with punctuation and whitespace normalized. You may drop stopwords or trim leading/trailing words from a quote.
- You may NOT invent new sentences. You may NOT paraphrase. You may NOT combine content from different fragments into a single new sentence.
- No connective prose. No summary. No explanation. No "The week began with…" narration.
- Your only authorial moves: selection, ordering, juxtaposition, and paragraph breaks. Pick the sharper line when two say similar things.
- If the week is thin, the essay is thin. Do not pad.

### whisper

- One sentence, ≤240 characters.
- The user's voice (first-person or aphoristic, as the fragments suggest) — not yours.
- It can be a verbatim line from the fragments if one carries the week; otherwise it may be a minimal stitching of two phrases.

### mark

- Exactly ONE Unicode grapheme (one character or one emoji).
- Should visually encode the week's texture. A hexagram, a single CJK character, a kanji, an emoji — whichever feels right.

## Step 5 — Validate

Write the draft to a scratch JSON file and run the project's validator. From the `to` repo root (this repo), run:

```bash
python3 - <<'PY'
import json, sys
sys.path.insert(0, '<absolute-path-to-to-repo>')
from bot.digest.validate import validate_quote_only, is_single_grapheme, whisper_ok

essay = """<your essay here>"""
whisper = """<your whisper here>"""
mark = "<your mark here>"
corpus = [
    """<body 1>""",
    """<body 2>""",
    # ... one entry per fragment body
]

ok_q, offenders = validate_quote_only(essay, corpus)
print(json.dumps({
    "quote_ok": ok_q,
    "offenders": offenders,
    "mark_ok": is_single_grapheme(mark),
    "whisper_ok": whisper_ok(whisper),
}, ensure_ascii=False, indent=2))
PY
```

Interpretation:
- `quote_ok: false` with `offenders: [...]` → retry ONCE, stripping or replacing the offending sentences with actual substrings. If the second attempt still fails, stop and show the user the offenders.
- `mark_ok: false` → the mark is not a single grapheme; fix it.
- `whisper_ok: false` → the whisper is empty or >240 chars; fix it.

## Step 6 — Show and confirm

Render the draft in the chat:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  <mark>  <whisper>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<essay>
```

Ask: *"accept? [y/retry/edit]"*. On `retry`, start over at Step 4. On `edit`, apply the user's edits and re-run validation from Step 5 before proceeding.

## Step 7 — Write the files

### `<week>/digest.md`

```
# YYYY-WNN

**<mark>**  _<whisper>_

<essay>
```

Note the exact format: uppercase `W` in the header, two spaces between `**<mark>**` and `_<whisper>_`, a blank line between the mark/whisper line and the essay. This matches what `/tweetweekly` expects to parse.

### `fz-ax-backup.json` (at the repo root)

This is a cumulative file. Read-modify-write:

- If it doesn't exist: ask the user for their DOB and weekStart preference (mon/sun) and bootstrap a minimal state (see `scripts/weekly_digest.py:update_fz_backup` in the `to` repo for the exact shape).
- If it exists:
  1. Parse as JSON.
  2. Compute `fz_week_idx` as `(local_date_of_any_day_in_week - dob).days // 7`. The DOB is in `state.dob` (ISO `YYYY-MM-DD`).
  3. Set `state.weeks[str(fz_week_idx)] = {"mark": <mark>, "whisper": <whisper>, "markedAt": <now ISO UTC, seconds, Z suffix>}`.
  4. Insert `fz_week_idx` into `state.anchors` (keep sorted, dedup).
  5. Update `state.exportedAt` to the same `now` string.
  6. Write back with `json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False)` + trailing newline.

The easiest way is to delegate to the existing helper:

```bash
python3 - <<'PY'
import sys
sys.path.insert(0, '<absolute-path-to-to-repo>')
from pathlib import Path
from scripts.weekly_digest import update_fz_backup

# Compute fz_week_idx from DOB + a date in the target week.
# Read state.dob from the existing fz-ax-backup.json if present.
...
update_fz_backup(
    Path('<captures-repo>/fz-ax-backup.json'),
    fz_week_idx=<int>,
    mark=<mark>, whisper=<whisper>,
    marked_at=<now ISO>,
)
PY
```

## Step 8 — Commit and push

```bash
cd <captures-repo>
git add <week>/digest.md fz-ax-backup.json
git commit -m "weekly digest YYYY-wNN"
git push
```

If the push fails (remote has diverged), pull-rebase and try once more. If it still fails, stop and tell the user.

## Step 9 — Finish

Tell the user plainly:

```
done. 2026-w17 wrote:
  <captures-repo>/2026-w17/digest.md
  <captures-repo>/fz-ax-backup.json

open fz.ax to see the week. /tweetweekly on Telegram to post.
```

## Rules

- **Never touch the user's API key.** All LLM work happens in this conversation.
- **Never invent prose.** If the validator rejects something, fix it by selecting different fragments — not by softening the rule.
- **Never show the user draft essays that haven't been validated.** They should only ever see a quote-only-clean version.
- The orchurator voice is explicitly NOT used here. That voice is for the bot's questions, not the user's own digest.
- If any step fails or is ambiguous, stop and ask. This is the most important file the user has; don't push half-work.
