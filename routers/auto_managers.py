import json
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, AutoManager, Strategy, new_id

router = APIRouter(prefix="/auto-managers", tags=["auto-managers"])


class AutoManagerCreate(BaseModel):
    name: str
    goal: str = ""
    strategy_ids: list[str] = []
    model: str = "qwen/qwen3.6-plus:free"
    check_interval_minutes: int = 30
    max_jobs_per_run: int = 2
    max_concurrent_jobs: int = 3
    active: bool = False


class AutoManagerUpdate(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    strategy_ids: Optional[list[str]] = None
    model: Optional[str] = None
    check_interval_minutes: Optional[int] = None
    max_jobs_per_run: Optional[int] = None
    max_concurrent_jobs: Optional[int] = None
    active: Optional[bool] = None


def _serialize(am: AutoManager, db: Session) -> dict:
    managed_ids = json.loads(am.strategy_ids or "[]")
    strategies = db.query(Strategy).filter(Strategy.id.in_(managed_ids)).all() if managed_ids else []
    return {
        "id":                     am.id,
        "name":                   am.name,
        "goal":                   am.goal,
        "strategy_ids":           managed_ids,
        "strategy_names":         [s.name for s in strategies],
        "model":                  am.model,
        "check_interval_minutes": am.check_interval_minutes,
        "max_jobs_per_run":       am.max_jobs_per_run,
        "max_concurrent_jobs":    am.max_concurrent_jobs,
        "active":                 am.active,
        "last_run":               am.last_run.isoformat() if am.last_run else None,
        "last_thinking":          am.last_thinking,
        "last_actions":           json.loads(am.last_actions or "[]"),
        "created_at":             am.created_at.isoformat() if am.created_at else None,
    }


@router.get("")
def list_managers(db: Session = Depends(get_db)):
    return [_serialize(am, db) for am in db.query(AutoManager).order_by(AutoManager.created_at).all()]


@router.post("", status_code=201)
def create_manager(body: AutoManagerCreate, db: Session = Depends(get_db)):
    am = AutoManager(
        id=new_id(),
        name=body.name,
        goal=body.goal,
        strategy_ids=json.dumps(body.strategy_ids),
        model=body.model,
        check_interval_minutes=body.check_interval_minutes,
        max_jobs_per_run=body.max_jobs_per_run,
        max_concurrent_jobs=body.max_concurrent_jobs,
        active=body.active,
    )
    db.add(am)
    db.commit()
    db.refresh(am)
    return _serialize(am, db)


@router.put("/{am_id}")
def update_manager(am_id: str, body: AutoManagerUpdate, db: Session = Depends(get_db)):
    am = db.query(AutoManager).filter(AutoManager.id == am_id).first()
    if not am:
        raise HTTPException(404, "AutoManager not found")
    if body.name is not None:                    am.name = body.name
    if body.goal is not None:                    am.goal = body.goal
    if body.strategy_ids is not None:            am.strategy_ids = json.dumps(body.strategy_ids)
    if body.model is not None:                   am.model = body.model
    if body.check_interval_minutes is not None:  am.check_interval_minutes = body.check_interval_minutes
    if body.max_jobs_per_run is not None:        am.max_jobs_per_run = body.max_jobs_per_run
    if body.max_concurrent_jobs is not None:     am.max_concurrent_jobs = body.max_concurrent_jobs
    if body.active is not None:                  am.active = body.active
    db.commit()
    db.refresh(am)
    return _serialize(am, db)


@router.post("/{am_id}/toggle")
def toggle_manager(am_id: str, db: Session = Depends(get_db)):
    am = db.query(AutoManager).filter(AutoManager.id == am_id).first()
    if not am:
        raise HTTPException(404, "AutoManager not found")
    am.active = not am.active
    db.commit()
    return _serialize(am, db)


@router.post("/{am_id}/run")
async def run_now(am_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Trigger an immediate AutoManager decision cycle (non-blocking)."""
    am = db.query(AutoManager).filter(AutoManager.id == am_id).first()
    if not am:
        raise HTTPException(404, "AutoManager not found")
    from auto_manager import run_auto_manager
    background_tasks.add_task(run_auto_manager, am_id, True)
    return {"ok": True, "message": f"Cycle started for '{am.name}'"}


@router.delete("/{am_id}", status_code=204)
def delete_manager(am_id: str, db: Session = Depends(get_db)):
    am = db.query(AutoManager).filter(AutoManager.id == am_id).first()
    if not am:
        raise HTTPException(404, "AutoManager not found")
    db.delete(am)
    db.commit()
