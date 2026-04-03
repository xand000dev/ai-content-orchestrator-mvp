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
