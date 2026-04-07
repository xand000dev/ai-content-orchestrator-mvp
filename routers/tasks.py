import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Task, Agent, Job
from orchestrator import manager, _check_job_completion, _try_telegram_autopost

router = APIRouter(prefix="/tasks", tags=["tasks"])


class ReviewAction(BaseModel):
    comment: str = ""


def _serialize(t: Task, db: Session) -> dict:
    agent = db.query(Agent).filter(Agent.id == t.agent_id).first() if t.agent_id else None
    return {
        "id": t.id,
        "job_id": t.job_id,
        "step_name": t.step_name,
        "agent_type": t.agent_type,
        "agent_name": agent.name if agent else None,
        "agent_model": agent.model if agent else None,
        "status": t.status,
        "input_data": json.loads(t.input_data or "{}"),
        "output_data": json.loads(t.output_data) if t.output_data else None,
        "depends_on": json.loads(t.depends_on or "[]"),
        "review_required": t.review_required,
        "reviewer_comment": t.reviewer_comment,
        "error_message": t.error_message,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


@router.get("")
def list_tasks(
    job_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(Task)
    if job_id:
        q = q.filter(Task.job_id == job_id)
    if agent_id:
        q = q.filter(Task.agent_id == agent_id)
    return [_serialize(t, db) for t in q.order_by(Task.created_at.desc()).limit(limit).all()]


@router.get("/review")
def list_review_tasks(db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.status == "waiting_review").all()
    return [_serialize(t, db) for t in tasks]


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)):
    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    return _serialize(t, db)


@router.post("/{task_id}/approve")
async def approve_task(task_id: str, body: ReviewAction, db: Session = Depends(get_db)):
    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status != "waiting_review":
        raise HTTPException(400, f"Task is '{t.status}', not 'waiting_review'")

    job_id = t.job_id
    t.status = "approved"
    t.reviewer_comment = body.comment
    db.commit()

    # Перевіряємо чи це був останній крок — якщо так, закриваємо job
    just_done = _check_job_completion(db, job_id)

    if just_done:
        # Job завершено → авто-постинг в Telegram (якщо налаштовано)
        asyncio.create_task(_try_telegram_autopost(job_id))
        await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "done"})
    else:
        # Є downstream tasks — реактивуємо job для оркестратора
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
    return _serialize(t, db)


@router.post("/{task_id}/reject")
async def reject_task(task_id: str, body: ReviewAction, db: Session = Depends(get_db)):
    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status != "waiting_review":
        raise HTTPException(400, f"Task is '{t.status}', not 'waiting_review'")

    job_id = t.job_id
    # Скидаємо в pending → агент виконає ще раз
    t.status = "pending"
    t.output_data = None
    t.error_message = f"Відхилено: {body.comment}" if body.comment else "Відхилено"
    t.reviewer_comment = body.comment
    if t.agent_id:
        ag = db.query(Agent).filter(Agent.id == t.agent_id).first()
        if ag and ag.status == "busy":
            ag.status = "idle"
    db.commit()

    # Реактивуємо job
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
    return _serialize(t, db)


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, db: Session = Depends(get_db)):
    """Reset an errored task back to pending so it gets re-dispatched."""
    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status != "error":
        raise HTTPException(400, f"Task is '{t.status}', not 'error'")

    job_id = t.job_id
    t.status = "pending"
    t.output_data = None
    t.error_message = None
    t.started_at = None
    t.completed_at = None
    if t.agent_id:
        ag = db.query(Agent).filter(Agent.id == t.agent_id).first()
        if ag:
            ag.status = "idle"
    db.commit()

    job = db.query(Job).filter(Job.id == job_id).first()
    if job and job.status in ("error", "stopped"):
        job.status = "running"
        job.completed_at = None
        db.commit()

    await manager.broadcast({"type": "task_update", "task_id": task_id, "job_id": job_id, "status": "pending"})
    await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "running"})
    return _serialize(t, db)


@router.post("/bulk-approve")
async def bulk_approve(db: Session = Depends(get_db)):
    """Approve all tasks currently waiting for review."""
    tasks = db.query(Task).filter(Task.status == "waiting_review").all()
    if not tasks:
        return {"approved": 0}

    approved_jobs = set()
    for t in tasks:
        t.status = "approved"
        approved_jobs.add(t.job_id)
    db.commit()

    just_done_jobs = []
    for job_id in approved_jobs:
        if _check_job_completion(db, job_id):
            just_done_jobs.append(job_id)
            asyncio.create_task(_try_telegram_autopost(job_id))
            await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "done"})
        else:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job and job.status not in ("done", "error", "stopped"):
                job.status = "running"
                db.commit()
            await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "running"})

    for t in tasks:
        await manager.broadcast({"type": "task_update", "task_id": t.id, "job_id": t.job_id, "status": "approved"})

    return {"approved": len(tasks), "jobs_completed": just_done_jobs}
