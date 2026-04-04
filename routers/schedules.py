import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Schedule, Pipeline, Platform, new_id

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str
    pipeline_id: str
    platform_id: str = ""
    cron_expr: str = "0 9 * * *"
    topic_template: str = ""
    keywords: str = ""
    extra: str = ""
    active: bool = True


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    pipeline_id: Optional[str] = None
    platform_id: Optional[str] = None
    cron_expr: Optional[str] = None
    topic_template: Optional[str] = None
    keywords: Optional[str] = None
    extra: Optional[str] = None
    active: Optional[bool] = None


def _calc_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    try:
        from croniter import croniter
        base = after or datetime.utcnow()
        return croniter(cron_expr, base).get_next(datetime)
    except Exception:
        return None


def _serialize(s: Schedule, db: Session) -> dict:
    pipeline = db.query(Pipeline).filter(Pipeline.id == s.pipeline_id).first()
    platform = db.query(Platform).filter(Platform.id == s.platform_id).first() if s.platform_id else None
    return {
        "id": s.id,
        "name": s.name,
        "pipeline_id": s.pipeline_id,
        "pipeline_name": pipeline.name if pipeline else "?",
        "platform_id": s.platform_id,
        "platform_name": platform.name if platform else None,
        "cron_expr": s.cron_expr,
        "cron_human": _cron_human(s.cron_expr),
        "topic_template": s.topic_template,
        "keywords": s.keywords,
        "extra": s.extra,
        "active": s.active,
        "last_run": s.last_run.isoformat() if s.last_run else None,
        "next_run": s.next_run.isoformat() if s.next_run else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _cron_human(expr: str) -> str:
    """Повернути читабельний опис cron-виразу."""
    presets = {
        "0 9 * * *":     "Щодня о 9:00",
        "0 18 * * *":    "Щодня о 18:00",
        "0 12 * * *":    "Щодня о 12:00",
        "0 8 * * *":     "Щодня о 8:00",
        "0 9 * * 1":     "Щопонеділка о 9:00",
        "0 9 * * 1,4":   "Пн і Чт о 9:00",
        "0 9 * * 1,3,5": "Пн, Ср, Пт о 9:00",
        "0 10 * * 1-5":  "Щодня (пн–пт) о 10:00",
        "0 12 * * 6":    "Щосуботи о 12:00",
        "0 */2 * * *":   "Кожні 2 години",
        "*/30 * * * *":  "Кожні 30 хвилин",
        "* * * * *":     "Щохвилини (тест)",
    }
    return presets.get(expr, expr)


@router.get("")
def list_schedules(db: Session = Depends(get_db)):
    return [_serialize(s, db) for s in
            db.query(Schedule).order_by(Schedule.created_at.desc()).all()]


@router.post("", status_code=201)
def create_schedule(body: ScheduleCreate, db: Session = Depends(get_db)):
    if not db.query(Pipeline).filter(Pipeline.id == body.pipeline_id).first():
        raise HTTPException(404, "Pipeline not found")
    s = Schedule(
        id=new_id(),
        name=body.name,
        pipeline_id=body.pipeline_id,
        platform_id=body.platform_id or None,
        cron_expr=body.cron_expr,
        topic_template=body.topic_template,
        keywords=body.keywords,
        extra=body.extra,
        active=body.active,
        next_run=_calc_next_run(body.cron_expr),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _serialize(s, db)


@router.put("/{schedule_id}")
def update_schedule(schedule_id: str, body: ScheduleUpdate, db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not s:
        raise HTTPException(404, "Schedule not found")
    if body.name is not None:           s.name = body.name
    if body.pipeline_id is not None:    s.pipeline_id = body.pipeline_id
    if body.platform_id is not None:    s.platform_id = body.platform_id or None
    if body.cron_expr is not None:
        s.cron_expr = body.cron_expr
        s.next_run = _calc_next_run(body.cron_expr)
    if body.topic_template is not None: s.topic_template = body.topic_template
    if body.keywords is not None:       s.keywords = body.keywords
    if body.extra is not None:          s.extra = body.extra
    if body.active is not None:
        s.active = body.active
        if body.active and not s.next_run:
            s.next_run = _calc_next_run(s.cron_expr)
    db.commit()
    db.refresh(s)
    return _serialize(s, db)


@router.post("/{schedule_id}/toggle")
def toggle_schedule(schedule_id: str, db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not s:
        raise HTTPException(404, "Schedule not found")
    s.active = not s.active
    if s.active:
        s.next_run = _calc_next_run(s.cron_expr)
    db.commit()
    return _serialize(s, db)


@router.post("/{schedule_id}/fire")
async def fire_schedule_now(schedule_id: str, db: Session = Depends(get_db)):
    """Запустити розклад вручну одразу зараз."""
    s = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not s:
        raise HTTPException(404, "Schedule not found")
    from orchestrator import _fire_schedule, manager
    job = await _fire_schedule(db, s)
    if not job:
        raise HTTPException(500, "Не вдалось запустити — перевір пайплайн")
    await manager.broadcast({"type": "job_update", "job_id": job.id, "status": "running"})
    return {"ok": True, "job_id": job.id}


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not s:
        raise HTTPException(404, "Schedule not found")
    db.delete(s)
    db.commit()
