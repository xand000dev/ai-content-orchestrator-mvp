"""
Core orchestration engine.

Every 2 seconds the loop:
 1. Finds Tasks in state PENDING whose depends_on steps are all DONE/APPROVED.
 2. Finds an IDLE Agent of the required type.
 3. Dispatches: sets task→running, agent→busy, fires an async coroutine.
 4. The coroutine calls OpenRouter, stores the result, and either:
      - sets task→waiting_review  (if review_required)
      - sets task→done            (otherwise, unlocking downstream tasks)
 5. Checks whether the Job is fully terminal and updates job.status.
 6. If job is done and platform is Telegram with auto_publish=True → auto-posts.

WebSocket manager lives here so routers can import it for push updates.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Set

from database import SessionLocal, Agent, Job, Platform, Task

logger = logging.getLogger(__name__)


# ─────────────────────────── WebSocket hub ───────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: list = []

    async def connect(self, ws):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)


manager = ConnectionManager()

# Set of task IDs currently being executed (prevent double-dispatch)
_running: Set[str] = set()


# ─────────────────────────── Helper functions ────────────────────────────────

def _collect_input(db, task: Task) -> dict:
    """Merge initial_input + outputs of all dependency steps."""
    job = db.query(Job).filter(Job.id == task.job_id).first()
    result: dict = json.loads(job.initial_input or "{}")

    dep_names: list = json.loads(task.depends_on or "[]")
    if not dep_names:
        return result

    dep_tasks = (
        db.query(Task)
        .filter(Task.job_id == task.job_id, Task.step_name.in_(dep_names))
        .all()
    )
    for dt in dep_tasks:
        if dt.output_data:
            result[dt.step_name] = json.loads(dt.output_data)
    return result


def _build_user_message(step_name: str, input_data: dict) -> str:
    topic    = input_data.get("topic",    "(тема не вказана)")
    keywords = input_data.get("keywords", "")
    extra    = input_data.get("extra",    "")

    lines = [f"## Завдання: {step_name}", f"**Тема:** {topic}"]
    if keywords:
        lines.append(f"**Ключові слова:** {keywords}")
    if extra:
        lines.append(f"**Додатковий контекст:** {extra}")

    prev = {k: v for k, v in input_data.items()
            if k not in ("topic", "keywords", "extra", "platform_id")}
    if prev:
        lines.append("\n## Результати попередніх кроків")
        for step, data in prev.items():
            text = data.get("result", str(data)) if isinstance(data, dict) else str(data)
            lines.append(f"\n### {step}\n{text}")

    return "\n".join(lines)


def _get_ready_tasks(db, job_id: str) -> list:
    """Return pending tasks whose every dependency is done/approved."""
    pending = (
        db.query(Task)
        .filter(Task.job_id == job_id, Task.status == "pending")
        .all()
    )
    all_tasks = db.query(Task).filter(Task.job_id == job_id).all()
    done_steps = {t.step_name for t in all_tasks if t.status in ("done", "approved")}

    return [
        t for t in pending
        if all(dep in done_steps for dep in json.loads(t.depends_on or "[]"))
    ]


def _check_job_completion(db, job_id: str) -> bool:
    """
    If all tasks are terminal, mark the job done/error.
    Returns True if job just became done.
    """
    tasks = db.query(Task).filter(Task.job_id == job_id).all()
    if not tasks:
        return False
    terminal = {"done", "approved", "error"}
    if all(t.status in terminal for t in tasks):
        job = db.query(Job).filter(Job.id == job_id).first()
        if job and job.status == "running":
            job.status = "error" if any(t.status == "error" for t in tasks) else "done"
            job.completed_at = datetime.utcnow()
            db.commit()
            logger.info("Job %s → %s", job_id[:8], job.status)
            return job.status == "done"
    return False


# ─────────────────────────── Telegram auto-post ──────────────────────────────

async def _try_telegram_autopost(job_id: str):
    """
    After a job completes, if the platform is Telegram and auto_publish=True,
    find the final content task and send it.
    """
    from telegram_bot import send_message  # local import

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        initial_input = json.loads(job.initial_input or "{}")
        platform_id   = initial_input.get("platform_id")
        if not platform_id:
            return

        platform = db.query(Platform).filter(Platform.id == platform_id).first()
        if not platform or platform.type != "telegram" or not platform.auto_publish:
            return

        creds      = json.loads(platform.credentials or "{}")
        bot_token  = creds.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        channel_id = creds.get("channel_id")
        if not channel_id:
            logger.warning("Telegram platform %s: channel_id not set", platform_id)
            return

        content = _pick_final_content(db, job_id)
        if not content:
            logger.warning("Job %s: no content to post", job_id[:8])
            return

        ok = await send_message(channel_id, content, token=bot_token or None)
        if ok:
            logger.info("✅ Auto-posted job %s to Telegram %s", job_id[:8], channel_id)
            await manager.broadcast({
                "type": "telegram_posted",
                "job_id": job_id,
                "channel": channel_id,
            })
        else:
            logger.error("❌ Failed to post job %s to Telegram", job_id[:8])

    except Exception as e:
        logger.exception("Telegram autopost failed for job %s: %s", job_id[:8], e)
    finally:
        db.close()


def _pick_final_content(db, job_id: str) -> str | None:
    """Pick the best task output to send to Telegram (publisher > writer > editor)."""
    tasks = db.query(Task).filter(Task.job_id == job_id).all()
    for preferred_type in ("publisher", "writer", "editor", "researcher"):
        for t in reversed(tasks):
            if t.agent_type == preferred_type and t.output_data:
                data = json.loads(t.output_data)
                return data.get("result", "")
    # Fallback: any task with output
    for t in reversed(tasks):
        if t.output_data:
            data = json.loads(t.output_data)
            return data.get("result", "")
    return None


# ─────────────────────────── Public helper ───────────────────────────────────

async def send_job_to_telegram(job_id: str, channel_id: str, bot_token: str | None = None) -> bool:
    """Manually send a completed job's content to Telegram. Called by router."""
    from telegram_bot import send_message

    db = SessionLocal()
    try:
        content = _pick_final_content(db, job_id)
        if not content:
            return False
        tok = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        return await send_message(channel_id, content, token=tok or None)
    finally:
        db.close()


