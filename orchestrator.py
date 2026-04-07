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

from sqlalchemy import func

from database import SessionLocal, Agent, Job, Platform, Task, safe_json

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

# ─────────────────────────── Console log broadcast ───────────────────────────

_log_buffer: list = []  # Ring buffer for recent logs (max 200)
_LOG_BUFFER_MAX = 200


async def _broadcast_log(level: str, source: str, message: str, details: dict | None = None):
    """Broadcast a structured log entry to all connected WebSocket clients."""
    entry = {
        "type": "console_log",
        "level": level,       # info | warn | error | success
        "source": source,     # orchestrator | agent | openrouter | autobot | telegram
        "message": message,
        "details": details or {},
        "ts": datetime.utcnow().strftime("%H:%M:%S"),
    }
    _log_buffer.append(entry)
    if len(_log_buffer) > _LOG_BUFFER_MAX:
        _log_buffer.pop(0)
    await manager.broadcast(entry)


def get_log_buffer() -> list:
    """Return current log buffer (called by API endpoint)."""
    return list(_log_buffer)


def _get_busy_counts(db) -> dict:
    """Return {agent_id: running_task_count} for agents with active tasks."""
    rows = (
        db.query(Task.agent_id, func.count().label("cnt"))
        .filter(Task.agent_id.isnot(None), Task.status.in_(["queued", "running"]))
        .group_by(Task.agent_id)
        .all()
    )
    return {r.agent_id: r.cnt for r in rows}


def _find_available_agent(db, agent_type: str, busy_counts: dict):
    """Find an agent of given type with remaining capacity (count < max_parallel_tasks)."""
    agents = db.query(Agent).filter(Agent.type == agent_type).all()
    # Prefer least-loaded agent first
    agents.sort(key=lambda a: busy_counts.get(a.id, 0))
    for agent in agents:
        if busy_counts.get(agent.id, 0) < agent.max_parallel_tasks:
            return agent
    return None


