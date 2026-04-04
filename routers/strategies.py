"""
Strategy + Topic Suggestion API.

Strategy — describes a content channel's focus, audience and style.
TopicSuggestion — AI-generated topic ideas for a strategy; can be
  approved/rejected manually or auto-launched.

Routes:
  GET/POST        /api/strategies
  GET/PUT/DELETE  /api/strategies/{id}
  POST            /api/strategies/{id}/generate-topics?count=5
  GET             /api/strategies/{id}/topics

  POST            /api/topics/{id}/approve
  POST            /api/topics/{id}/reject
  POST            /api/topics/{id}/launch
  DELETE          /api/topics/{id}
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import (
    get_db, new_id,
    Strategy, TopicSuggestion, Pipeline, Platform, Job, Task,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["strategies"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class StrategyCreate(BaseModel):
    name: str
    niche: str = ""
    audience: str = ""
    content_pillars: list[str] = []
    tone: str = ""
    pipeline_id: str = ""
    platform_id: str = ""
    frequency_per_week: int = 3
    auto_approve: bool = False
    active: bool = True


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    niche: Optional[str] = None
    audience: Optional[str] = None
    content_pillars: Optional[list[str]] = None
    tone: Optional[str] = None
    pipeline_id: Optional[str] = None
    platform_id: Optional[str] = None
    frequency_per_week: Optional[int] = None
    auto_approve: Optional[bool] = None
    active: Optional[bool] = None


# ── Serializers ───────────────────────────────────────────────────────────────

def _serialize_strategy(s: Strategy, db: Session) -> dict:
    pipeline = db.query(Pipeline).filter(Pipeline.id == s.pipeline_id).first() if s.pipeline_id else None
    platform = db.query(Platform).filter(Platform.id == s.platform_id).first() if s.platform_id else None
    topic_counts = {
        "total":    db.query(TopicSuggestion).filter(TopicSuggestion.strategy_id == s.id).count(),
        "pending":  db.query(TopicSuggestion).filter(TopicSuggestion.strategy_id == s.id, TopicSuggestion.status == "pending").count(),
        "approved": db.query(TopicSuggestion).filter(TopicSuggestion.strategy_id == s.id, TopicSuggestion.status == "approved").count(),
        "launched": db.query(TopicSuggestion).filter(TopicSuggestion.strategy_id == s.id, TopicSuggestion.status == "launched").count(),
    }
    return {
        "id":                s.id,
        "name":              s.name,
        "niche":             s.niche,
        "audience":          s.audience,
        "content_pillars":   json.loads(s.content_pillars or "[]"),
        "tone":              s.tone,
        "pipeline_id":       s.pipeline_id,
        "pipeline_name":     pipeline.name if pipeline else None,
        "platform_id":       s.platform_id,
        "platform_name":     platform.name if platform else None,
        "frequency_per_week": s.frequency_per_week,
        "auto_approve":      s.auto_approve,
        "active":            s.active,
        "topic_counts":      topic_counts,
        "created_at":        s.created_at.isoformat() if s.created_at else None,
    }


def _serialize_topic(t: TopicSuggestion) -> dict:
    return {
        "id":          t.id,
        "strategy_id": t.strategy_id,
        "topic":       t.topic,
        "keywords":    t.keywords,
        "extra":       t.extra,
        "status":      t.status,
        "job_id":      t.job_id,
        "created_at":  t.created_at.isoformat() if t.created_at else None,
    }


# ── Topic parsing helper ──────────────────────────────────────────────────────

def _parse_topics_from_llm(text: str) -> list[dict]:
    """Extract a JSON array from LLM response, robust to markdown fences."""
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"```$", "", text).strip()
    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # Find first [...] block
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    logger.warning("Could not parse topics JSON from LLM response: %s", text[:200])
    return []


# ── Strategy CRUD ─────────────────────────────────────────────────────────────

@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db)):
    rows = db.query(Strategy).order_by(Strategy.created_at.desc()).all()
    return [_serialize_strategy(s, db) for s in rows]


@router.post("/strategies", status_code=201)
def create_strategy(body: StrategyCreate, db: Session = Depends(get_db)):
    if body.pipeline_id and not db.query(Pipeline).filter(Pipeline.id == body.pipeline_id).first():
        raise HTTPException(404, "Pipeline not found")
    s = Strategy(
        id=new_id(),
        name=body.name,
        niche=body.niche,
        audience=body.audience,
        content_pillars=json.dumps(body.content_pillars),
        tone=body.tone,
        pipeline_id=body.pipeline_id or None,
        platform_id=body.platform_id or None,
        frequency_per_week=body.frequency_per_week,
        auto_approve=body.auto_approve,
        active=body.active,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _serialize_strategy(s, db)


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "Strategy not found")
    return _serialize_strategy(s, db)


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: str, body: StrategyUpdate, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "Strategy not found")
    if body.name is not None:              s.name = body.name
    if body.niche is not None:             s.niche = body.niche
    if body.audience is not None:          s.audience = body.audience
    if body.content_pillars is not None:   s.content_pillars = json.dumps(body.content_pillars)
    if body.tone is not None:              s.tone = body.tone
    if body.pipeline_id is not None:       s.pipeline_id = body.pipeline_id or None
    if body.platform_id is not None:       s.platform_id = body.platform_id or None
    if body.frequency_per_week is not None: s.frequency_per_week = body.frequency_per_week
    if body.auto_approve is not None:      s.auto_approve = body.auto_approve
    if body.active is not None:            s.active = body.active
    db.commit()
    db.refresh(s)
    return _serialize_strategy(s, db)


@router.delete("/strategies/{strategy_id}", status_code=204)
def delete_strategy(strategy_id: str, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "Strategy not found")
    db.query(TopicSuggestion).filter(TopicSuggestion.strategy_id == strategy_id).delete()
    db.delete(s)
    db.commit()


# ── Topic generation ──────────────────────────────────────────────────────────

@router.post("/strategies/{strategy_id}/generate-topics")
async def generate_topics(
    strategy_id: str,
    count: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Call OpenRouter to generate content topic ideas for the strategy."""
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "Strategy not found")

    from openrouter import call_openrouter, FREE_MODELS

    pillars = json.loads(s.content_pillars or "[]")
    pillars_str = ", ".join(pillars) if pillars else "будь-які"

    system_prompt = (
        "You are a content strategist specializing in Telegram channels. "
        "Your task is to generate specific, engaging content topic ideas. "
        "Respond ONLY with a valid JSON array — no markdown, no explanation."
    )
    user_message = (
        f"Generate {count} unique Telegram post topic ideas for a channel about: {s.niche or 'general'}\n"
        f"Target audience: {s.audience or 'general audience'}\n"
        f"Content pillars: {pillars_str}\n"
        f"Tone: {s.tone or 'engaging and practical'}\n\n"
        f"Requirements:\n"
        f"- Each topic must be specific and actionable, not generic\n"
        f"- Vary the angles (how-to, mistakes, case study, list, story)\n"
        f"- Ukrainian or Russian language (match the niche language)\n\n"
        f"Respond with JSON array only:\n"
        f'[{{"topic": "...", "keywords": "keyword1, keyword2", "extra": "optional context"}}, ...]'
    )

    model = FREE_MODELS[0]  # qwen/qwen3.6-plus:free
    api_key = os.getenv("OPENROUTER_API_KEY")

    try:
        raw = await call_openrouter(
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            api_key=api_key,
        )
    except Exception as e:
        raise HTTPException(500, f"OpenRouter error: {e}")

    topics = _parse_topics_from_llm(raw)
    if not topics:
        raise HTTPException(500, f"Не вдалось розпарсити теми. Відповідь моделі: {raw[:300]}")

    created = []
    for item in topics:
        if not isinstance(item, dict) or not item.get("topic"):
            continue
        t = TopicSuggestion(
            id=new_id(),
            strategy_id=strategy_id,
            topic=str(item.get("topic", "")).strip(),
            keywords=str(item.get("keywords", "")).strip(),
            extra=str(item.get("extra", "")).strip(),
            status="pending",
        )
        db.add(t)
        created.append(t)

    # Auto-approve + launch if strategy has auto_approve=True
    launched_count = 0
    if s.auto_approve and s.pipeline_id:
        pipeline = db.query(Pipeline).filter(Pipeline.id == s.pipeline_id).first()
        if pipeline:
            steps = json.loads(pipeline.steps or "[]")
            for t in created:
                job = _create_job_for_topic(db, t, s, steps)
                t.status = "launched"
                t.job_id = job.id
                launched_count += 1

    db.commit()
    logger.info(
        "💡 Generated %d topics for strategy '%s' (auto-launched: %d)",
        len(created), s.name, launched_count,
    )
    return {
        "generated": len(created),
        "launched":  launched_count,
        "auto_approve": s.auto_approve,
    }


