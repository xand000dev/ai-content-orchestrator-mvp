# AI Content Orchestrator

> Автономна фабрика контенту для Telegram-каналів на базі OpenRouter + FastAPI

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-embedded-003B57?logo=sqlite&logoColor=white)
![Alpine.js](https://img.shields.io/badge/Alpine.js-3-8BC0D0?logo=alpine.js&logoColor=white)

---

## Що це таке

Система, де AI-агенти по черзі виконують кроки контент-пайплайну — дослідження → написання → редактура → публікація в Telegram. Людина визначає структуру, агенти виконують, **AutoManager** думає замість тебе.

```
Стратегія ──► Теми ──► Job ──► Tasks (DAG) ──► Telegram
                 ▲                               │
                 └────── AutoManager ◄───────────┘
                         (observe→think→act)
```

---

## Ключові можливості

| # | Фіча | Опис |
|---|------|------|
| 🤖 | **AutoManager** | AI-агент що сам вирішує: що запустити, скільки тримати активним, коли генерувати нові теми |
| 🗺️ | **DAG-пайплайни** | Кроки з `depends_on` — паралельне та послідовне виконання |
| 📱 | **Telegram Bot Control** | Схвалення/відхилення контенту прямо з телефону через inline-кнопки |
| 💡 | **Auto-Topic Generator** | AI генерує ідеї для контенту на основі ніші та аудиторії стратегії |
| ⏰ | **Планувальник** | Cron-розклад з шаблонами тем (`{date}`, `{weekday}`) |
| 🗃️ | **Obsidian Vault** | Всі outputs агентів автоматично зберігаються як linked Markdown notes |
| ⚡ | **Real-time UI** | WebSocket оновлення, toast-сповіщення, animated live feed |

---

## Швидкий старт

```bash
# 1. Залежності
pip install -r requirements.txt

# 2. Конфіг
cp .env.example .env
# Відкрий .env і заповни OPENROUTER_API_KEY (обов'язково)

# 3. Запуск
python main.py
# → http://localhost:8000
```

### Наповнити тестовими даними

```bash
python seed.py
```

Створює: 5 агентів, 3 пайплайни, 2 платформи, 5 jobs, 2 розклади, 3 стратегії з темами.

---

## Конфігурація `.env`

```env
# Обов'язково
OPENROUTER_API_KEY=sk-or-...

# Telegram публікація
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=@your_channel   # або числовий ID -100...

# Telegram Bot Control (approve/reject з телефону)
TELEGRAM_ADMIN_CHAT_ID=123456789
TELEGRAM_BOT_CONTROL=true

# Obsidian Knowledge Base
OBSIDIAN_VAULT_PATH=C:/Users/you/ObsidianVault
```

---

## Архітектура

### Потік даних

```
Pipeline (шаблон кроків)
    └─► Job (конкретна тема)
            └─► Tasks (окремі виконання агентів)
                    └─► OpenRouter LLM
                            └─► output_data → Telegram / Obsidian
```

### Основні файли

```
main.py               — FastAPI app, startup, WebSocket endpoint
orchestrator.py       — головний цикл (2s tick): dispatch tasks, check completion
auto_manager.py       — AutoManager: observe → think (OpenRouter) → act → broadcast
database.py           — SQLAlchemy моделі: Pipeline, Job, Task, Agent, Strategy, ...
telegram_bot.py       — send_message, bot polling, inline keyboard callbacks
obsidian_writer.py    — запис нотаток у Obsidian vault
routers/
  agents.py           — CRUD агентів
  pipelines.py        — CRUD пайплайнів
  jobs.py             — CRUD + /start, /stop
  tasks.py            — list, /approve, /reject
  schedules.py        — CRUD + /toggle, /fire
  strategies.py       — CRUD + /generate-topics, /topics/*
  auto_managers.py    — CRUD + /toggle, /run
static/index.html     — Single-file SPA (Alpine.js + Tailwind CDN)
seed.py               — тестові дані
```

### Statuses задач

```
pending → queued → running → waiting_review → approved
                           → done
                           → error
```

### AutoManager цикл

```python
# Кожні check_interval_minutes:
state  = _observe(db, am)          # агенти, jobs, теми, стратегії
prompt = _build_prompt(state, am)  # system + user message
resp   = openrouter(prompt)        # → {thinking, actions:[...]}
log    = _execute_actions(...)     # launch_topic / approve_topic / generate_topics / noop
broadcast("auto_manager_decision") # → WebSocket → live feed на дашборді
```

Пріоритет дій: `approved_topics → approve_pending → generate_topics → noop`

---

## API

| Метод | Endpoint | Опис |
|-------|----------|------|
| GET | `/api/health` | Статус конфігурації |
| GET | `/api/telegram/test?channel_id=...` | Тест підключення до каналу |
| POST | `/api/telegram/send-content` | Надіслати job напряму |
| GET | `/api/obsidian/status` | Статус Obsidian інтеграції |
| POST | `/api/obsidian/sync-all` | Синхронізувати всі jobs у vault |
| GET | `/api/bot-control/status` | Статус Telegram Bot Control |
| * | `/api/agents` | CRUD агентів |
| * | `/api/pipelines` | CRUD пайплайнів |
| * | `/api/jobs` | CRUD + start/stop |
| * | `/api/tasks` | List + approve/reject |
| * | `/api/schedules` | CRUD + toggle/fire |
| * | `/api/strategies` | CRUD + generate-topics |
| * | `/api/auto-managers` | CRUD + toggle/run |
| WS | `/ws` | Real-time оновлення |

---

## Вільні моделі OpenRouter

Перевірено що працюють:

```
qwen/qwen3.6-plus:free          ← рекомендовано для AutoManager
stepfun/step-3.5-flash:free
meta-llama/llama-3.3-70b-instruct:free
google/gemini-2.0-flash-exp:free
deepseek/deepseek-r1:free
```

---

## Розробка

```bash
# Перезапуск з hot-reload (увімкнено за замовчуванням)
python main.py

# Перевірка імпортів після змін
python -c "from auto_manager import tick_auto_managers; print('OK')"
python -c "from routers.auto_managers import router; print('OK')"
```

### Додати новий тип дії до AutoManager

1. Додати обробку в `_execute_actions()` у `auto_manager.py`
2. Описати дію в system prompt у `_build_prompt()`
3. UI автоматично покаже нову дію в live feed (рендерить `action.type` + `action.topic`)

---

## Стек

- **Backend**: Python 3.11, FastAPI, SQLAlchemy, SQLite, uvicorn
- **AI**: OpenRouter (будь-яка модель, за замовчуванням `qwen/qwen3.6-plus:free`)
- **Frontend**: Alpine.js 3, Tailwind CSS CDN, WebSocket
- **Інтеграції**: Telegram Bot API, Obsidian (local vault), croniter
