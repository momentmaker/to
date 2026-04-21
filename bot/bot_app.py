from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from bot import db
from bot.config import Settings
from bot.handlers import (
    ask_handler,
    error_handler,
    export_handler,
    help_handler,
    photo_message_handler,
    reflect_handler,
    setmark_handler,
    setvow_handler,
    skip_handler,
    start_handler,
    status_handler,
    text_message_handler,
    voice_message_handler,
)
from bot.llm.router import build_providers
from bot.scheduler import build_scheduler, drain_on_boot


_VALID_PROVIDER_NAMES = {"anthropic", "openai"}
_PROVIDER_ENV_VARS = (
    "LLM_PROVIDER_INGEST", "LLM_PROVIDER_DAILY", "LLM_PROVIDER_WHY",
    "LLM_PROVIDER_DIGEST", "LLM_PROVIDER_ORACLE", "LLM_PROVIDER_TWEET",
    "LLM_PROVIDER_VISION",
)


def _validate(settings: Settings) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if settings.TELEGRAM_OWNER_ID == 0:
        raise RuntimeError("TELEGRAM_OWNER_ID is required (single-user owner gate)")
    # parse_dob raises ValueError on empty or malformed input
    db.settings_dob(settings.DOB)
    # ZoneInfo("") raises ValueError; ZoneInfo("Fake/Zone") raises
    # ZoneInfoNotFoundError. Catch both so the boot-time validation is real.
    try:
        ZoneInfo(settings.TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise RuntimeError(f"TIMEZONE={settings.TIMEZONE!r} is not a valid IANA zone") from e
    if not (settings.ANTHROPIC_API_KEY or settings.OPENAI_API_KEY):
        raise RuntimeError(
            "at least one of ANTHROPIC_API_KEY / OPENAI_API_KEY is required"
        )
    # Catch typos like LLM_PROVIDER_INGEST=claude at boot, not at first message.
    for var in _PROVIDER_ENV_VARS:
        val = getattr(settings, var)
        if val not in _VALID_PROVIDER_NAMES:
            raise RuntimeError(
                f"{var}={val!r} is not a valid provider. "
                f"Expected one of {sorted(_VALID_PROVIDER_NAMES)}."
            )


async def create_bot_app(settings: Settings):
    _validate(settings)

    app = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    conn = await db.connect(settings.SQLITE_PATH)
    providers = build_providers(settings)
    app.bot_data["db"] = conn
    app.bot_data["settings"] = settings
    app.bot_data["providers"] = providers
    app.bot_data["scheduler"] = build_scheduler(
        conn=conn, settings=settings, providers=providers, bot=app.bot,
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("skip", skip_handler))
    app.add_handler(CommandHandler("reflect", reflect_handler))
    app.add_handler(CommandHandler("setvow", setvow_handler))
    app.add_handler(CommandHandler("setmark", setmark_handler))
    app.add_handler(CommandHandler("export", export_handler))
    app.add_handler(CommandHandler("ask", ask_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_message_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message_handler))
    app.add_error_handler(error_handler)

    return app
