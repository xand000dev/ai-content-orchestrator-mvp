"""
Тестовий сценарій: Telegram контент-завод

Запуск:
    python seed.py

Після запуску:
    1. Створи .env з ключами (дивись .env.example)
    2. python main.py
    3. http://localhost:8000 → Задачі → Старт
"""

import json, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from database import Base, engine, SessionLocal, Agent, Pipeline, Platform, Job, new_id

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# Моделі що реально відповідають (підтверджено користувачем)
MODEL_FAST  = "qwen/qwen3.6-plus:free"
MODEL_FLASH = "stepfun/step-3.5-flash:free"
MODEL_DEEP  = "qwen/qwen3.6-plus:free"


def clean():
    from database import Task
    for m in [Task, Job, Agent, Pipeline, Platform]:
        db.query(m).delete()
    db.commit()
    print("✓ Базу очищено")


def seed_agents():
    agents = [
        Agent(
            id=new_id(), name="Дослідник", type="researcher",
            model=MODEL_DEEP,
            system_prompt=(
                "Ти — контент-стратег для Telegram каналів. "
                "Отримуєш тему і надаєш: "
                "1) Чому ця тема зараз актуальна; "
                "2) 3-5 конкретних підтем та аргументів; "
                "3) Цікавий факт або статистика; "
                "4) Рекомендований кут подачі. "
                "Відповідай коротко та по суті. Мова: українська."
            ),
            status="idle",
        ),
        Agent(
            id=new_id(), name="Telegram Автор", type="writer",
            model=MODEL_FAST,
            system_prompt=(
                "Ти — топовий автор Telegram каналів з мільйонною аудиторією. "
                "Пишеш пости що збирають сотні реакцій. Твій стиль: "
                "• Перший рядок — ГАЧОК (провокує, дивує або інтригує). Ніякого 'Друзі, сьогодні...' "
                "• Структура: гачок → ситуація/проблема → твій погляд → практичний висновок → CTA "
                "• Довжина: 200-800 символів залежно від теми "
                "• 1-2 емодзі, не більше "
                "• В кінці: заклик до дії або питання до читачів "
                "Відповідай ЛИШЕ текстом поста, без коментарів. Мова: українська."
            ),
            status="idle",
        ),
        Agent(
            id=new_id(), name="Редактор", type="editor",
            model=MODEL_FLASH,
            system_prompt=(
                "Ти — редактор Telegram каналу. "
                "Отримуєш чорновик поста і: "
                "1) Скорочуєш воду та зайві слова; "
                "2) Посилюєш гачок у першому реченні; "
                "3) Перевіряєш чи є чіткий CTA в кінці; "
                "4) Повертаєш ЛИШЕ фінальний текст поста без пояснень. "
                "Мова: українська."
            ),
            status="idle",
        ),
    ]
    for a in agents:
        db.add(a)
    db.commit()
    print(f"✓ {len(agents)} агентів")
    return agents


def seed_platforms():
    platforms = [
        Platform(
            id=new_id(), name="Мій Telegram Канал", type="telegram",
            credentials=json.dumps({
                "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "ВСТАВИТИ_ТОКЕН_ТУТ"),
                "channel_id": "@your_channel",   # або -100xxxxxxxxxx
            }),
            settings=json.dumps({"language": "uk"}),
            auto_publish=False,   # True = автоматично відправляти після завершення
            active=True,
        ),
    ]
    for p in platforms:
        db.add(p)
    db.commit()
    print(f"✓ {len(platforms)} платформ")
    return platforms


def seed_pipelines():
    # Простий пайплайн: дослідження → пост з перевіркою
    tg_simple = Pipeline(
        id=new_id(), name="Telegram: Пост (з перевіркою)", platform="telegram",
        content_type="text",
        description="Дослідження → Написання → [Перевірка] → Відправка",
        steps=json.dumps([
            {"step_name": "дослідження",  "agent_type": "researcher", "depends_on": [],               "review_required": False},
            {"step_name": "пост",         "agent_type": "writer",     "depends_on": ["дослідження"],  "review_required": True},
        ]),
    )

    # Пайплайн з редактором (без ревью — автопілот)
    tg_auto = Pipeline(
        id=new_id(), name="Telegram: Автопілот (без перевірки)", platform="telegram",
        content_type="text",
        description="Дослідження → Написання → Редагування → Одразу в канал",
        steps=json.dumps([
            {"step_name": "дослідження",  "agent_type": "researcher", "depends_on": [],               "review_required": False},
            {"step_name": "чорновик",     "agent_type": "writer",     "depends_on": ["дослідження"],  "review_required": False},
            {"step_name": "фінал",        "agent_type": "editor",     "depends_on": ["чорновик"],     "review_required": False},
        ]),
    )

    db.add(tg_simple)
    db.add(tg_auto)
    db.commit()
    print("✓ 2 пайплайни")
    return tg_simple, tg_auto


def seed_jobs(tg_simple, tg_auto, platform):
    jobs = [
        Job(
            id=new_id(), pipeline_id=tg_simple.id, status="pending",
            initial_input=json.dumps({
                "topic": "Чому більшість людей не заробляють онлайн — і що з цим робити",
                "keywords": "онлайн заробіток, помилки, фриланс",
                "platform_id": platform.id,
                "extra": "Тон: чесний, без мотиваційного спаму. Аудиторія: 20-35 років.",
            }),
        ),
        Job(
            id=new_id(), pipeline_id=tg_auto.id, status="pending",
            initial_input=json.dumps({
                "topic": "5 речей які я б зробив по-іншому якби починав канал з нуля",
                "keywords": "telegram канал, помилки, ріст",
                "platform_id": platform.id,
                "extra": "Від першої особи. Особистий досвід. Конкретні приклади.",
            }),
        ),
    ]
    for j in jobs:
        db.add(j)
    db.commit()
    print(f"✓ {len(jobs)} задачі готові до запуску")
    return jobs


if __name__ == "__main__":
    print("\n🌱 Seed...\n")
    clean()
    seed_agents()
    platforms = seed_platforms()
    tg_simple, tg_auto = seed_pipelines()
    seed_jobs(tg_simple, tg_auto, platforms[0])
    db.close()

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    token_status = "✅ знайдено в env" if tg_token else "⚠️  НЕ встановлено"

    print(f"""
═══════════════════════════════════════════════════════
✅ Готово!

ДЕ ВКАЗАТИ ТОКЕНИ:
  Файл:  ai_orchestrator/.env

  Вміст:
    OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxx
    TELEGRAM_BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxx

  TELEGRAM_BOT_TOKEN {token_status}

ЯК ОТРИМАТИ TELEGRAM ТОКЕН:
  1. Відкрий @BotFather у Telegram
  2. /newbot → введи ім'я бота
  3. Скопіюй токен у .env
  4. Додай бота як АДМІНІСТРАТОРА свого каналу

ЗАПУСК:
  python main.py → http://localhost:8000
  Задачі → натисни "Старт" → спостерігай на Дашборді
═══════════════════════════════════════════════════════
""")
