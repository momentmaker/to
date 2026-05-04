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
