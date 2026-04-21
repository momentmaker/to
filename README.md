# to — The Commonplace

A personal Telegram bot for building your own commonplace book. Pipe in every highlight, overheard line, saved tweet, and link — `to` stores it, asks *"why?"* on links in the moment, prompts you for a daily reflection, and weekly produces an anthology essay composed entirely of your own quotes. The output also exports as a cumulative JSON backup compatible with [fz.ax](https://fz.ax) (year-in-weeks dashboard).

The bot is voiced by a persona named **orchurator** — part child, part fool, part sage. It never performs wisdom.

MIT licensed. Single-user by design (one owner per deploy).

---

## What `to` does

1. **Ingests** every highlight you send it — plain text, URLs (article/HN/Reddit/X), voice notes (Whisper transcription), photos (Claude/GPT vision OCR + description).
2. **Asks you "why?"** in the moment whenever you save a link — captures the spark before you forget it.
3. **Writes every capture** to a private GitHub repo you own, as Markdown with TOML frontmatter, organized by week.
4. **Prompts you at your chosen hour** for a daily reflection built from today's captures.
5. **Weekly**, it generates an anthology essay composed **entirely of your own words** (strict quote-only validation), plus a one-sentence "whisper" and a single-glyph "mark" — all exported as a cumulative `fz-ax-backup.json` you can drop into fz.ax.
6. **Lets you consult your past self** via `/ask <question>` — BM25 retrieval across everything you've saved, then orchurator weaves an answer citing your own fragments.
7. **Optionally tweets** daily reflections or weekly whispers (opt-in, your creds).

---

## Stack

- Python 3.12 + FastAPI + `python-telegram-bot[webhooks]`
- SQLite (`aiosqlite`, WAL) with FTS5 for the Oracle
- APScheduler for daily prompts, weekly digests, nightly catch-up
- Anthropic + OpenAI SDKs (per-purpose routing, explicit prompt caching on Claude, automatic prefix caching on GPT-4.1)
- Whisper (OpenAI) for voice transcription
- Zyte + Exa for scraping JS-heavy pages and X/Reddit
- tweepy for the optional X integration
- Docker + Coolify for primary deploy; Fly/Railway/self-host documented below

---

## Quickstart (local, polling mode)

```bash
git clone git@github.com:momentmaker/to.git
cd to
python -m venv .venv && source .venv/bin/activate
pip install -r bot/requirements.txt
cp .env.example .env
# edit .env with at minimum:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_OWNER_ID     (see below)
#   DOB                   (YYYY-MM-DD — drives the fz.ax week index)
#   ANTHROPIC_API_KEY     (or OPENAI_API_KEY; at least one required)
MODE=polling SQLITE_PATH=./to.db python -m bot.main
```

Message your bot on Telegram and send anything — text, a URL, a voice note. You'll get `"kept."` and, on URLs, an orchurator "why?" question.

### Finding your `TELEGRAM_OWNER_ID`

Message [@userinfobot](https://t.me/userinfobot) and it returns your numeric Telegram user id. Every chat that isn't that id is rejected — this is a single-user bot by design.

---

## Commands

| Command | Purpose |
|---|---|
| `/start` `/help` | Orchurator-voiced welcome + command list |
| `/status` | Corpus count, this week's captures, LLM month-to-date spend + cache hit rate, tweet status |
| `/ask <question>` | Consult your past self. Supports `since:YYYY-MM-DD` and `limit:N` modifiers |
| `/reflect` | Force today's evening prompt to fire now |
| `/skip` | Clear any pending why-question or reflection prompt |
| `/setvow <text>` | Store the line you want pinned above the year (shows in fz.ax) |
| `/setmark <glyph>` | Override the current week's mark (single Unicode character) |
| `/export` | Force the weekly digest + fz.ax backup to regenerate now |

---

## Deploy

### Coolify (recommended)

1. Add a new **Dockerfile** resource pointing at this repo.
2. Set environment variables from `.env.example` (at minimum: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `DOB`, one of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`).
3. Set `MODE=webhook`, `TELEGRAM_WEBHOOK_URL` to your Coolify-provided HTTPS URL, and a random `TELEGRAM_WEBHOOK_SECRET`.
4. Attach a persistent volume mounted at `/data` so the SQLite file survives deploys.
5. Coolify handles TLS termination. Port 8000 is exposed by the container.

### Fly.io

```bash
fly launch --no-deploy
fly volumes create to_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_ID=... DOB=... ANTHROPIC_API_KEY=...
fly deploy
```

Add `[mounts]` in `fly.toml`: `source = "to_data"`, `destination = "/data"`. Bot runs webhook mode on port 8000, Fly's proxy handles TLS.

### Railway

1. New project from GitHub repo. Railway auto-detects the Dockerfile.
2. Set environment variables in the Railway dashboard.
3. Attach a volume at `/data`.
4. Generate a domain; use it for `TELEGRAM_WEBHOOK_URL`.

### Self-host (Docker Compose)

```bash
cp .env.example .env  # fill in
docker compose up -d
```

The compose file mounts a named volume `to-data` at `/data`. For webhook mode, put an HTTPS-terminating proxy (Caddy, nginx, Cloudflare Tunnel) in front of port 8000.

---

## Config reference

### Required

| Var | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/botfather) |
| `TELEGRAM_OWNER_ID` | Your numeric Telegram user id (from @userinfobot) |
| `DOB` | `YYYY-MM-DD` — drives the fz.ax week index; immutable after first use |
| One of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | At least one |
| `TIMEZONE` | IANA zone like `Asia/Tokyo`; default `UTC` |

### GitHub sync (strongly recommended)

| Var | What |
|---|---|
| `GITHUB_TOKEN` | Fine-grained PAT with `contents:write` on the target repo |
| `GITHUB_REPO` | `owner/repo` of your **private** commonplace repo |
| `GITHUB_BRANCH` | Default `main` |

Without these, captures stay in the SQLite file only.

### LLM routing

| Var | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER_INGEST` | `anthropic` | Structuring each capture |
| `LLM_PROVIDER_DAILY`  | `anthropic` | Daily prompt generation |
| `LLM_PROVIDER_WHY`    | `anthropic` | Capture-time why questions |
| `LLM_PROVIDER_DIGEST` | `anthropic` | Weekly essay (quote-only) |
| `LLM_PROVIDER_ORACLE` | `anthropic` | `/ask` retrieval + synthesis |
| `LLM_PROVIDER_TWEET`  | `openai`    | Tweet drafting |
| `LLM_PROVIDER_VISION` | `anthropic` | Image OCR + description |

If you only have one key, set all seven to the same provider — the router will fall back to whichever is available.

### Optional

- **Scrapers** (`ZYTE_API_KEY`, `EXA_API_KEY`) — improve recall on JS-heavy sites and X/Reddit.
- **Scheduling** (`DAILY_PROMPT_LOCAL_TIME`, `WEEKLY_DIGEST_DOW`, `WEEKLY_DIGEST_LOCAL_TIME`, `WHY_WINDOW_MINUTES`).
- **X posting** (`X_DAILY_ENABLED`, `X_WEEKLY_ENABLED`, `X_CONSUMER_KEY`, `X_CONSUMER_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`).
- **Alerting** (`DHYAMA_BOT_TOKEN`, `DHYAMA_CHAT_ID`) — a separate Telegram bot that receives startup + error + budget-cap notifications.
- **Budget** (`LLM_MONTHLY_USD_CAP`) — soft cap; at 90% you get a dhyama warning, at 100% non-digest calls degrade to `*_CHEAP` models. Digest is always preserved.

---

## Privacy warning

**Your private GitHub repo is private from randoms, not end-to-end encrypted.** If you paste something sensitive into `to` (an API key in a text message, a URL with `user:pass@…` credentials, an embarrassing DM screenshot), it lands in SQLite AND gets pushed to GitHub in plaintext. A future repo leak or GitHub breach would expose it.

Treat `to` like your notebook — some things don't belong in a notebook.

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

253 tests, runs in ~1s. Stages 1–7 cover the full pipeline; see `/Users/rubberduck/.claude/plans/idea-the-commonplace-gleaming-platypus.md` (local-only planning artifact) for the stage breakdown.

---

## License

MIT. See `LICENSE`.
