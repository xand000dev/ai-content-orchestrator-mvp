import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Agent, new_id

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    type: str
    model: str = "meta-llama/llama-3.3-70b-instruct:free"
    system_prompt: str = ""
    capabilities: list = []
    max_parallel_tasks: int = 1


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    capabilities: Optional[list] = None
    max_parallel_tasks: Optional[int] = None
    status: Optional[str] = None


def _serialize(a: Agent) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "type": a.type,
        "model": a.model,
        "system_prompt": a.system_prompt,
        "capabilities": json.loads(a.capabilities or "[]"),
        "max_parallel_tasks": a.max_parallel_tasks,
        "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("")
def list_agents(db: Session = Depends(get_db)):
    return [_serialize(a) for a in db.query(Agent).order_by(Agent.created_at).all()]


@router.post("", status_code=201)
def create_agent(body: AgentCreate, db: Session = Depends(get_db)):
    agent = Agent(
        id=new_id(),
        name=body.name,
        type=body.type,
        model=body.model,
        system_prompt=body.system_prompt,
        capabilities=json.dumps(body.capabilities),
        max_parallel_tasks=body.max_parallel_tasks,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return _serialize(agent)


@router.get("/{agent_id}")
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(404, "Agent not found")
    return _serialize(a)


@router.put("/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate, db: Session = Depends(get_db)):
    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(404, "Agent not found")
    if body.name is not None:
        a.name = body.name
    if body.type is not None:
        a.type = body.type
    if body.model is not None:
        a.model = body.model
    if body.system_prompt is not None:
        a.system_prompt = body.system_prompt
    if body.capabilities is not None:
        a.capabilities = json.dumps(body.capabilities)
    if body.max_parallel_tasks is not None:
        a.max_parallel_tasks = body.max_parallel_tasks
    if body.status is not None:
        a.status = body.status
    db.commit()
    db.refresh(a)
    return _serialize(a)


@router.delete("/{agent_id}", status_code=204)
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(404, "Agent not found")
    db.delete(a)
    db.commit()
