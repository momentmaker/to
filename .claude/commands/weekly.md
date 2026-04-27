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

- If the user passed `$ARGUMENTS` (e.g. `2026-w17`), validate it matches `^\d{4}-w\d{2}$` and the directory `<captures-repo>/<week>/` exists. If it doesn't exist, stop and tell the user which weeks DO exist.
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
  2. Read `fz_week_idx` straight from any capture's TOML frontmatter (`week_idx`). The bot already computed it correctly per `local_date` at capture time. **Do NOT recompute it from the ISO Monday** — for any non-Monday DOB, iso-Mon falls in the previous fz-week and you'll anchor the digest one hex behind the captures.
  3. Set `state.weeks[str(fz_week_idx)] = {"mark": <mark>, "whisper": <whisper>, "markedAt": <now ISO UTC, seconds, Z suffix>}`.
  4. Insert `fz_week_idx` into `state.anchors` (keep sorted, dedup).
  5. Update `state.exportedAt` to the same `now` string.
  6. Write back with `json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False)` + trailing newline.

The easiest way is to delegate to the existing helper. Example (substitute the bracketed values):

```bash
python3 - <<'PY'
import re, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, '<absolute-path-to-to-repo>')
from scripts.weekly_digest import update_fz_backup

captures_root = Path('<captures-repo>')
week = '<week>'  # e.g. "2026-w17"

# Pull fz_week_idx from any capture's frontmatter — authoritative, DOB-agnostic.
fz_week_idx = None
for md in sorted((captures_root / week).glob("*.md")):
    if md.name == "digest.md":
        continue
    m = re.search(r'^week_idx\s*=\s*(\d+)\s*$', md.read_text(encoding="utf-8"), re.M)
    if m:
        fz_week_idx = int(m.group(1))
        break
if fz_week_idx is None:
    print("no capture with week_idx in frontmatter; cannot anchor digest")
    sys.exit(1)

backup = captures_root / 'fz-ax-backup.json'
if not backup.exists():
    print("no fz-ax-backup.json — pass dob + week_start on first run")
    sys.exit(1)

now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

update_fz_backup(
    backup,
    fz_week_idx=fz_week_idx,
    mark="<mark>", whisper="<whisper>",
    marked_at=now_iso,
)
print(f"updated fz-ax-backup.json for week {fz_week_idx}")
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

## Step 9 — Optional: compose a tweet and copy it to the clipboard

Ask the user: *"want a tweet ready to paste on X? [y/N]"*. Default is no — skip to Step 10.

If yes, draft ONE tweet following these rules (same constraints as the bot's `SYSTEM_TWEET_WEEKLY` in `bot/prompts.py`, but since we're in-conversation, emit the text directly — no JSON envelope):

- **Maximum 260 characters** — count graphemes, not code units. Emoji sequences (ZWJ families, flags) count as one each.
- In the user's voice, NOT orchurator's.
- Anchor on the whisper if it fits; otherwise quote the strongest line from the essay. You may combine with the mark as a leading glyph.
- Engaging without performing. No hashtags unless they appeared in the user's fragments. No `@` mentions. No emojis unless the user used them.
- No preamble, no sign-off, no quotes wrapping the whole tweet.

Validate the length programmatically (grapheme-accurate) and copy to the clipboard. From the `to` repo:

```bash
python3 - <<'PY'
import subprocess, sys, shutil
sys.path.insert(0, '<absolute-path-to-to-repo>')
from bot.tweet import truncate_tweet
import grapheme

tweet = """<your tweet here>"""
trimmed = truncate_tweet(tweet)  # grapheme-aware, no ellipsis
length = grapheme.length(trimmed)

# Prefer pbcopy (macOS), fall back to xclip, wl-copy, or print.
# A binary present on PATH can still fail at runtime (e.g. xclip in headless
# SSH, wl-copy without a Wayland session) — catch that and try the next one.
copied = False
for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
    if not shutil.which(cmd[0]):
        continue
    try:
        subprocess.run(cmd, input=trimmed, text=True, check=True,
                       capture_output=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        continue
    print(f"copied ({length} graphemes) via {cmd[0]}")
    copied = True
    break
if not copied:
    print("no clipboard tool available; paste manually:")
    print(trimmed)
PY
```

Show the tweet to the user in the chat too — they should see what's on their clipboard before they paste.

## Step 10 — Finish

Tell the user plainly:

```
done. 2026-w17 wrote:
  <captures-repo>/2026-w17/digest.md
  <captures-repo>/fz-ax-backup.json

open fz.ax to see the week.
to post:
  • /tweetweekly on Telegram (auto)
  • or paste — the tweet is on your clipboard (if you said yes in Step 9)
```

## Rules

- **Never touch the user's API key.** All LLM work happens in this conversation.
- **Never invent prose.** If the validator rejects something, fix it by selecting different fragments — not by softening the rule.
- **Never show the user draft essays that haven't been validated.** They should only ever see a quote-only-clean version.
- The orchurator voice is explicitly NOT used here. That voice is for the bot's questions, not the user's own digest.
- If any step fails or is ambiguous, stop and ask. This is the most important file the user has; don't push half-work.
