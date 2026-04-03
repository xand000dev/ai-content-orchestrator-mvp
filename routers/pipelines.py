import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Pipeline, new_id

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


class StepDef(BaseModel):
    step_name: str
    agent_type: str
    depends_on: list = []
    review_required: bool = False


class PipelineCreate(BaseModel):
    name: str
    platform: str = "any"
    content_type: str = "text"
    description: str = ""
    steps: list[StepDef] = []


class PipelineUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    content_type: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[list[StepDef]] = None


def _serialize(p: Pipeline) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "platform": p.platform,
        "content_type": p.content_type,
        "description": p.description,
        "steps": json.loads(p.steps or "[]"),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("")
def list_pipelines(db: Session = Depends(get_db)):
    return [_serialize(p) for p in db.query(Pipeline).order_by(Pipeline.created_at).all()]


@router.post("", status_code=201)
def create_pipeline(body: PipelineCreate, db: Session = Depends(get_db)):
    p = Pipeline(
        id=new_id(),
        name=body.name,
        platform=body.platform,
        content_type=body.content_type,
        description=body.description,
        steps=json.dumps([s.model_dump() for s in body.steps]),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.get("/{pipeline_id}")
def get_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    p = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not p:
        raise HTTPException(404, "Pipeline not found")
    return _serialize(p)


@router.put("/{pipeline_id}")
def update_pipeline(pipeline_id: str, body: PipelineUpdate, db: Session = Depends(get_db)):
    p = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not p:
        raise HTTPException(404, "Pipeline not found")
    if body.name is not None:
        p.name = body.name
    if body.platform is not None:
        p.platform = body.platform
    if body.content_type is not None:
        p.content_type = body.content_type
    if body.description is not None:
        p.description = body.description
    if body.steps is not None:
        p.steps = json.dumps([s.model_dump() for s in body.steps])
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.delete("/{pipeline_id}", status_code=204)
def delete_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    p = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not p:
        raise HTTPException(404, "Pipeline not found")
    db.delete(p)
    db.commit()
