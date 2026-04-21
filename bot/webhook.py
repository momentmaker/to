import hmac

from fastapi import FastAPI, Request, Response

from bot.config import Settings

app = FastAPI()

_bot_app = None
_settings: Settings | None = None


def init_webhook(bot_app, settings: Settings):
    global _bot_app, _settings
    _bot_app = bot_app
    _settings = settings


@app.post("/webhook")
async def webhook(request: Request):
    if _settings and _settings.TELEGRAM_WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret, _settings.TELEGRAM_WEBHOOK_SECRET):
            return Response(status_code=403)

    from telegram import Update
    data = await request.json()
    update = Update.de_json(data, _bot_app.bot)
    # Update.de_json returns None for unrecognized update shapes; ack Telegram
    # with 200 so it doesn't retry, but don't feed None to process_update.
    if update is None:
        return Response(status_code=200)
    await _bot_app.process_update(update)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}
