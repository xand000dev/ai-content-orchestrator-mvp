"""
Telegram Bot API — відправка повідомлень у канал + Bot Control для review.

Вимоги:
  - Бот створений через @BotFather
  - TELEGRAM_BOT_TOKEN у .env
  - Бот доданий як адміністратор каналу

Bot Control (опційно):
  - TELEGRAM_ADMIN_CHAT_ID — ваш особистий Telegram ID (дізнатись: @userinfobot)
  - TELEGRAM_BOT_CONTROL=true — увімкнути отримання review-сповіщень у телефоні

Токен у .env:
  TELEGRAM_BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxx

channel_id у налаштуваннях платформи:
  "@my_channel_username"  (публічний канал)
  або "-100xxxxxxxxxx"    (числовий ID приватного каналу)
"""

import asyncio
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


# ─────────────────────── Bot Control (Review via Telegram) ───────────────────

async def send_review_notification(
    admin_chat_id: str,
    task_id: str,
    step_name: str,
    content: str,
    token: str | None = None,
) -> bool:
    """
    Надіслати адміну повідомлення з контентом + кнопками Схвалити/Відхилити.
    Виклик: коли task переходить у статус waiting_review.
    """
    tok = token or _token()
    url = f"{TG_BASE}/bot{tok}/sendMessage"

    preview = content[:900] + ("…" if len(content) > 900 else "")
    text = (
        f"🔍 <b>Review: {step_name}</b>\n\n"
        f"{preview}\n\n"
        f"<code>{task_id[:12]}…</code>"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Схвалити", "callback_data": f"approve:{task_id}"},
            {"text": "✗ Відхилити", "callback_data": f"reject:{task_id}"},
        ]]
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json={
            "chat_id": admin_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        })
        data = resp.json()
        if not data.get("ok"):
            logger.error("Review notification failed (admin=%s): %s", admin_chat_id, data.get("description"))
            return False
    logger.info("📱 Review notification sent to admin %s for task %s", admin_chat_id, task_id[:8])
    return True


async def _answer_callback(callback_query_id: str, text: str = "", token: str | None = None):
    """Відповісти на callback щоб прибрати спінер з кнопки."""
    tok = token or _token()
    url = f"{TG_BASE}/bot{tok}/answerCallbackQuery"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json={"callback_query_id": callback_query_id, "text": text})


async def _handle_callback(action: str, task_id: str, cq_id: str, tok: str):
    """Обробити approve/reject callback — вносить зміни у БД і broadcast."""
    from database import SessionLocal, Task, Agent, Job
    from orchestrator import manager, _check_job_completion, _try_telegram_autopost

    db = SessionLocal()
    try:
        t = db.query(Task).filter(Task.id == task_id).first()
        if not t or t.status != "waiting_review":
            await _answer_callback(cq_id, "❌ Задача не знайдена або вже оброблена", token=tok)
            return

        job_id = t.job_id

        if action == "approve":
            t.status = "approved"
            db.commit()
            just_done = _check_job_completion(db, job_id)
            if just_done:
                asyncio.create_task(_try_telegram_autopost(job_id))
                await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "done"})
            else:
                job = db.query(Job).filter(Job.id == job_id).first()
                if job and job.status not in ("done", "error", "stopped"):
                    job.status = "running"
                    db.commit()
            await manager.broadcast({
                "type": "task_update",
                "task_id": task_id,
                "job_id": job_id,
                "status": "approved",
            })
            await _answer_callback(cq_id, "✅ Схвалено!", token=tok)
            logger.info("✅ Task %s approved via Telegram Bot", task_id[:8])

        elif action == "reject":
            t.status = "pending"
            t.output_data = None
            t.error_message = "Відхилено через Telegram"
            if t.agent_id:
                ag = db.query(Agent).filter(Agent.id == t.agent_id).first()
                if ag and ag.status == "busy":
                    ag.status = "idle"
            db.commit()
            job = db.query(Job).filter(Job.id == job_id).first()
            if job and job.status not in ("done", "error", "stopped"):
                job.status = "running"
                db.commit()
            await manager.broadcast({
                "type": "task_update",
                "task_id": task_id,
                "job_id": job_id,
                "status": "pending",
            })
            await _answer_callback(cq_id, "✗ Відхилено, агент перезапуститься", token=tok)
            logger.info("✗ Task %s rejected via Telegram Bot", task_id[:8])

    except Exception as e:
        logger.exception("Handle callback error for task %s: %s", task_id[:8], e)
        await _answer_callback(cq_id, "❌ Внутрішня помилка", token=tok)
    finally:
        db.close()


async def start_bot_polling(admin_chat_id: str):
    """
    Довге polling Telegram для отримання callback_query (кнопки approve/reject).
    Запускається як asyncio task при старті якщо TELEGRAM_BOT_CONTROL=true.
    """
    logger.info("🤖 Telegram Bot Control активовано (admin_chat=%s)", admin_chat_id)
    offset = 0
    retry_count = 0

    while True:
        try:
            tok = _token()
            url = f"{TG_BASE}/bot{tok}/getUpdates"
            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.get(url, params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["callback_query"],
                })
            data = resp.json()

            if not data.get("ok"):
                logger.warning("getUpdates error: %s", data.get("description"))
                await asyncio.sleep(5)
                continue

            retry_count = 0  # Reset backoff on successful response
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue
                cb_data = cq.get("data", "")
                if ":" not in cb_data:
                    continue
                action, task_id = cb_data.split(":", 1)
                if action not in ("approve", "reject"):
                    continue
                asyncio.create_task(_handle_callback(action, task_id, cq["id"], tok))

        except asyncio.CancelledError:
            logger.info("Bot polling stopped")
            break
        except Exception as e:
            logger.exception("Telegram polling error: %s", e)
            sleep_time = min(2 ** retry_count, 60)
            retry_count = min(retry_count + 1, 6)
            await asyncio.sleep(sleep_time)
