import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Task, Agent, Job
from orchestrator import manager

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

    t.status = "approved"
    t.reviewer_comment = body.comment
    db.commit()

    # Make sure the parent job is still running so orchestrator picks up downstream tasks
    job = db.query(Job).filter(Job.id == t.job_id).first()
    if job and job.status not in ("done", "error", "stopped"):
        job.status = "running"
        db.commit()

    await manager.broadcast({
        "type": "task_update",
        "task_id": task_id,
        "job_id": t.job_id,
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

    t.status = "rejected"
    t.reviewer_comment = body.comment
    # Reset to pending so it can be re-dispatched
    t.status = "pending"
    t.output_data = None
    t.error_message = f"Rejected: {body.comment}"
    if t.agent_id:
        ag = db.query(Agent).filter(Agent.id == t.agent_id).first()
        if ag and ag.status == "busy":
            ag.status = "idle"
    db.commit()

    job = db.query(Job).filter(Job.id == t.job_id).first()
    if job and job.status not in ("done", "error", "stopped"):
        job.status = "running"
        db.commit()

    await manager.broadcast({
        "type": "task_update",
        "task_id": task_id,
        "job_id": t.job_id,
        "status": "pending",
    })
    return _serialize(t, db)
