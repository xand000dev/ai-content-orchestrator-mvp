import uuid
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./orchestrator.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_id():
    return str(uuid.uuid4())


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    # researcher | writer | image | voice | editor | publisher | custom
    type = Column(String, nullable=False)
    model = Column(String, default="meta-llama/llama-3.3-70b-instruct:free")
    system_prompt = Column(Text, default="")
    # JSON list of platform slugs this agent supports, e.g. ["youtube","telegram"]
    capabilities = Column(Text, default="[]")
    max_parallel_tasks = Column(Integer, default=1)
    # idle | busy | error | offline
    status = Column(String, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    # youtube | telegram | instagram | tiktok | blog | any
    platform = Column(String, default="any")
    # text | video | image | audio | mixed
    content_type = Column(String, default="text")
    description = Column(Text, default="")
    # JSON list of step dicts:
    # [{"step_name": "research", "agent_type": "researcher",
    #   "depends_on": [], "review_required": false}, ...]
    steps = Column(Text, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=new_id)
    pipeline_id = Column(String, ForeignKey("pipelines.id"), nullable=False)
    # pending | running | done | error | stopped
    status = Column(String, default="pending")
    # JSON: {"topic": "...", "keywords": "...", "platform_id": "...", "extra": "..."}
    initial_input = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=new_id)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)
    step_name = Column(String, nullable=False)
    agent_type = Column(String, nullable=False)
    # pending | running | waiting_review | approved | rejected | done | error
    status = Column(String, default="pending")
    # JSON: merged initial_input + outputs of dependency steps
    input_data = Column(Text, default="{}")
    # JSON: {"result": "...", "step": "..."}
    output_data = Column(Text, nullable=True)
    # JSON list of step_name strings this task depends on
    depends_on = Column(Text, default="[]")
    review_required = Column(Boolean, default=False)
    reviewer_comment = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    pipeline_id = Column(String, ForeignKey("pipelines.id"), nullable=False)
    platform_id = Column(String, nullable=True)
    # Cron expression, e.g. "0 9 * * 1" = every Monday at 9:00 UTC
    cron_expr = Column(String, nullable=False, default="0 9 * * *")
    # Topic template — supports {date} and {weekday} placeholders
    topic_template = Column(Text, default="")
    keywords = Column(Text, default="")
    extra = Column(Text, default="")
    active = Column(Boolean, default=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    niche = Column(String, default="")
    audience = Column(Text, default="")
    # JSON list of content pillars, e.g. ["поради","кейси","помилки"]
    content_pillars = Column(Text, default="[]")
    tone = Column(String, default="")
    pipeline_id = Column(String, ForeignKey("pipelines.id"), nullable=True)
    platform_id = Column(String, nullable=True)
    frequency_per_week = Column(Integer, default=3)
    auto_approve = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TopicSuggestion(Base):
    __tablename__ = "topic_suggestions"

    id = Column(String, primary_key=True, default=new_id)
    strategy_id = Column(String, ForeignKey("strategies.id"), nullable=False)
    topic = Column(Text, nullable=False)
    keywords = Column(Text, default="")
    extra = Column(Text, default="")
    # pending | approved | rejected | launched
    status = Column(String, default="pending")
    job_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AutoManager(Base):
    __tablename__ = "auto_managers"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    # High-level goal, e.g. "Keep 2-3 posts/day active across the freelance channel"
    goal = Column(Text, default="")
    # JSON list of strategy IDs this manager controls (empty = all active strategies)
    strategy_ids = Column(Text, default="[]")
    model = Column(String, default="qwen/qwen3.6-plus:free")
    # How often to run the decision cycle (minutes)
    check_interval_minutes = Column(Integer, default=30)
    # Max jobs to launch per single decision cycle
    max_jobs_per_run = Column(Integer, default=2)
    # Don't launch new jobs if this many are already running
    max_concurrent_jobs = Column(Integer, default=3)
    active = Column(Boolean, default=False)
    last_run = Column(DateTime, nullable=True)
    # AI reasoning text from the last decision
    last_thinking = Column(Text, nullable=True)
    # JSON list of actions taken: [{"type":..., "topic":..., "result":...}]
    last_actions = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Platform(Base):
    __tablename__ = "platforms"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    # youtube | telegram | instagram | tiktok | blog
    type = Column(String, nullable=False)
    # JSON with tokens / API keys for this platform
    credentials = Column(Text, default="{}")
    # JSON: {"schedule": "...", "language": "uk", ...}
    settings = Column(Text, default="{}")
    auto_publish = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
