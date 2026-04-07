"""
AutoManager — AI meta-agent that autonomously manages the content factory.

Every check_interval_minutes it runs a full cycle:
  OBSERVE  → collects system state (agents, jobs, topics, strategies)
  THINK    → calls OpenRouter → receives JSON {thinking, actions:[...]}
  ACT      → executes actions: launch_topic / approve_topic / generate_topics / create_job / noop
  BROADCAST → WebSocket "auto_manager_decision" for real-time dashboard feed
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Set

from database import (
    SessionLocal, AutoManager, Strategy, TopicSuggestion,
    Pipeline, Platform, Job, Task, Agent, new_id, safe_json,
)

logger = logging.getLogger(__name__)


# ── Observe ───────────────────────────────────────────────────────────────────

def _observe(db, am: AutoManager) -> dict:
    """Collect current system state relevant to this AutoManager."""
    now = datetime.utcnow()

    all_agents = db.query(Agent).all()
    idle_count = sum(1 for a in all_agents if a.status == "idle")

    running_jobs = db.query(Job).filter(Job.status == "running").all()

    recent_done = (
        db.query(Job)
        .filter(Job.status == "done", Job.completed_at >= now - timedelta(hours=24))
        .order_by(Job.completed_at.desc())
        .limit(5).all()
    )

    managed_ids = safe_json(am.strategy_ids, [])
    if managed_ids:
        strategies = db.query(Strategy).filter(Strategy.id.in_(managed_ids)).all()
    else:
        strategies = db.query(Strategy).filter(Strategy.active == True).all()

    pipelines = db.query(Pipeline).all()
    platforms = db.query(Platform).filter(Platform.active == True).all()

    pending_topics, approved_topics = [], []
    for s in strategies:
        for t in db.query(TopicSuggestion).filter(
            TopicSuggestion.strategy_id == s.id, TopicSuggestion.status == "pending"
        ).limit(5).all():
            pending_topics.append({"id": t.id, "topic": t.topic[:80], "strategy": s.name, "strategy_id": s.id})

        for t in db.query(TopicSuggestion).filter(
            TopicSuggestion.strategy_id == s.id, TopicSuggestion.status == "approved"
        ).limit(5).all():
            approved_topics.append({"id": t.id, "topic": t.topic[:80], "strategy": s.name, "strategy_id": s.id})

    return {
        "now_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()],
        "agents": {"idle": idle_count, "total": len(all_agents)},
        "running_jobs": [
            {"id": j.id[:8], "topic": safe_json(j.initial_input, {}).get("topic", "?")[:60]}
            for j in running_jobs
        ],
        "recent_done_24h": [
            {"topic": safe_json(j.initial_input, {}).get("topic", "?")[:60]}
            for j in recent_done
        ],
        "strategies": [
            {
                "id": s.id,
                "name": s.name,
                "niche": s.niche,
                "pipeline_id": s.pipeline_id,
                "pipeline_name": next((p.name for p in pipelines if p.id == s.pipeline_id), None),
                "platform_id": s.platform_id,
                "frequency_per_week": s.frequency_per_week,
            }
            for s in strategies
        ],
        "pending_topics": pending_topics,
        "approved_topics": approved_topics,
        "pipelines": [{"id": p.id, "name": p.name} for p in pipelines],
        "platforms": [{"id": p.id, "name": p.name, "type": p.type} for p in platforms],
        "max_jobs_per_run": am.max_jobs_per_run,
        "max_concurrent_jobs": am.max_concurrent_jobs,
    }


# ── Think ─────────────────────────────────────────────────────────────────────

def _build_prompt(state: dict, am: AutoManager) -> tuple[str, str]:
    system = (
        "You are an autonomous AI Content Manager for a Telegram channel content factory.\n"
        "Your role: analyze the current system state and decide what content actions to take.\n\n"
        f"YOUR GOAL: {am.goal or 'Keep the content pipeline active with quality posts.'}\n\n"
        "PRIORITY ORDER (follow strictly):\n"
        f"1. If approved topics exist → launch them (up to {state['max_jobs_per_run']} per cycle)\n"
        f"2. If pending topics exist → approve the most promising ones\n"
        "3. If topic queue is empty → generate new topics for a strategy\n"
        f"4. If {state['max_concurrent_jobs']}+ jobs are already running → choose noop\n"
        "5. If nothing is needed → choose noop\n\n"
        "You MUST respond with valid JSON only — no markdown, no text outside JSON:\n"
        '{"thinking": "1-2 sentences assessment", "actions": [...]}\n\n'
        "Available action types:\n"
        '  {"type": "noop", "reason": "..."}\n'
        '  {"type": "launch_topic", "topic_id": "<full-uuid>", "reason": "..."}\n'
        '  {"type": "approve_topic", "topic_id": "<full-uuid>", "reason": "..."}\n'
        '  {"type": "generate_topics", "strategy_id": "<full-uuid>", "count": 5, "reason": "..."}\n'
        '  {"type": "create_job", "pipeline_id": "<full-uuid>", "topic": "text", '
        '"keywords": "kw1, kw2", "platform_id": "<full-uuid>", "reason": "..."}\n'
    )

    def fmt_list(items, fn):
        return "\n".join(fn(i) for i in items) or "  (none)"

    user = (
        f"CURRENT TIME: {state['now_utc']} ({state['weekday']})\n\n"
        f"AGENTS: {state['agents']['idle']}/{state['agents']['total']} idle\n\n"
        f"RUNNING JOBS ({len(state['running_jobs'])}):\n"
        + fmt_list(state["running_jobs"], lambda j: f"  [{j['id']}] {j['topic']}") + "\n\n"
        "STRATEGIES YOU MANAGE:\n"
        + fmt_list(state["strategies"], lambda s: f"  [{s['id'][:8]}] {s['name']} | {s['niche']} | pipeline: {s.get('pipeline_name','?')} | {s['frequency_per_week']}×/week") + "\n\n"
        "PENDING TOPICS (need approval):\n"
        + fmt_list(state["pending_topics"], lambda t: f"  [{t['id']}] \"{t['topic']}\" [{t['strategy']}]") + "\n\n"
        "APPROVED TOPICS (ready to launch):\n"
        + fmt_list(state["approved_topics"], lambda t: f"  [{t['id']}] \"{t['topic']}\" [{t['strategy']}]") + "\n\n"
        "RECENTLY COMPLETED (last 24h):\n"
        + fmt_list(state["recent_done_24h"], lambda j: f"  - {j['topic']}") + "\n\n"
        "AVAILABLE PIPELINES:\n"
        + fmt_list(state["pipelines"], lambda p: f"  [{p['id']}] {p['name']}") + "\n\n"
        "AVAILABLE PLATFORMS:\n"
        + fmt_list(state["platforms"], lambda p: f"  [{p['id']}] {p['name']} ({p['type']})") + "\n\n"
        "Decide what to do right now."
    )
    return system, user


def _parse_decision(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    logger.warning("AutoManager: could not parse decision: %s", text[:300])
    return {"thinking": "parse error", "actions": [{"type": "noop", "reason": "could not parse AI response"}]}


# ── Act ───────────────────────────────────────────────────────────────────────

async def _execute_actions(db, am: AutoManager, actions: list, state: dict) -> list[dict]:
    log = []
    jobs_launched = 0

    for action in actions:
        atype = action.get("type", "noop")
        try:
            if atype == "noop":
                log.append({"type": "noop", "reason": action.get("reason", "")})

            elif atype == "launch_topic":
                if jobs_launched >= am.max_jobs_per_run:
                    log.append({"type": "launch_topic", "skipped": True, "reason": "max_jobs_per_run reached"})
                    continue
                if len(state["running_jobs"]) + jobs_launched >= am.max_concurrent_jobs:
                    log.append({"type": "launch_topic", "skipped": True, "reason": "max_concurrent_jobs reached"})
                    continue
                topic_id = action.get("topic_id", "")
                t = db.query(TopicSuggestion).filter(TopicSuggestion.id == topic_id).first()
                if not t:
                    # try prefix match
                    all_t = state["approved_topics"] + state["pending_topics"]
                    match_id = next((x["id"] for x in all_t if x["id"].startswith(topic_id)), None)
                    t = db.query(TopicSuggestion).filter(TopicSuggestion.id == match_id).first() if match_id else None
                if not t or t.status not in ("approved", "pending"):
                    log.append({"type": "launch_topic", "error": f"topic not found: {topic_id[:8]}"})
                    continue
                s = db.query(Strategy).filter(Strategy.id == t.strategy_id).first()
                if not s or not s.pipeline_id:
                    log.append({"type": "launch_topic", "error": "strategy has no pipeline"})
                    continue
                pipeline = db.query(Pipeline).filter(Pipeline.id == s.pipeline_id).first()
                if not pipeline:
                    log.append({"type": "launch_topic", "error": "pipeline not found"})
                    continue
                steps = json.loads(pipeline.steps or "[]")
                job = _create_job(db, t.topic, t.keywords, s.platform_id or "", s.pipeline_id, steps, extra=t.extra)
                t.status = "launched"
                t.job_id = job.id
                db.commit()
                jobs_launched += 1
                log.append({"type": "launch_topic", "topic": t.topic[:60], "job_id": job.id[:8]})
                logger.info("🤖 AutoManager launched topic '%s' → Job %s", t.topic[:50], job.id[:8])

            elif atype == "approve_topic":
                topic_id = action.get("topic_id", "")
                t = db.query(TopicSuggestion).filter(TopicSuggestion.id == topic_id).first()
                if not t:
                    match_id = next((x["id"] for x in state["pending_topics"] if x["id"].startswith(topic_id)), None)
                    t = db.query(TopicSuggestion).filter(TopicSuggestion.id == match_id).first() if match_id else None
                if not t:
                    log.append({"type": "approve_topic", "error": f"topic not found: {topic_id[:8]}"})
                    continue
                t.status = "approved"
                db.commit()
                log.append({"type": "approve_topic", "topic": t.topic[:60]})
                logger.info("🤖 AutoManager approved topic '%s'", t.topic[:50])

            elif atype == "generate_topics":
                strategy_id = action.get("strategy_id", "")
                if not db.query(Strategy).filter(Strategy.id == strategy_id).first():
                    match_id = next((s["id"] for s in state["strategies"] if s["id"].startswith(strategy_id)), None)
                    strategy_id = match_id or strategy_id
                count = min(int(action.get("count", 5)), 10)
                generated = await _generate_topics_for_strategy(db, strategy_id, count)
                log.append({"type": "generate_topics", "strategy_id": strategy_id[:8], "generated": generated})

            elif atype == "create_job":
                if jobs_launched >= am.max_jobs_per_run:
                    log.append({"type": "create_job", "skipped": True, "reason": "max_jobs_per_run reached"})
                    continue
                if len(state["running_jobs"]) + jobs_launched >= am.max_concurrent_jobs:
                    log.append({"type": "create_job", "skipped": True, "reason": "max_concurrent_jobs reached"})
                    continue
                pipeline_id = action.get("pipeline_id", "")
                if not db.query(Pipeline).filter(Pipeline.id == pipeline_id).first():
                    match_id = next((p["id"] for p in state["pipelines"] if p["id"].startswith(pipeline_id)), None)
                    pipeline_id = match_id or pipeline_id
                pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
                if not pipeline:
                    log.append({"type": "create_job", "error": "pipeline not found"})
                    continue
                platform_id = action.get("platform_id", "")
                if platform_id and not db.query(Platform).filter(Platform.id == platform_id).first():
                    match_id = next((p["id"] for p in state["platforms"] if p["id"].startswith(platform_id)), None)
                    platform_id = match_id or platform_id
                steps = json.loads(pipeline.steps or "[]")
                topic = action.get("topic", "")
                job = _create_job(db, topic, action.get("keywords", ""), platform_id, pipeline_id, steps)
                db.commit()
                jobs_launched += 1
                log.append({"type": "create_job", "topic": topic[:60], "job_id": job.id[:8]})
                logger.info("🤖 AutoManager created job '%s' → %s", topic[:50], job.id[:8])

        except Exception as e:
            logger.exception("AutoManager action '%s' failed: %s", atype, e)
            log.append({"type": atype, "error": str(e)})

    return log


def _create_job(db, topic: str, keywords: str, platform_id: str, pipeline_id: str, steps: list, extra: str = "") -> Job:
    job = Job(
        id=new_id(),
        pipeline_id=pipeline_id,
        status="running",
        initial_input=json.dumps({
            "topic": topic,
            "keywords": keywords,
            "platform_id": platform_id,
            "extra": extra,
            "created_by": "auto_manager",
        }),
        started_at=datetime.utcnow(),
    )
    db.add(job)
    for step in steps:
        db.add(Task(
            id=new_id(),
            job_id=job.id,
            step_name=step["step_name"],
            agent_type=step["agent_type"],
            depends_on=json.dumps(step.get("depends_on", [])),
            review_required=step.get("review_required", False),
            status="pending",
        ))
    return job


async def _generate_topics_for_strategy(db, strategy_id: str, count: int) -> int:
    from openrouter import call_openrouter, FREE_MODELS

    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        return 0

    pillars = json.loads(s.content_pillars or "[]")
    system = (
        "You are a content strategist. Generate specific Telegram post topic ideas. "
        "Respond ONLY with a valid JSON array, no markdown."
    )
    user = (
        f"Generate {count} unique Telegram post topics for: {s.niche or 'general'}\n"
        f"Audience: {s.audience or 'general'}\n"
        f"Content pillars: {', '.join(pillars) or 'any'}\n"
        f"Tone: {s.tone or 'practical'}\n\n"
        f"JSON array only:\n"
        f'[{{"topic": "...", "keywords": "kw1, kw2", "extra": ""}}, ...]'
    )

    try:
        raw = await call_openrouter(
            model=FREE_MODELS[0],
            system_prompt=system,
            user_message=user,
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
    except Exception as e:
        logger.error("AutoManager generate_topics failed: %s", e)
        return 0

    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"```\s*$", "", raw).strip()
    try:
        items = json.loads(raw)
    except Exception:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        items = json.loads(match.group()) if match else []

    created = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("topic"):
            continue
        db.add(TopicSuggestion(
            id=new_id(),
            strategy_id=strategy_id,
            topic=str(item.get("topic", "")).strip(),
            keywords=str(item.get("keywords", "")).strip(),
            extra=str(item.get("extra", "")).strip(),
            status="pending",
        ))
        created += 1

    db.commit()
    logger.info("🤖 AutoManager generated %d topics for strategy %s", created, strategy_id[:8])
    return created


# ── Main cycle ────────────────────────────────────────────────────────────────

# Guard: prevent concurrent runs of the same manager
_running_managers: Set[str] = set()


async def run_auto_manager(am_id: str, force: bool = False):
    """Execute one full AutoManager decision cycle: observe → think → act → broadcast.

    force=True skips the active check (used for manual "Run now" triggers).
    """
    if am_id in _running_managers:
        logger.info("🤖 AutoManager %s already running — skipping duplicate", am_id[:8])
        return
    _running_managers.add(am_id)

    from openrouter import call_openrouter
    from orchestrator import manager as ws_manager, _broadcast_log  # local import to avoid circular

    db = SessionLocal()
    try:
        am = db.query(AutoManager).filter(AutoManager.id == am_id).first()
        if not am:
            return
        if not force and not am.active:
            return

        logger.info("🤖 AutoManager '%s' running cycle...", am.name)

        # 1. Observe
        state = _observe(db, am)

        # 2. Think
        system_prompt, user_message = _build_prompt(state, am)
        try:
            raw = await call_openrouter(
                model=am.model,
                system_prompt=system_prompt,
                user_message=user_message,
                api_key=os.getenv("OPENROUTER_API_KEY"),
            )
        except Exception as e:
            logger.error("AutoManager '%s' OpenRouter error: %s", am.name, e)
            await _broadcast_log("error", "autobot", f"🤖 {am.name}: OpenRouter помилка — {str(e)[:100]}")
            am.last_run = datetime.utcnow()
            am.last_thinking = f"OpenRouter error: {e}"
            am.last_actions = json.dumps([{"type": "noop", "error": str(e)}])
            db.commit()
            return

        decision = _parse_decision(raw)
        thinking = decision.get("thinking", "")
        actions = decision.get("actions", [])
        if not isinstance(actions, list):
            actions = []

        logger.info("🤖 AutoManager '%s' thinking: %s", am.name, thinking)
        await _broadcast_log("info", "autobot", f"🤖 {am.name}: {thinking[:150]}", {"actions_count": len(actions)})

        # 3. Act
        action_log = await _execute_actions(db, am, actions, state)

        # 4. Save state
        am.last_run = datetime.utcnow()
        am.last_thinking = thinking
        am.last_actions = json.dumps(action_log)
        db.commit()

        # 5. Broadcast
        await ws_manager.broadcast({
            "type": "auto_manager_decision",
            "manager_id": am_id,
            "manager_name": am.name,
            "thinking": thinking,
            "actions": action_log,
        })

        logger.info("🤖 AutoManager '%s' cycle done: %d actions", am.name, len(action_log))
        for al in action_log:
            atype = al.get('type', 'noop')
            if al.get('error'):
                await _broadcast_log("error", "autobot", f"🤖 {am.name} [{atype}]: {al['error']}")
            elif atype == 'launch_topic':
                await _broadcast_log("success", "autobot", f"🤖 {am.name}: запустив тему '{al.get('topic', '?')[:60]}'")
            elif atype == 'approve_topic':
                await _broadcast_log("success", "autobot", f"🤖 {am.name}: схвалив тему '{al.get('topic', '?')[:60]}'")
            elif atype == 'generate_topics':
                await _broadcast_log("info", "autobot", f"🤖 {am.name}: згенеровано {al.get('generated', 0)} тем")
            elif atype != 'noop':
                await _broadcast_log("info", "autobot", f"🤖 {am.name}: {atype}")

    except Exception as e:
        logger.exception("AutoManager '%s' cycle error: %s", am_id, e)
    finally:
        _running_managers.discard(am_id)
        db.close()


async def tick_auto_managers():
    """Called every 60s from orchestrator loop. Fires any overdue AutoManagers."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        managers = db.query(AutoManager).filter(AutoManager.active == True).all()
        for am in managers:
            interval = timedelta(minutes=am.check_interval_minutes)
            if am.last_run is None or (now - am.last_run) >= interval:
                asyncio.create_task(run_auto_manager(am.id))
    except Exception:
        logger.exception("tick_auto_managers error")
    finally:
        db.close()