# ── Topic list ────────────────────────────────────────────────────────────────

@router.get("/strategies/{strategy_id}/topics")
def list_topics(
    strategy_id: str,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(TopicSuggestion).filter(TopicSuggestion.strategy_id == strategy_id)
    if status:
        q = q.filter(TopicSuggestion.status == status)
    return [_serialize_topic(t) for t in q.order_by(TopicSuggestion.created_at.desc()).all()]


# ── Topic actions ─────────────────────────────────────────────────────────────

@router.post("/topics/{topic_id}/approve")
def approve_topic(topic_id: str, db: Session = Depends(get_db)):
    t = db.query(TopicSuggestion).filter(TopicSuggestion.id == topic_id).first()
    if not t:
        raise HTTPException(404, "Topic not found")
    t.status = "approved"
    db.commit()
    return _serialize_topic(t)


@router.post("/topics/{topic_id}/reject")
def reject_topic(topic_id: str, db: Session = Depends(get_db)):
    t = db.query(TopicSuggestion).filter(TopicSuggestion.id == topic_id).first()
    if not t:
        raise HTTPException(404, "Topic not found")
    t.status = "rejected"
    db.commit()
    return _serialize_topic(t)


@router.post("/topics/{topic_id}/launch")
def launch_topic(topic_id: str, db: Session = Depends(get_db)):
    t = db.query(TopicSuggestion).filter(TopicSuggestion.id == topic_id).first()
    if not t:
        raise HTTPException(404, "Topic not found")
    if t.status == "launched":
        raise HTTPException(400, "Already launched")

    s = db.query(Strategy).filter(Strategy.id == t.strategy_id).first()
    if not s or not s.pipeline_id:
        raise HTTPException(400, "Strategy has no pipeline configured")

    pipeline = db.query(Pipeline).filter(Pipeline.id == s.pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    steps = json.loads(pipeline.steps or "[]")
    if not steps:
        raise HTTPException(400, "Pipeline has no steps")

    job = _create_job_for_topic(db, t, s, steps)
    t.status = "launched"
    t.job_id = job.id
    db.commit()

    logger.info("💡 Topic '%s' launched → Job %s", t.topic[:50], job.id[:8])
    return {"ok": True, "job_id": job.id, "topic": _serialize_topic(t)}


@router.delete("/topics/{topic_id}", status_code=204)
def delete_topic(topic_id: str, db: Session = Depends(get_db)):
    t = db.query(TopicSuggestion).filter(TopicSuggestion.id == topic_id).first()
    if not t:
        raise HTTPException(404, "Topic not found")
    db.delete(t)
    db.commit()


# ── Internal: create job from topic ──────────────────────────────────────────

def _create_job_for_topic(db, topic: TopicSuggestion, strategy: Strategy, steps: list) -> Job:
    """Create and start a Job from a topic suggestion."""
    initial_input = json.dumps({
        "topic":       topic.topic,
        "keywords":    topic.keywords,
        "extra":       topic.extra,
        "platform_id": strategy.platform_id or "",
        "strategy_id": strategy.id,
    })
    job = Job(
        id=new_id(),
        pipeline_id=strategy.pipeline_id,
        status="running",
        initial_input=initial_input,
        started_at=datetime.utcnow(),
    )
    db.add(job)
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
    return job
