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
from routers import agents, jobs, pipelines, platforms, tasks

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

# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


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
    asyncio.create_task(orchestrator_loop())


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
