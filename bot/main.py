import asyncio
import logging
import signal

from bot.bot_app import create_bot_app
from bot.config import Settings

settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


async def _close_db(app):
    conn = app.bot_data.get("db")
    if conn is not None:
        try:
            await conn.close()
        except Exception:
            log.exception("failed to close db connection")


async def _start_scheduler(app):
    """Start the APScheduler and run a boot-time drain once to catch up from
    any crash/shutdown that left captures pending or unsynced.
    """
    scheduler = app.bot_data.get("scheduler")
    if scheduler is None:
        return
    try:
        scheduler.start()
        log.info("scheduler started")
    except Exception:
        log.exception("scheduler failed to start")
        return
    from bot.scheduler import drain_on_boot
    await drain_on_boot(
        conn=app.bot_data["db"],
        settings=app.bot_data["settings"],
        providers=app.bot_data["providers"],
        bot=app.bot,
    )


async def _stop_scheduler(app):
    scheduler = app.bot_data.get("scheduler")
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        log.exception("scheduler shutdown failed")


async def run_polling():
    app = await create_bot_app(settings)
    try:
        async with app:
            await app.start()
            log.info("bot polling")
            await app.updater.start_polling()
            await _start_scheduler(app)

            try:
                from bot.notify import send_alert
                await send_alert("bot started (polling)", severity="info")
            except Exception:
                pass

            stop = asyncio.Event()
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()

            log.info("shutting down")
            await _stop_scheduler(app)
            await app.updater.stop()
            await app.stop()
    finally:
        await _close_db(app)


async def run_webhook():
    import uvicorn
    from bot.webhook import app as fastapi_app, init_webhook

    bot_app = await create_bot_app(settings)
    init_webhook(bot_app, settings)

    try:
        async with bot_app:
            await bot_app.start()
            await _start_scheduler(bot_app)

            if settings.TELEGRAM_WEBHOOK_URL:
                await bot_app.bot.set_webhook(
                    url=settings.TELEGRAM_WEBHOOK_URL,
                    secret_token=settings.TELEGRAM_WEBHOOK_SECRET or None,
                )
                log.info("webhook set: %s", settings.TELEGRAM_WEBHOOK_URL)

            config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()

            await _stop_scheduler(bot_app)
            await bot_app.stop()
    finally:
        await _close_db(bot_app)


def main():
    if settings.MODE == "webhook":
        log.info("webhook mode")
        asyncio.run(run_webhook())
    else:
        log.info("polling mode")
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
