# to — The Commonplace

A personal Telegram bot for building your own commonplace book. Pipe in every highlight, overheard line, saved tweet, and link — `to` stores it, asks *"why?"* on links in the moment, prompts you for a daily reflection, and weekly produces an anthology essay composed entirely of your own quotes. The output also exports as a cumulative JSON backup compatible with [fz.ax](https://fz.ax) (year-in-weeks dashboard).

The bot is voiced by a persona named **orchurator** — part child, part fool, part sage. It never performs wisdom.

> Status: **Stage 1 / 7.** Plain-text capture + owner-gated Telegram bot. LLM processing, scraping, digests, Oracle, and tweets land in later stages.

## Stack

- Python 3.12 + FastAPI + `python-telegram-bot[webhooks]`
- SQLite (`aiosqlite`, WAL) with FTS5 for the Oracle (Stage 6)
- APScheduler for daily/weekly jobs (Stage 3+)
- Anthropic SDK with explicit prompt caching; OpenAI SDK with automatic prefix caching; both wired as peer providers (Stage 2+)
- Docker + Coolify for primary deploy; Fly / Railway / self-host documented

## Quickstart (local, polling)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r bot/requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, DOB
MODE=polling SQLITE_PATH=./to.db python -m bot.main
```

### Finding your `TELEGRAM_OWNER_ID`

Message [@userinfobot](https://t.me/userinfobot) and it returns your numeric Telegram user id. The bot rejects every chat that isn't this id.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

## Roadmap

See `/Users/rubberduck/.claude/plans/idea-the-commonplace-gleaming-platypus.md` for the full 7-stage plan.

## License

MIT.
