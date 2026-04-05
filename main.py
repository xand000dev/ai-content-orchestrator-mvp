import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from database import Base, engine
from orchestrator import manager, orchestrator_loop
from routers import agents, jobs, pipelines, platforms, tasks, schedules, strategies, auto_managers

logging.basicConfig(level=logging.INFO)

# Create all tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(title="AI Content Orchestrator", version="0.1.0")

# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(agents.router, prefix="/api")
app.include_router(pipelines.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(platforms.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")
app.include_router(strategies.router, prefix="/api")
app.include_router(auto_managers.router, prefix="/api")

# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/telegram/test")
async def telegram_test(channel_id: str, text: str = "🤖 Тест підключення від AI Orchestrator — все працює!"):
    """Відправити тестове повідомлення. Використовує TELEGRAM_BOT_TOKEN з .env"""
    from telegram_bot import send_message, test_connection
    conn = await test_connection(channel_id)
    if not conn["ok"]:
        return {"ok": False, "error": conn.get("error"), "tip": "Перевір TELEGRAM_BOT_TOKEN у .env"}
    ok = await send_message(channel_id, text)
    return {
        "ok": ok,
        "bot": conn.get("bot_name"),
        "channel": channel_id,
        "error": None if ok else "Не вдалось надіслати. Перевір чи бот є адміністратором каналу.",
    }


@app.post("/api/telegram/send-content")
async def send_content_direct(channel_id: str, job_id: str):
    """Надіслати контент конкретної задачі напряму в канал (для ручного тесту)."""
    from orchestrator import send_job_to_telegram
    ok = await send_job_to_telegram(job_id, channel_id)
    return {"ok": ok, "channel": channel_id, "job_id": job_id}


@app.get("/api/obsidian/status")
async def obsidian_status():
    """Статус інтеграції з Obsidian."""
    from obsidian_writer import get_writer
    writer = get_writer()
    vault  = os.getenv("OBSIDIAN_VAULT_PATH", "")
    notes  = 0
    if writer.enabled and writer.base:
        notes = sum(1 for _ in writer.base.rglob("*.md"))
    return {
        "enabled":    writer.enabled,
        "vault_path": vault or None,
        "notes_count": notes,
        "tip": None if writer.enabled else "Додай OBSIDIAN_VAULT_PATH=C:/path/to/vault у .env",
    }


@app.post("/api/obsidian/sync-all")
async def obsidian_sync_all():
    """Синхронізувати всі завершені jobs у Obsidian vault."""
    from obsidian_writer import get_writer
    from database import SessionLocal
    writer = get_writer()
    if not writer.enabled:
        return {"ok": False, "error": "OBSIDIAN_VAULT_PATH не вказано"}
    db = SessionLocal()
    try:
        count = writer.sync_all_jobs(db)
    finally:
        db.close()
    return {"ok": True, "synced": count}


@app.get("/api/bot-control/status")
async def bot_control_status():
    """Статус Telegram Bot Control."""
    admin_chat = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    bot_control = os.getenv("TELEGRAM_BOT_CONTROL", "").lower() in ("true", "1", "yes")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    active = bool(admin_chat and bot_control and tg_token)
    return {
        "active": active,
        "admin_chat_id": admin_chat or None,
        "bot_control_env": bot_control,
        "token_set": bool(tg_token),
        "tip": None if active else "Додай TELEGRAM_ADMIN_CHAT_ID та TELEGRAM_BOT_CONTROL=true у .env",
    }


@app.get("/api/health")
async def health():
    """Перевірка конфігурації. Відкрий у браузері якщо щось не працює."""
    or_key   = os.getenv("OPENROUTER_API_KEY", "")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return {
        "openrouter_key": "✅ встановлено" if or_key else "❌ НЕ встановлено (задачі не виконуватимуться!)",
        "telegram_token": "✅ встановлено" if tg_token else "⚠️  не встановлено (Telegram постинг не працюватиме)",
        "tip": "Скопіюй .env.example → .env і заповни ключі",
    }


@app.get("/api/logs/recent")
async def logs_recent():
    """Повернути останні записи логу консолі."""
    from orchestrator import get_log_buffer
    return get_log_buffer()


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; actual messages come from broadcasts
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── Lifecycle ─────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # One-time migration: bump agents still at legacy default of 1 parallel task
    from database import SessionLocal as _SL, Agent as _Agent
    _db = _SL()
    try:
        updated = _db.query(_Agent).filter(_Agent.max_parallel_tasks == 1).update(
            {"max_parallel_tasks": 3}, synchronize_session=False
        )
        _db.commit()
        if updated:
            logging.getLogger(__name__).info("⚙️  Upgraded %d agent(s) to max_parallel_tasks=3", updated)
    finally:
        _db.close()

    asyncio.create_task(orchestrator_loop())
    # Telegram Bot Control — polling для approve/reject прямо з телефону
    admin_chat = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    bot_control = os.getenv("TELEGRAM_BOT_CONTROL", "").lower() in ("true", "1", "yes")
    if admin_chat and bot_control:
        from telegram_bot import start_bot_polling
        asyncio.create_task(start_bot_polling(admin_chat))


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
