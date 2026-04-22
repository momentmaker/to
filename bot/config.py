from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_OWNER_ID: int = 0
    MODE: str = "polling"
    TELEGRAM_WEBHOOK_URL: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    LLM_PROVIDER_INGEST: str = "anthropic"
    LLM_PROVIDER_DAILY: str = "anthropic"
    LLM_PROVIDER_WHY: str = "anthropic"
    LLM_PROVIDER_DIGEST: str = "anthropic"
    LLM_PROVIDER_ORACLE: str = "anthropic"
    LLM_PROVIDER_TWEET: str = "openai"
    LLM_PROVIDER_VISION: str = "anthropic"

    CLAUDE_MODEL_INGEST: str = "claude-sonnet-4-6"
    CLAUDE_MODEL_DIGEST: str = "claude-opus-4-7"
    CLAUDE_MODEL_CHEAP: str = "claude-haiku-4-5-20251001"
    OPENAI_MODEL_INGEST: str = "gpt-4.1-mini"
    OPENAI_MODEL_DIGEST: str = "gpt-4.1"
    OPENAI_MODEL_CHEAP: str = "gpt-4.1-nano"

    SQLITE_PATH: str = "/data/to.db"
    DOB: str = ""
    TIMEZONE: str = "UTC"
    WEEK_START: str = "mon"

    GITHUB_TOKEN: str = ""
    GITHUB_REPO: str = ""
    GITHUB_BRANCH: str = "main"

    ZYTE_API_KEY: str = ""
    EXA_API_KEY: str = ""
    # X/Twitter URLs route through Nitter (via Zyte for the Anubis PoW).
    # Comma-separated list — tried in order, first one that serves content
    # wins. Survives individual instances going down without a redeploy.
    # If all listed instances fail, run scripts/zyte_nitter_probe.py to
    # find live ones and update this env var.
    NITTER_INSTANCES: str = "nitter.tiekoetter.com,nitter.cz,nitter.net,nitter.privacydev.net"

    DAILY_PROMPT_LOCAL_TIME: str = "21:30"
    WEEKLY_DIGEST_ENABLED: bool = False
    WEEKLY_DIGEST_DOW: str = "sat"
    WEEKLY_DIGEST_LOCAL_TIME: str = "22:00"
    WHY_WINDOW_MINUTES: int = 10

    X_DAILY_ENABLED: bool = False
    X_WEEKLY_ENABLED: bool = False
    X_CONSUMER_KEY: str = ""
    X_CONSUMER_SECRET: str = ""
    X_ACCESS_TOKEN: str = ""
    X_ACCESS_TOKEN_SECRET: str = ""

    DHYAMA_BOT_TOKEN: str = ""
    DHYAMA_CHAT_ID: str = ""

    LLM_MONTHLY_USD_CAP: float = 30.0
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": str(Path(__file__).parent.parent / ".env"), "extra": "ignore"}
