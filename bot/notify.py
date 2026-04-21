import logging
import os

import httpx

log = logging.getLogger(__name__)

DHYAMA_TOKEN = os.environ.get("DHYAMA_BOT_TOKEN", "")
DHYAMA_CHAT_ID = os.environ.get("DHYAMA_CHAT_ID", "")
PROJECT = "to"

SEVERITY_ICON = {
    "critical": "\U0001F534",
    "warning": "\U0001F7E1",
    "info": "\U0001F7E2",
    "digest": "\U0001F4CA",
}


async def send_alert(message: str, severity: str = "info"):
    if not DHYAMA_TOKEN or not DHYAMA_CHAT_ID:
        log.warning("dhyama not configured, skipping alert")
        return

    icon = SEVERITY_ICON.get(severity, "ℹ️")
    text = f"{icon} <b>[{PROJECT}]</b> {message}"

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{DHYAMA_TOKEN}/sendMessage",
                json={
                    "chat_id": int(DHYAMA_CHAT_ID),
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
    except Exception as e:
        log.warning("Failed to send dhyama alert: %s", e)