# ─────────────────────────── Agent execution ─────────────────────────────────

async def _run_task(task_id: str):
    """Execute one agent task end-to-end."""
    from openrouter import call_openrouter

    db = SessionLocal()
    job_id = None
    try:
        task  = db.query(Task).filter(Task.id == task_id).first()
        agent = db.query(Agent).filter(Agent.id == task.agent_id).first()
        job_id = task.job_id

        task.status      = "running"
        task.started_at  = datetime.utcnow()
        db.commit()

        await manager.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "job_id": job_id,
            "step_name": task.step_name,
            "status": "running",
        })

        user_msg      = _build_user_message(task.step_name, json.loads(task.input_data or "{}"))
        system_prompt = agent.system_prompt or f"You are an expert {agent.type} AI agent."

        logger.info("⚡ Task %s | agent=%s | model=%s", task.step_name, agent.name, agent.model)

        result_text = await call_openrouter(
            model=agent.model,
            system_prompt=system_prompt,
            user_message=user_msg,
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        task.output_data   = json.dumps({"result": result_text, "step": task.step_name})
        task.completed_at  = datetime.utcnow()
        task.status        = "waiting_review" if task.review_required else "done"
        agent.status       = "idle"
        db.commit()

        logger.info("✓ Task %s → %s", task.step_name, task.status)

        await manager.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "job_id": job_id,
            "step_name": task.step_name,
            "status": task.status,
            "preview": result_text[:200],
        })

        if task.status == "done":
            just_done = _check_job_completion(db, job_id)
            if just_done:
                asyncio.create_task(_try_telegram_autopost(job_id))
                await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "done"})

    except Exception as exc:
        logger.exception("❌ Task %s failed: %s", task_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
        db = SessionLocal()
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status        = "error"
            task.error_message = str(exc)
            task.completed_at  = datetime.utcnow()
            if task.agent_id:
                ag = db.query(Agent).filter(Agent.id == task.agent_id).first()
                if ag:
                    ag.status = "idle"
            db.commit()
        await manager.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "job_id": job_id,
            "status": "error",
            "error": str(exc),
        })
    finally:
        _running.discard(task_id)
        db.close()


# ─────────────────────────── Orchestrator loop ───────────────────────────────

async def _tick():
    db = SessionLocal()
    try:
        running_jobs = db.query(Job).filter(Job.status == "running").all()

        for job in running_jobs:
            ready = _get_ready_tasks(db, job.id)

            if ready:
                for task in ready:
                    if task.id in _running:
                        continue
                    agent = (
                        db.query(Agent)
                        .filter(Agent.type == task.agent_type, Agent.status == "idle")
                        .first()
                    )
                    if not agent:
                        continue
                    task.input_data = json.dumps(_collect_input(db, task))
                    task.agent_id   = agent.id
                    task.status     = "queued"
                    agent.status    = "busy"
                    db.commit()
                    logger.info("→ Dispatch task=%s agent=%s", task.step_name, agent.name)
                    _running.add(task.id)
                    asyncio.create_task(_run_task(task.id))
            else:
                # Немає pending задач — можливо всі завершені (схвалені вручну)
                just_done = _check_job_completion(db, job.id)
                if just_done:
                    asyncio.create_task(_try_telegram_autopost(job.id))
                    await manager.broadcast({"type": "job_update", "job_id": job.id, "status": "done"})

    except Exception:
        logger.exception("Orchestrator tick error")
    finally:
        db.close()


async def orchestrator_loop():
    logger.info("🚀 Orchestrator started")
    while True:
        await _tick()
        await asyncio.sleep(2)