def _sync_agent_status(db, agent_id: str, busy_counts: dict):
    """Update agent.status to reflect actual task load."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent:
        count = busy_counts.get(agent_id, 0)
        agent.status = "busy" if count > 0 else "idle"


def _reconcile_all_agent_statuses(db):
    """Reconcile every agent's status against actual queued/running task count.

    Called at the start of every tick so agents never stay permanently 'busy'
    after a job finishes, crashes, or is stopped.
    """
    busy_counts = _get_busy_counts(db)
    agents = db.query(Agent).filter(Agent.status.in_(["idle", "busy"])).all()
    changed = False
    for agent in agents:
        expected = "busy" if busy_counts.get(agent.id, 0) > 0 else "idle"
        if agent.status != expected:
            agent.status = expected
            changed = True
    if changed:
        db.commit()


# Tasks running longer than this are considered stuck (coroutine died silently)
_TASK_WATCHDOG_MINUTES = 10


def _watchdog_stuck_tasks(db):
    """Reset tasks stuck in running/queued that are no longer in the _running set.

    A task can get stuck when:
    - Server was restarted (startup handles this, but watchdog is a safety net)
    - An asyncio coroutine was cancelled without updating task status
    - A network partition killed the connection without raising an exception
    """
    cutoff = datetime.utcnow() - timedelta(minutes=_TASK_WATCHDOG_MINUTES)

    stuck = (
        db.query(Task)
        .filter(
            Task.status.in_(["running", "queued"]),
            Task.started_at < cutoff,
            Task.id.notin_(list(_running)) if _running else Task.status.in_(["running", "queued"]),
        )
        .all()
    )
    # Also catch queued tasks with no started_at that have been waiting too long
    queued_no_start = (
        db.query(Task)
        .filter(
            Task.status == "queued",
            Task.started_at.is_(None),
            Task.created_at < cutoff,
            Task.id.notin_(list(_running)) if _running else Task.status == "queued",
        )
        .all()
    )

    all_stuck = {t.id: t for t in stuck + queued_no_start}.values()
    reset_count = 0
    for task in all_stuck:
        if task.id in _running:
            continue  # genuinely running in this process — leave it alone
        logger.warning(
            "⚠️  Watchdog: reset stuck task '%s' (%s) status=%s started=%s",
            task.step_name, task.id[:8], task.status, task.started_at,
        )
        task.status = "pending"
        task.started_at = None
        task.error_message = f"Auto-reset by watchdog after >{_TASK_WATCHDOG_MINUTES}min"
        if task.agent_id:
            ag = db.query(Agent).filter(Agent.id == task.agent_id).first()
            if ag:
                ag.status = "idle"
        reset_count += 1

    if reset_count:
        db.commit()
        asyncio.create_task(_broadcast_log(
            "warn", "orchestrator",
            f"⚠️  Watchdog: скинув {reset_count} завислих задач → pending",
            {"count": reset_count},
        ))


# ─────────────────────────── Helper functions ────────────────────────────────

def _collect_input(db, task: Task) -> dict:
    """Merge initial_input + outputs of all dependency steps."""
    job = db.query(Job).filter(Job.id == task.job_id).first()
    result: dict = safe_json(job.initial_input, {})

    dep_names: list = safe_json(task.depends_on, [])
    if not dep_names:
        return result

    dep_tasks = (
        db.query(Task)
        .filter(Task.job_id == task.job_id, Task.step_name.in_(dep_names))
        .all()
    )
    for dt in dep_tasks:
        if dt.output_data:
            result[dt.step_name] = safe_json(dt.output_data, {})
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
        if all(dep in done_steps for dep in safe_json(t.depends_on, []))
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
            topic = safe_json(job.initial_input, {}).get("topic", "?")[:50]
            asyncio.create_task(_broadcast_log(
                "success" if job.status == "done" else "error",
                "orchestrator",
                f"Job завершено → {job.status}: {topic}",
                {"job_id": job_id[:8]},
            ))
            just_done = job.status == "done"
            if just_done:
                from obsidian_writer import get_writer
                get_writer().write_job_note(db, job_id)
            return just_done
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

        initial_input = safe_json(job.initial_input, {})
        platform_id   = initial_input.get("platform_id")
        if not platform_id:
            return

        platform = db.query(Platform).filter(Platform.id == platform_id).first()
        if not platform or platform.type != "telegram" or not platform.auto_publish:
            return

        creds      = safe_json(platform.credentials, {})
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
            from obsidian_writer import get_writer
            get_writer().write_published_note(job_id, channel_id, content)
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
                return safe_json(t.output_data, {}).get("result", "")
    # Fallback: any task with output
    for t in reversed(tasks):
        if t.output_data:
            return safe_json(t.output_data, {}).get("result", "")
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
    from openrouter import call_openrouter, OpenRouterError

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

        user_msg      = _build_user_message(task.step_name, safe_json(task.input_data, {}))
        system_prompt = agent.system_prompt or f"You are an expert {agent.type} AI agent."

        logger.info("⚡ Task %s | agent=%s | model=%s", task.step_name, agent.name, agent.model)
        await _broadcast_log(
            "info", "agent",
            f"⚡ {agent.name} розпочав «{task.step_name}»",
            {"model": agent.model, "task_id": task_id[:8], "job_id": job_id[:8]},
        )

        await _broadcast_log(
            "info", "openrouter",
            f"→ API запит: {agent.model.split('/')[-1]}",
            {"model": agent.model, "step": task.step_name, "prompt_len": len(user_msg)},
        )

        result_text = await call_openrouter(
            model=agent.model,
            system_prompt=system_prompt,
            user_message=user_msg,
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

        await _broadcast_log(
            "success", "openrouter",
            f"← Відповідь: {agent.model.split('/')[-1]} ({len(result_text)} символів)",
            {"model": agent.model, "step": task.step_name, "response_len": len(result_text)},
        )

        task.output_data   = json.dumps({"result": result_text, "step": task.step_name})
        task.completed_at  = datetime.utcnow()
        task.status        = "waiting_review" if task.review_required else "done"
        # Set agent status based on remaining active tasks (not blindly "idle")
        remaining = db.query(Task).filter(
            Task.agent_id == agent.id,
            Task.status.in_(["queued", "running"]),
        ).count()
        agent.status = "busy" if remaining > 0 else "idle"
        db.commit()

        logger.info("✓ Task %s → %s", task.step_name, task.status)
        await _broadcast_log(
            "success", "agent",
            f"✓ {agent.name} завершив «{task.step_name}» → {task.status}",
            {"task_id": task_id[:8], "job_id": job_id[:8], "status": task.status},
        )

        await manager.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "job_id": job_id,
            "step_name": task.step_name,
            "status": task.status,
            "preview": result_text[:200],
        })

        # Telegram Bot Control — надіслати review-сповіщення адміну
        if task.status == "waiting_review":
            admin_chat = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
            bot_control = os.getenv("TELEGRAM_BOT_CONTROL", "").lower() in ("true", "1", "yes")
            if admin_chat and bot_control:
                from telegram_bot import send_review_notification
                asyncio.create_task(send_review_notification(
                    admin_chat, task_id, task.step_name, result_text
                ))

        if task.status == "done":
            just_done = _check_job_completion(db, job_id)
            if just_done:
                asyncio.create_task(_try_telegram_autopost(job_id))
                await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "done"})

    except OpenRouterError as exc:
        err_msg = f"[{exc.model or 'unknown'}] {exc}"
        await _broadcast_log(
            "error", "openrouter",
            f"❌ OpenRouter помилка: {err_msg[:120]}",
            {"task_id": task_id[:8], "model": exc.model or "?"},
        )
        logger.error("❌ Task %s OpenRouter error: %s", task_id, err_msg)
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
        db = SessionLocal()
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status        = "error"
            task.error_message = err_msg
            task.completed_at  = datetime.utcnow()
            if task.agent_id:
                ag = db.query(Agent).filter(Agent.id == task.agent_id).first()
                if ag:
                    remaining = db.query(Task).filter(
                        Task.agent_id == ag.id,
                        Task.status.in_(["queued", "running"]),
                    ).count()
                    ag.status = "busy" if remaining > 0 else "idle"
            db.commit()
        await manager.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "job_id": job_id,
            "status": "error",
            "error": err_msg,
        })

    except Exception as exc:
        logger.exception("❌ Task %s failed: %s", task_id, exc)
        await _broadcast_log(
            "error", "agent",
            f"❌ Задача впала: {str(exc)[:120]}",
            {"task_id": task_id[:8]},
        )
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
                    remaining = db.query(Task).filter(
                        Task.agent_id == ag.id,
                        Task.status.in_(["queued", "running"]),
                    ).count()
                    ag.status = "busy" if remaining > 0 else "idle"
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
        # Always reconcile agent statuses — fixes stuck 'busy' after crash/stop
        _reconcile_all_agent_statuses(db)

        running_jobs = db.query(Job).filter(Job.status == "running").all()
        if not running_jobs:
            return

        # Pre-compute running task count per agent once per tick
        busy_counts = _get_busy_counts(db)

        for job in running_jobs:
            ready = _get_ready_tasks(db, job.id)

            if ready:
                for task in ready:
                    if task.id in _running:
                        continue
                    agent = _find_available_agent(db, task.agent_type, busy_counts)
                    if not agent:
                        continue
                    # Update in-tick counter so next task in same loop sees correct load
                    busy_counts[agent.id] = busy_counts.get(agent.id, 0) + 1

                    task.input_data = json.dumps(_collect_input(db, task))
                    task.agent_id   = agent.id
                    task.status     = "queued"
                    agent.status    = "busy"
                    db.commit()
                    asyncio.create_task(_broadcast_log(
                        "info", "orchestrator",
                        f"→ Dispatch «{task.step_name}» → {agent.name} ({busy_counts[agent.id]}/{agent.max_parallel_tasks})",
                        {"agent": agent.name, "step": task.step_name},
                    ))
                    logger.info("→ Dispatch task=%s agent=%s (load %d/%d)",
                                task.step_name, agent.name,
                                busy_counts[agent.id], agent.max_parallel_tasks)
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


# ─────────────────────────── Scheduler ──────────────────────────────────────

async def _fire_schedule(db, sched) -> "Job | None":
    """Створити і запустити Job з розкладу. Повертає Job або None."""
    from database import Pipeline, Task, Job as JobModel, new_id as _new_id

    pipeline = db.query(Pipeline).filter(Pipeline.id == sched.pipeline_id).first()
    if not pipeline:
        logger.warning("Schedule %s: pipeline %s not found", sched.name, sched.pipeline_id[:8])
        return None

    steps = json.loads(pipeline.steps or "[]")
    if not steps:
        logger.warning("Schedule %s: pipeline has no steps", sched.name)
        return None

    # Замінити плейсхолдери у темі
    now = datetime.utcnow()
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    topic = (sched.topic_template or f"Автопост {now.strftime('%d.%m.%Y')}")
    topic = topic.replace("{date}", now.strftime("%d.%m.%Y"))
    topic = topic.replace("{weekday}", weekdays[now.weekday()])
    topic = topic.replace("{month}", now.strftime("%B"))

    initial_input = json.dumps({
        "topic": topic,
        "keywords": sched.keywords or "",
        "platform_id": sched.platform_id or "",
        "extra": sched.extra or "",
        "schedule_id": sched.id,
    })

    job = JobModel(
        id=_new_id(),
        pipeline_id=sched.pipeline_id,
        status="running",
        initial_input=initial_input,
        started_at=now,
    )
    db.add(job)

    for step in steps:
        task = Task(
            id=_new_id(),
            job_id=job.id,
            step_name=step["step_name"],
            agent_type=step["agent_type"],
            depends_on=json.dumps(step.get("depends_on", [])),
            review_required=step.get("review_required", False),
            status="pending",
        )
        db.add(task)

    db.commit()
    logger.info("⏰ Schedule '%s' fired → Job %s | topic: %s", sched.name, job.id[:8], topic[:50])
    return job


async def _tick_schedules():
    """Перевірити розклади і запустити прострочені. Викликається раз на хвилину."""
    from database import Schedule
    try:
        from croniter import croniter
    except ImportError:
        logger.warning("croniter не встановлено — pip install croniter")
        return

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        active = db.query(Schedule).filter(Schedule.active == True).all()
        for sched in active:
            # Якщо next_run ще не прораховано — рахуємо
            if not sched.next_run:
                sched.next_run = croniter(sched.cron_expr, now).get_next(datetime)
                db.commit()
                continue

            if sched.next_run <= now:
                job = await _fire_schedule(db, sched)
                sched.last_run = now
                sched.next_run = croniter(sched.cron_expr, now).get_next(datetime)
                db.commit()

                if job:
                    await manager.broadcast({
                        "type": "schedule_fired",
                        "schedule_id": sched.id,
                        "schedule_name": sched.name,
                        "job_id": job.id,
                    })
                    await manager.broadcast({"type": "job_update", "job_id": job.id, "status": "running"})

    except Exception:
        logger.exception("Schedule tick error")
    finally:
        db.close()


async def orchestrator_loop():
    logger.info("🚀 Orchestrator started")
    tick_count = 0
    while True:
        await _tick()
        tick_count += 1
        # Кожні 30 тіків = ~60 секунд → перевіряємо розклади
        if tick_count >= 30:
            await _tick_schedules()
            from auto_manager import tick_auto_managers
            await tick_auto_managers()
            # Watchdog: reset tasks stuck >10min that aren't actually running
            _wd_db = SessionLocal()
            try:
                _watchdog_stuck_tasks(_wd_db)
            finally:
                _wd_db.close()
            tick_count = 0
        await asyncio.sleep(2)
