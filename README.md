# to — your commonplace book, run by a bot

**Pipe every highlight, overheard line, saved tweet, and link into Telegram. The bot stores it, asks _"why?"_ on links in the moment, nudges you for a daily reflection, and — if you want — weekly produces an anthology essay composed entirely of your own words.** Single-user by design. Your data lives in a private GitHub repo you own. MIT licensed.

The bot is voiced by a persona called **orchurator** — part child, part fool, part sage. It never performs wisdom.

```
orchurator is here. say anything and i will keep it.
```

---

## What this is

- A **Telegram bot** you DM your notebooks into — text, URLs, voice notes, photos, PDFs, HN threads, tweets.
- A **structured ingest pipeline** — Claude/GPT extracts title, tags, quotes, and a one-line summary from every capture.
- A **GitHub-backed archive** — every capture lands in a private repo as Markdown with TOML frontmatter, organized `YYYY-wNN/`.
- An **Oracle** (`/ask`) — consult your past self via BM25 retrieval + orchurator synthesis citing your own fragments by `[N]`.
- A **weekly anthology essay** composed from **your own verbatim quotes** (no hallucination — there's a substring validator) — either generated server-side by the bot, or locally by you via Claude Code against the captures repo.
- A **cumulative `fz-ax-backup.json`** matching [fz.ax](https://fz.ax) exactly, so your year-in-weeks dashboard updates itself.

Designed to run quietly on Coolify, Fly, Railway, or any Docker host. 275 tests. Typical monthly LLM spend: $5–$15 depending on capture volume.

---

## Run it in 15 minutes

You'll create two Telegram bots, one private GitHub repo, grab one API key, and deploy. In that order.

### 1. Create two Telegram bots

Message [@BotFather](https://t.me/botfather) on Telegram.

**Main bot** — the one you'll DM your captures to:
```
/newbot
name:     Commonplace
username: <your_handle>_to_bot
```
Save the token (e.g. `1234567890:ABC…`). This is your `TELEGRAM_BOT_TOKEN`.

Then, still in @BotFather:
```
/setprivacy     → pick your bot → Disable
/setcommands    → pick your bot → paste the command block below
```

<details>
<summary>📋 Command block to paste (click to expand)</summary>

```
status - corpus + budget + config
ask - consult your past self
reflect - force today's prompt
highlight - attach a passage to a capture (as reply)
forget - delete a capture (id or "last")
export - force weekly digest (opt-in)
tweetweekly - post the weekly tweet from digest.md
setmark - override this week's mark
setvow - set the year's vow
skip - dismiss pending question
help - command list
```
</details>

**Alerting bot (dhyama)** — gets startup + error + budget-cap pings so your main DMs stay clean:
```
/newbot
name:     Commonplace Alerts
username: <your_handle>_alerts_bot
```
Save this token as `DHYAMA_BOT_TOKEN`.

### 2. Get your Telegram IDs

**Your user id** — DM [@userinfobot](https://t.me/userinfobot). It replies with your numeric id (e.g. `12345678`). This is your `TELEGRAM_OWNER_ID`. Every chat that isn't this id is rejected.

**Alerting chat id** — DM your **alerting** bot with any message (say "hi"), then open in a browser:
```
https://api.telegram.org/bot<DHYAMA_BOT_TOKEN>/getUpdates
```
Copy the number from `"chat":{"id": NNNNN, ...}`. Often the same as your user id.

### 3. Create your private captures repo

On GitHub: **New repo → Private → name it anything** (e.g. `my-commonplace`). Seed it with a README so the default branch exists.

Then make a fine-grained PAT for the bot to push to it:

**Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new**
- Resource owner: your account
- Repository access: **Only select repositories** → pick your captures repo
- Permissions → Repository → **Contents: Read and write**

Save the token as `GITHUB_TOKEN`. Save `owner/repo` as `GITHUB_REPO`.

### 4. Get an LLM key

You need **at least one** of:

- **Anthropic Claude** (recommended — primary provider): [console.anthropic.com](https://console.anthropic.com) → API Keys → Create → `ANTHROPIC_API_KEY`. Load $10+ credit to start.
- **OpenAI** (required for Whisper voice transcription; otherwise optional): [platform.openai.com](https://platform.openai.com) → API keys → `OPENAI_API_KEY`. Load $5+.

You can run the whole bot on just Claude — voice notes won't transcribe, but every other path works.

### 5. Deploy

Pick one:

<details>
<summary><b>🚀 Coolify (recommended)</b></summary>

1. **Create new Application** → paste `https://github.com/momentmaker/to`
2. **Build Pack:** `Dockerfile`
3. **Port:** `8000`
4. **Environment Variables** — add everything from [Configuration](#configuration). Minimum for a working boot:
   ```
   TELEGRAM_BOT_TOKEN=…
   TELEGRAM_OWNER_ID=…
   DOB=YYYY-MM-DD
   TIMEZONE=America/Chicago     # IANA zone, NOT an abbreviation
   ANTHROPIC_API_KEY=…          # or OPENAI_API_KEY
   GITHUB_TOKEN=…
   GITHUB_REPO=owner/repo
   MODE=webhook
   TELEGRAM_WEBHOOK_SECRET=<openssl rand -hex 32>
   ```
5. **Storage → Volume Mount:** Name `to-data`, Destination `/data`, leave Source Path blank.
6. **Domains:** generate an HTTPS URL. Add it:
   ```
   TELEGRAM_WEBHOOK_URL=https://<your-coolify-url>/webhook
   ```
7. **Deploy.** First build ≈ 3 min.
8. Your alerting bot should ping: `🟢 [to] bot started (webhook)`.
</details>

<details>
<summary><b>Fly.io</b></summary>

```bash
fly launch --no-deploy
fly volumes create to_data --size 1 --region <region>
fly secrets set \
  TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… \
  DOB=… TIMEZONE=… \
  ANTHROPIC_API_KEY=… \
  GITHUB_TOKEN=… GITHUB_REPO=… \
  MODE=webhook TELEGRAM_WEBHOOK_SECRET=<random>
fly deploy
```
In `fly.toml`: `[mounts] source = "to_data", destination = "/data"`. Grab the app's URL, then `fly secrets set TELEGRAM_WEBHOOK_URL=https://<yours>.fly.dev/webhook`.
</details>

<details>
<summary><b>Railway</b></summary>

New project from GitHub repo, Railway auto-detects the `Dockerfile`. Set env vars in the dashboard. Attach a volume at `/data`. Generate a domain, set `TELEGRAM_WEBHOOK_URL=https://<domain>/webhook`.
</details>

<details>
<summary><b>Docker Compose (self-host)</b></summary>

```bash
git clone https://github.com/momentmaker/to
cd to
cp .env.example .env   # fill in
docker compose up -d
```
For webhook mode, put an HTTPS-terminating proxy (Caddy, nginx, Cloudflare Tunnel) in front of port 8000. For local testing, `MODE=polling` skips the webhook entirely.
</details>

### 6. First message

Open your main bot in Telegram. Send `/start`. You should see:
```
orchurator is here. say anything and i will keep it.
```

Then send anything — a line you overheard, a link, a photo of a book page, a voice note, a short PDF. You'll get `kept.` within a second, and within 60 seconds a new `.md` file in your GitHub captures repo.

---

## Using it

### What the bot accepts

| Send this | And it | Stored under |
|---|---|---|
| Plain text | Stores verbatim + LLM-extracts title/tags/summary | `kind=text` |
| A URL | Scrapes (Readability, Zyte fallback for JS-heavy, HN firebase for `news.ycombinator.com`, Exa for X/Reddit) → extracts → **asks you "why?"** | `kind=url`, with why as a child |
| A voice note | Transcribes via Whisper → processes as text | `kind=voice`, transcript in `payload.transcript` |
| A photo | Vision OCR + description via Claude/GPT-4o | `kind=image`, in `payload.vision` |
| A **PDF** | Extracts text via `pypdf`, classifies by token estimate (tiny / medium / large), processes normally. Scanned / image-only PDFs are rejected with a nudge to send a photo. Rejects >50 pages or >20k tokens | `kind=pdf`, text in `raw`, `payload.{page_count, token_estimate, tier, filename}` |
| A forwarded message | Preserves the forward metadata | `payload.forward_origin` |

### Commands

| Command | What it does |
|---|---|
| `/start` · `/help` | Orchurator-voiced welcome + command list |
| `/status` | Corpus count, this week's captures, LLM month-to-date spend per provider, cache hit rate, tweet state, config |
| `/ask <question>` | Consult your past self. Supports `since:YYYY-MM-DD` and `limit:N` anywhere in the question |
| `/reflect` | Force today's evening prompt to fire now |
| `/forget <id>` or `/forget last` | Irrevocably delete a capture (SQLite + GitHub). `last` targets the most recent |
| `/highlight <text>` (as a reply) | Attach a verbatim passage to a previous capture. Renders inline inside the parent's `.md`. Great for saving specific lines from a PDF or article you already captured |
| `/skip` | Clear any pending why-question or reflection prompt |
| `/setmark <glyph>` | Override the current week's mark (one emoji or character) |
| `/setvow <text>` | Pin the line you want above the year in fz.ax |
| `/export` | Force the weekly digest + fz.ax backup to regenerate now (opt-in cron; this always works regardless) |
| `/tweetweekly [YYYY-wNN]` | Read `<week>/digest.md` from the captures repo and post a ≤260-char tweet drawn from it. Defaults to the current week. Use this if you run the digest locally — the auto-tweet only fires when the bot runs the digest itself |

---

## The weekly ritual

The weekly digest is the single most expensive thing the bot does — one Opus-class call, ~$0.30–$1 per week. So it's **opt-in**. You have three modes:

### Default: local run (cheapest, most control)

The bot stays quiet on the weekend. At `WEEKLY_DIGEST_DOW` `WEEKLY_DIGEST_LOCAL_TIME` (default Saturday 22:00), it **pings you on Telegram** with:

> 🕯 digest time — 2026-W17 has 12 captures. pull the repo and run the digest prompt locally when you're ready.

You have **two paths** from there. Both produce the same files (`YYYY-wNN/digest.md` + updated `fz-ax-backup.json`) and both apply the same quote-only substring validator the bot uses server-side, so outputs are interchangeable with the server-side digest.

#### Path A — the dedicated CLI (recommended)

```bash
cd ~/my-commonplace
git pull
ANTHROPIC_API_KEY=sk-ant-... python /path/to/to/scripts/weekly_digest.py
```

Beautiful TUI with progress stages, grapheme-aware validation, cost estimate, interactive accept/retry, and (with `--push`) a full automation mode that pulls, generates, commits, and pushes in one command.

```bash
# full automation — run it once a week
ANTHROPIC_API_KEY=sk-ant-... python ~/path/to/to/scripts/weekly_digest.py --yes --push

# or a specific week with dry-run
python ~/path/to/to/scripts/weekly_digest.py --week 2026-w17 --dry-run

# or list what's available
python ~/path/to/to/scripts/weekly_digest.py --list
```

See `scripts/weekly_digest.py --help` for all options.

**Dependencies for the CLI**: `pip install anthropic tomli_w grapheme rich`

**💡 Tell your AI agent to remember your captures repo.** If you use Claude Code, Cursor, or similar, save a memory like:

> my `to` captures repo lives at `~/GitHub/my-commonplace` and the `weekly_digest` CLI is at `~/GitHub/to/scripts/weekly_digest.py`

Future sessions will just know — no re-explaining the layout.

#### Path B — `/weekly` slash command (Claude Code)

If you're a Claude Code user and don't want to burn API credit, this repo ships a `.claude/commands/weekly.md` slash command. Run Claude Code from this (`to`) repo, then:

```
/weekly              # latest week without a digest
/weekly 2026-w17     # a specific week
```

Claude Code itself drafts the essay/whisper/mark in-conversation (no Anthropic API key needed), runs the same `bot/digest/validate.py` quote-only validator the bot uses, writes `digest.md` + updates `fz-ax-backup.json`, and commits + pushes to your captures repo.

The command is **agnostic to your setup** — it checks memory for your captures repo path on first run, asks if missing, and remembers it. Works identically whether your private repo is `yourname/self`, `yourname/commonplace`, or anything else.

### On-demand (for any mode)

```
/export
```
forces the bot to run the digest right now. Respects `/setmark` if you've already picked a mark this week.

### Full auto

```
WEEKLY_DIGEST_ENABLED=true
```
makes the cron generate the digest server-side every Saturday. `/export` still works too.

---

## Configuration

### Required

| Var | Example | What |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `1234:ABC…` | From @BotFather |
| `TELEGRAM_OWNER_ID` | `12345678` | Numeric Telegram user id (@userinfobot) |
| `DOB` | `1990-01-15` | **Immutable** — drives the fz.ax week index. Set it right the first time. |
| `TIMEZONE` | `America/Chicago` | IANA zone. `CST` / `PST` don't work. |
| One of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | `sk-ant-…` / `sk-…` | At least one. Most features work on Anthropic alone. |

### GitHub sync (strongly recommended)

| Var | Default | What |
|---|---|---|
| `GITHUB_TOKEN` | — | Fine-grained PAT with contents:write on the captures repo |
| `GITHUB_REPO` | — | `owner/repo` of your private captures repo |
| `GITHUB_BRANCH` | `main` | Change to `master` if your repo defaults to master |

Without these, captures stay in the SQLite file only (not pushed anywhere).

### Webhook deploy

| Var | What |
|---|---|
| `MODE` | `webhook` (production) or `polling` (local dev) |
| `TELEGRAM_WEBHOOK_URL` | Your HTTPS URL + `/webhook` |
| `TELEGRAM_WEBHOOK_SECRET` | Random hex string; Telegram includes it as a header, bot rejects mismatches |

### LLM routing (per-purpose; defaults are sensible)

| Var | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER_INGEST` | `anthropic` | Structuring each capture |
| `LLM_PROVIDER_DAILY`  | `anthropic` | Evening prompt |
| `LLM_PROVIDER_WHY`    | `anthropic` | Capture-time why question |
| `LLM_PROVIDER_DIGEST` | `anthropic` | Weekly essay (quote-only) |
| `LLM_PROVIDER_ORACLE` | `anthropic` | `/ask` retrieval + synthesis |
| `LLM_PROVIDER_TWEET`  | `openai`    | Tweet drafting |
| `LLM_PROVIDER_VISION` | `anthropic` | Image OCR + description |
| `CLAUDE_MODEL_INGEST` | `claude-sonnet-4-6` | |
| `CLAUDE_MODEL_DIGEST` | `claude-opus-4-7` | Headline-feature model |
| `CLAUDE_MODEL_CHEAP`  | `claude-haiku-4-5-20251001` | Used above budget cap |
| `OPENAI_MODEL_INGEST` | `gpt-4.1-mini` | |
| `OPENAI_MODEL_DIGEST` | `gpt-4.1` | |
| `OPENAI_MODEL_CHEAP`  | `gpt-4.1-nano` | |

If only one key is set, the router silently falls back to whichever provider is configured, with a one-time warning per purpose. Functionally the bot works either way.

**Running OpenAI-only?** The defaults above all say `anthropic`, which means you'll see 7 "falling back to openai" warnings on your first day. To silence them, flip every `LLM_PROVIDER_*` to `openai`:

```
LLM_PROVIDER_INGEST=openai
LLM_PROVIDER_DAILY=openai
LLM_PROVIDER_WHY=openai
LLM_PROVIDER_DIGEST=openai
LLM_PROVIDER_ORACLE=openai
LLM_PROVIDER_TWEET=openai
LLM_PROVIDER_VISION=openai
```

**Running Anthropic-only?** Voice notes won't transcribe (Whisper is OpenAI-only — there's no Anthropic equivalent). The capture still lands with a `transcript_error` field; every other path (text, URL, photo, PDF, HN, Oracle, digest) works fine on Claude alone.

### Schedule

| Var | Default | What |
|---|---|---|
| `DAILY_PROMPT_LOCAL_TIME` | `21:30` | When the evening reflection fires |
| `WEEKLY_DIGEST_ENABLED` | `false` | `true` runs the digest server-side; `false` just DMs a reminder |
| `WEEKLY_DIGEST_DOW` | `sat` | `mon` `tue` `wed` `thu` `fri` `sat` `sun` |
| `WEEKLY_DIGEST_LOCAL_TIME` | `22:00` | Local time for the digest or reminder |
| `WHY_WINDOW_MINUTES` | `10` | After a URL save, how long to treat the next reply as the "why" |

### Optional

| Var | What |
|---|---|
| `ZYTE_API_KEY` | Scraper fallback for JS-heavy sites |
| `EXA_API_KEY` | Required for X.com / Reddit URLs (they block direct scraping) |
| `X_DAILY_ENABLED` · `X_WEEKLY_ENABLED` · `X_CONSUMER_KEY` · `X_CONSUMER_SECRET` · `X_ACCESS_TOKEN` · `X_ACCESS_TOKEN_SECRET` | Post reflections / digests to X (opt-in) |
| `DHYAMA_BOT_TOKEN` · `DHYAMA_CHAT_ID` | Separate Telegram bot for startup + error + budget alerts |
| `LLM_MONTHLY_USD_CAP` | Soft cap. At 90% you get a dhyama warning. Above 100%, non-digest calls degrade to `*_CHEAP` (digest is preserved) |

---

## How it works

For curious humans and AI agents trying to extend or debug.

### Architecture

```
Telegram                                                  GitHub (your private repo)
    │                                                           ▲
    │  webhook POST                                             │  PUT /contents
    ▼                                                           │
┌───────────────────┐   ┌───────────────────┐   ┌──────────────┴─────┐
│ bot/webhook.py    │──▶│ PTB Application   │──▶│ bot/handlers.py    │
│   (FastAPI)       │   │ (python-telegram- │   │   owner-gated,     │
│   owner-gated     │   │  bot)             │   │   dispatches kind  │
└───────────────────┘   └───────────────────┘   └──────┬─────────────┘
                                                        │
                           ┌────────────────────────────┼────────────────────────────┐
                           ▼                            ▼                            ▼
                    ┌─────────────┐             ┌──────────────┐           ┌──────────────┐
                    │ ingest/     │             │ llm/ router  │           │ github_sync  │
                    │   scraper   │             │   + budget   │           │   markdown   │
                    │   routing   │             │   guard      │           │   render     │
                    └─────┬───────┘             └──────┬───────┘           └──────┬───────┘
                          │                            │                          │
                          └──────────────┬─────────────┘                          │
                                         ▼                                        │
                                ┌────────────────┐                                │
                                │ SQLite (WAL)   │◀───────────────────────────────┘
                                │   captures     │
                                │   captures_fts │   ← FTS5, powers /ask
                                │   daily/weekly │
                                │   llm_usage    │   ← budget ledger
                                └────────────────┘
                                         ▲
                                         │
                                ┌────────┴────────┐
                                │  APScheduler    │
                                │   process_      │
                                │   pending (60s) │
                                │   nightly_sync  │
                                │   daily_prompt  │
                                │   weekly_*      │
                                └─────────────────┘
```

No external cron. Everything runs in the single Python process. Scheduler is APScheduler on asyncio. DB is a single aiosqlite connection in WAL mode.

### Key files

| Path | What lives there |
|---|---|
| `bot/main.py` | Entry point: polling vs webhook, signal handling, DB lifecycle |
| `bot/bot_app.py` | Telegram Application builder, handler registration, config validation |
| `bot/handlers.py` | Every `/command` + message-kind router. Owner gate is here. |
| `bot/db.py` | Schema + migrations (`MIGRATIONS` list + `PRAGMA user_version`), insert/query helpers |
| `bot/llm/` | Provider abstraction. `base.py` = types + timeout table. `anthropic.py` / `openai.py` = adapters with explicit prompt caching. `router.py` = per-purpose provider selection + budget-driven model degrade. `budget.py` = usage ledger + cap enforcement. |
| `bot/ingest/` | Scraping pipeline. `router.py` classifies + dispatches. `generic.py` / `zyte.py` / `hn.py` / `exa.py` for URLs. `voice.py` / `vision.py` for media. |
| `bot/process.py` | Post-ingest LLM call for title/tags/quotes/summary |
| `bot/oracle.py` | `/ask` — query expansion, FTS5 retrieval, orchurator synthesis with `[N]` citations |
| `bot/digest/` | Weekly pipeline. `weekly.py` = orchestrator. `validate.py` = quote-only + grapheme + whisper length. `fz_state.py` = cumulative fz.ax JSON builder. |
| `bot/forget.py` | `/forget` cascade logic (SQLite + GitHub, handles why-siblings) |
| `bot/tweet.py` | X posting (opt-in). `bot/scheduler.py` = all cron jobs. |
| `bot/persona.py` · `bot/prompts.py` | The orchurator voice block and every SYSTEM_* prompt |
| `bot/reflection.py` · `bot/why.py` | Pending-state machines in the `kv` table with atomic `DELETE ... RETURNING` consume |

### Key invariants worth knowing

- **User is always owner.** Every handler checks `TELEGRAM_OWNER_ID`. Non-owner chats are silently dropped.
- **Captures are append-only by default.** Only `/forget` removes them.
- **Dedupe by `(source, telegram_msg_id)`.** Telegram webhook retries won't create duplicates.
- **Whys and highlights render inline inside their parent's `.md` file.** They have their own row in SQLite but no separate GitHub file. Whys get a `## why?` section, highlights a `## highlights` section.
- **Quote-only enforcement is a normalized-substring check.** Case, punctuation, and whitespace are normalized; every sentence in the essay must be a substring of the combined corpus.
- **Digest is idempotent per `fz_week_idx`.** Running twice in the same week is a no-op unless `force=True`.
- **User-set marks (`/setmark`) survive digest runs.** The code preserves them via `user_mark_override`.

---

## Privacy warning

**Your private GitHub repo is private from randoms, not end-to-end encrypted.** If you paste something sensitive into `to` — an API key in a text message, a URL with `user:pass@…` credentials, an embarrassing DM screenshot — it lands in SQLite AND gets pushed to GitHub in plaintext. A future repo leak or GitHub breach would expose it.

Treat `to` like your notebook. Some things don't belong in a notebook.

If you do paste something by accident: `/forget last` removes it from both sides within ~2 seconds.

---

## Development

```bash
git clone git@github.com:momentmaker/to
cd to
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/          # 275 tests, runs in ~1s
```

For local testing:
```bash
MODE=polling SQLITE_PATH=./to.db python -m bot.main
```

No webhook needed in polling mode — the bot pulls updates directly from Telegram.

### Running specific tests

```bash
python -m pytest tests/test_oracle.py -v            # just the Oracle
python -m pytest tests/ -k "forget"                 # every /forget test
python -m pytest tests/ -k "budget or guard"        # budget-related
```

### Adding a new feature

Start in `bot/prompts.py` if you need a new SYSTEM prompt. Add a new `purpose` literal in `bot/llm/base.py`. Add a new handler in `bot/handlers.py`. Register it in `bot/bot_app.py`. Follow the test patterns in `tests/` — each bot module has a matching `test_*.py`.

---

## For AI agents setting this up

If you're Claude Code, Cursor, Devin, or similar, here's the fastest path to a working deploy for a user:

1. Ask the user for: their Telegram user id, their DOB (YYYY-MM-DD), their timezone (IANA — e.g. `America/Chicago` not `CST`), whether they want Anthropic only or Anthropic+OpenAI, whether they already have a GitHub repo for captures.
2. Create both Telegram bots via @BotFather in their session and collect tokens.
3. Help them generate the GitHub fine-grained PAT (they have to do this; you can't).
4. Walk them through Coolify (or their chosen host) using the exact env block from step 5 of [Run it in 15 minutes](#run-it-in-15-minutes).
5. Confirm the dhyama alerting bot received the `🟢 [to] bot started (webhook)` message before telling the user it's ready.
6. First smoke test: have the user send a plain text line. Within 60 seconds the file should appear in their captures repo. Then try a URL to confirm the why flow.
7. If the weekly reminder is what they want (default), tell them to look for the Saturday 22:00 ping and walk them through the Claude Code digest prompt from [The weekly ritual](#the-weekly-ritual).

Common mistakes to catch:
- `TIMEZONE` set to an abbreviation (`CST`, `PST`) — must be IANA.
- `GITHUB_BRANCH` not matching the actual default (check the repo — `main` vs `master`).
- Missing webhook secret — Telegram accepts without it but it's the only thing keeping rando bots from hitting the endpoint.
- OpenAI key missing but user sends voice notes — Whisper needs it, every other path works on Anthropic alone.

---

## License

MIT. See `LICENSE`.

Contributions welcome, but this is designed as a single-user tool. Fork it and make it yours.
