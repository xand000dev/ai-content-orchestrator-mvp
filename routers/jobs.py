import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Job, Task, Pipeline, Platform, new_id
from orchestrator import manager, send_job_to_telegram

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    pipeline_id: str
    topic: str
    keywords: str = ""
    platform_id: str = ""
    extra: str = ""


def _task_status_counts(db: Session, job_id: str) -> dict:
    tasks = db.query(Task).filter(Task.job_id == job_id).all()
    total = len(tasks)
    done = sum(1 for t in tasks if t.status in ("done", "approved"))
    reviewing = sum(1 for t in tasks if t.status == "waiting_review")
    running = sum(1 for t in tasks if t.status == "running")
    return {"total": total, "done": done, "reviewing": reviewing, "running": running}


def _serialize(j: Job, db: Session) -> dict:
    pipeline = db.query(Pipeline).filter(Pipeline.id == j.pipeline_id).first()
    counts = _task_status_counts(db, j.id)
    return {
        "id": j.id,
        "pipeline_id": j.pipeline_id,
        "pipeline_name": pipeline.name if pipeline else "?",
        "status": j.status,
        "initial_input": json.loads(j.initial_input or "{}"),
        "task_counts": counts,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }


@router.get("")
def list_jobs(db: Session = Depends(get_db)):
    jobs = db.query(Job).order_by(Job.created_at.desc()).all()
    return [_serialize(j, db) for j in jobs]


@router.post("", status_code=201)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    pipeline = db.query(Pipeline).filter(Pipeline.id == body.pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    initial_input = {
        "topic": body.topic,
        "keywords": body.keywords,
        "platform_id": body.platform_id,
        "extra": body.extra,
    }
    job = Job(
        id=new_id(),
        pipeline_id=body.pipeline_id,
        status="pending",
        initial_input=json.dumps(initial_input),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _serialize(job, db)


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    j = db.query(Job).filter(Job.id == job_id).first()
    if not j:
        raise HTTPException(404, "Job not found")
    return _serialize(j, db)


@router.post("/{job_id}/start")
async def start_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ("pending", "error"):
        raise HTTPException(400, f"Cannot start a job in status '{job.status}'")

    pipeline = db.query(Pipeline).filter(Pipeline.id == job.pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    steps = json.loads(pipeline.steps or "[]")
    if not steps:
        raise HTTPException(400, "Pipeline has no steps")

    # Remove existing tasks if restarting from error
    db.query(Task).filter(Task.job_id == job_id).delete()

    for step in steps:
        task = Task(
            id=new_id(),
            job_id=job.id,
            step_name=step["step_name"],
            agent_type=step["agent_type"],
            depends_on=json.dumps(step.get("depends_on", [])),
            review_required=step.get("review_required", False),
            status="pending",
        )
        db.add(task)

    job.status = "running"
    job.started_at = datetime.utcnow()
    job.completed_at = None
    db.commit()

    await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "running"})
    return _serialize(job, db)


@router.post("/{job_id}/stop")
async def stop_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    # Mark pending/queued/running tasks as error
    tasks = db.query(Task).filter(
        Task.job_id == job_id,
        Task.status.in_(["pending", "queued", "running"]),
    ).all()
    for t in tasks:
        t.status = "error"
        t.error_message = "Stopped by user"

    job.status = "stopped"
    job.completed_at = datetime.utcnow()
    db.commit()

    await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "stopped"})
    return {"ok": True}


@router.post("/{job_id}/send-telegram")
async def send_telegram(job_id: str, db: Session = Depends(get_db)):
    """Вручну відправити готовий контент у Telegram канал платформи."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "done":
        raise HTTPException(400, f"Job ще не завершено (status={job.status})")

    initial_input = json.loads(job.initial_input or "{}")
    platform_id   = initial_input.get("platform_id")
    if not platform_id:
        raise HTTPException(400, "Для цієї задачі платформа не вказана")

    platform = db.query(Platform).filter(Platform.id == platform_id).first()
    if not platform or platform.type != "telegram":
        raise HTTPException(400, "Платформа не є Telegram або не знайдена")

    creds      = json.loads(platform.credentials or "{}")
    bot_token  = creds.get("bot_token") or None
    channel_id = creds.get("channel_id")
    if not channel_id:
        raise HTTPException(400, "channel_id не вказано у налаштуваннях платформи")

    ok = await send_job_to_telegram(job_id, channel_id, bot_token)
    if not ok:
        raise HTTPException(500, "Не вдалося відправити повідомлення у Telegram. Перевір токен та права бота.")

    await manager.broadcast({"type": "telegram_posted", "job_id": job_id, "channel": channel_id})
    return {"ok": True, "channel": channel_id}


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    db.query(Task).filter(Task.job_id == job_id).delete()
    db.delete(job)
    db.commit()
