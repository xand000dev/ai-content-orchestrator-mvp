"""
Telegram Bot API — відправка повідомлень у канал.

Вимоги:
  - Бот створений через @BotFather
  - TELEGRAM_BOT_TOKEN у .env
  - Бот доданий як адміністратор каналу

Токен у .env:
  TELEGRAM_BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxx

channel_id у налаштуваннях платформи:
  "@my_channel_username"  (публічний канал)
  або "-100xxxxxxxxxx"    (числовий ID приватного каналу)
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)

TG_BASE = "https://api.telegram.org"


def _token() -> str:
    t = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not t:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN не встановлено. "
            "Скопіюй .env.example → .env і вкажи токен від @BotFather."
        )
    return t


async def send_message(channel_id: str, text: str, token: str | None = None) -> bool:
    """Надіслати текстове повідомлення у Telegram канал/чат."""
    tok = token or _token()
    url = f"{TG_BASE}/bot{tok}/sendMessage"

    # Telegram обмежує одне повідомлення до 4096 символів
    chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            resp = await client.post(
                url,
                json={"chat_id": channel_id, "text": chunk, "parse_mode": "HTML"},
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error(
                    "Telegram помилка (channel=%s): %s", channel_id, data.get("description")
                )
                return False
    return True


async def test_connection(channel_id: str, token: str | None = None) -> dict:
    """Перевірити підключення до Telegram без надсилання реального контенту."""
    try:
        tok = token or _token()
        url = f"{TG_BASE}/bot{tok}/getMe"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("ok"):
                bot = data["result"]
                return {
                    "ok": True,
                    "bot_name": bot.get("username"),
                    "bot_id": bot.get("id"),
                }
            return {"ok": False, "error": data.get("description")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
