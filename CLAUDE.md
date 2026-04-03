# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
pip install -r requirements.txt
cp .env.example .env          # add your OPENROUTER_API_KEY
python main.py                # http://localhost:8000
```

## Core architecture

**Pipeline → Job → Task** is the fundamental data flow:

- **Pipeline** – reusable template with a list of steps (DAG). Each step declares `agent_type`, `depends_on` (list of step_names), and `review_required`.
- **Job** – one run of a Pipeline with a concrete topic/input. Starting a job creates Task records for every step.
- **Task** – a single agent execution. The orchestrator dispatches it when all `depends_on` steps are `done`/`approved`.

## Orchestrator logic (`orchestrator.py`)

Every 2 seconds `_tick()` runs:
1. Queries `Job.status == "running"`.
2. For each job, calls `_get_ready_tasks()` — tasks whose dependency steps are all terminal.
3. Finds an `idle` Agent of the required `agent_type`.
4. Assigns agent, sets `task.status = "queued"`, `agent.status = "busy"`, fires `asyncio.create_task(_run_task(task_id))`.
5. `_run_task` calls OpenRouter, stores `output_data`, then:
   - sets status to `waiting_review` if `review_required`
   - otherwise sets to `done` (unlocking downstream tasks)
6. `_check_job_completion` marks the job `done`/`error` when all tasks are terminal.

Approving a `waiting_review` task (via `POST /api/tasks/{id}/approve`) sets it to `approved` and re-activates the job so downstream tasks get picked up.

Rejecting resets the task to `pending` so it gets re-dispatched on the next tick.

## Task statuses flow

```
pending → queued → running → waiting_review → approved → (downstream unblocked)
                           → done
                           → error
```

## API routes

| Prefix | Router file |
|---|---|
| `/api/agents` | `routers/agents.py` — full CRUD |
| `/api/pipelines` | `routers/pipelines.py` — full CRUD |
| `/api/jobs` | `routers/jobs.py` — CRUD + `/start`, `/stop` |
| `/api/tasks` | `routers/tasks.py` — list, `/review`, `/approve`, `/reject` |
| `/api/platforms` | `routers/platforms.py` — full CRUD |
| `/ws` | WebSocket in `main.py` — broadcasts `task_update` / `job_update` events |

## Frontend (`static/index.html`)

Single-file SPA using Alpine.js + Tailwind CDN. All state lives in the `app()` function. Real-time updates via WebSocket with a polling fallback every 8 seconds.

Tabs: Dashboard · Review · Jobs · Agents · Pipelines · Platforms.

## OpenRouter free models

- `meta-llama/llama-3.3-70b-instruct:free`
- `google/gemini-2.0-flash-exp:free`
- `deepseek/deepseek-r1:free`
- `mistralai/mistral-7b-instruct:free`
- `qwen/qwen-2.5-72b-instruct:free`

## Database

SQLite (`orchestrator.db`, auto-created). Models in `database.py`. To switch to PostgreSQL: change `DATABASE_URL` in `database.py` and add `psycopg2` to requirements.
