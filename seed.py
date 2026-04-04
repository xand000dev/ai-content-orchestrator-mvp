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
from database import (
    Base, engine, SessionLocal,
    Agent, Pipeline, Platform, Job, Task,
    Schedule, Strategy, TopicSuggestion,
    new_id,
)

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# Моделі що реально відповідають (підтверджено)
MODEL_FAST  = "qwen/qwen3.6-plus:free"
MODEL_FLASH = "stepfun/step-3.5-flash:free"
MODEL_DEEP  = "qwen/qwen3.6-plus:free"


def clean():
    for m in [TopicSuggestion, Strategy, Schedule, Task, Job, Agent, Pipeline, Platform]:
        db.query(m).delete()
    db.commit()
    print("✓ Базу очищено")


# ── Agents ────────────────────────────────────────────────────────────────────

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
        Agent(
            id=new_id(), name="SEO-дослідник", type="researcher",
            model=MODEL_FLASH,
            system_prompt=(
                "Ти — SEO-стратег та аналітик трендів. "
                "Для заданої теми надаєш: "
                "1) 10 ключових слів та фраз для пошуку; "
                "2) Пов'язані теми та питання аудиторії; "
                "3) Головні болі та потреби читачів. "
                "Відповідь у структурованому вигляді. Мова: українська."
            ),
            status="idle",
        ),
        Agent(
            id=new_id(), name="Сторітелер", type="writer",
            model=MODEL_FAST,
            system_prompt=(
                "Ти — майстер сторітелінгу для Telegram. "
                "Перетворюєш будь-яку тему на захопливу історію: "
                "• Починаєш з конкретної сцени або моменту, не з теорії "
                "• Є герой, конфлікт і розв'язка "
                "• Мораль природно випливає з історії, не прописується явно "
                "• 300-600 символів, тримаєш читача до кінця "
                "Відповідай ЛИШЕ текстом поста. Мова: українська."
            ),
            status="idle",
        ),
    ]
    for a in agents:
        db.add(a)
    db.commit()
    print(f"✓ {len(agents)} агентів")
    return {a.name: a for a in agents}


# ── Platforms ─────────────────────────────────────────────────────────────────

def seed_platforms():
    platforms = [
        Platform(
            id=new_id(), name="Мій Telegram Канал", type="telegram",
            credentials=json.dumps({
                "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
                "channel_id": os.getenv("TELEGRAM_CHANNEL_ID", "@your_channel"),
            }),
            settings=json.dumps({"language": "uk"}),
            auto_publish=False,
            active=True,
        ),
        Platform(
            id=new_id(), name="Авто-канал (публікує сам)", type="telegram",
            credentials=json.dumps({
                "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
                "channel_id": os.getenv("TELEGRAM_CHANNEL_ID", "@your_channel"),
            }),
            settings=json.dumps({"language": "uk"}),
            auto_publish=True,
            active=True,
        ),
    ]
    for p in platforms:
        db.add(p)
    db.commit()
    print(f"✓ {len(platforms)} платформи")
    return platforms


# ── Pipelines ─────────────────────────────────────────────────────────────────

def seed_pipelines():
    tg_review = Pipeline(
        id=new_id(), name="Telegram: Пост з перевіркою", platform="telegram",
        content_type="text",
        description="Дослідження → Написання → [Перевірка людиною]",
        steps=json.dumps([
            {"step_name": "дослідження", "agent_type": "researcher", "depends_on": [],              "review_required": False},
            {"step_name": "пост",        "agent_type": "writer",     "depends_on": ["дослідження"], "review_required": True},
        ]),
    )

    tg_auto = Pipeline(
        id=new_id(), name="Telegram: Автопілот", platform="telegram",
        content_type="text",
        description="Дослідження → Написання → Редагування → одразу в канал",
        steps=json.dumps([
            {"step_name": "дослідження", "agent_type": "researcher", "depends_on": [],              "review_required": False},
            {"step_name": "чорновик",    "agent_type": "writer",     "depends_on": ["дослідження"], "review_required": False},
            {"step_name": "фінал",       "agent_type": "editor",     "depends_on": ["чорновик"],    "review_required": False},
        ]),
    )

    tg_story = Pipeline(
        id=new_id(), name="Telegram: Сторітелінг", platform="telegram",
        content_type="text",
        description="SEO-аналіз → Сторія → Редагування → [Перевірка]",
        steps=json.dumps([
            {"step_name": "аналіз",     "agent_type": "researcher", "depends_on": [],         "review_required": False},
            {"step_name": "сторія",     "agent_type": "writer",     "depends_on": ["аналіз"], "review_required": False},
            {"step_name": "шліфування", "agent_type": "editor",     "depends_on": ["сторія"], "review_required": True},
        ]),
    )

    for p in [tg_review, tg_auto, tg_story]:
        db.add(p)
    db.commit()
    print("✓ 3 пайплайни")
    return tg_review, tg_auto, tg_story


# ── Jobs ──────────────────────────────────────────────────────────────────────

def seed_jobs(tg_review, tg_auto, tg_story, platforms):
    platform = platforms[0]
    auto_platform = platforms[1]

    jobs_data = [
        # Готові до старту (pending)
        dict(pipeline=tg_review, status="pending", platform=platform, data={
            "topic": "Чому більшість людей не заробляють онлайн — і що з цим робити",
            "keywords": "онлайн заробіток, помилки, фриланс",
            "extra": "Тон: чесний, без мотиваційного спаму. Аудиторія: 20-35 років.",
        }),
        dict(pipeline=tg_auto, status="pending", platform=auto_platform, data={
            "topic": "5 речей які я б зробив по-іншому якби починав канал з нуля",
            "keywords": "telegram канал, помилки, ріст",
            "extra": "Від першої особи. Особистий досвід.",
        }),
        dict(pipeline=tg_story, status="pending", platform=platform, data={
            "topic": "Як один лист змінив мою кар'єру фрілансера",
            "keywords": "фриланс, кар'єра, клієнти, листи",
            "extra": "Особиста або вигадана, але реалістична історія.",
        }),
        dict(pipeline=tg_auto, status="pending", platform=auto_platform, data={
            "topic": "ChatGPT vs реальний копірайтер: чесне порівняння у 2025",
            "keywords": "ChatGPT, копірайтер, AI контент",
            "extra": "Без хайпу. Конкретні плюси і мінуси обох.",
        }),
        dict(pipeline=tg_review, status="pending", platform=platform, data={
            "topic": "Скільки реально можна заробити на Telegram каналі за рік",
            "keywords": "монетизація telegram, заробіток, канал",
            "extra": "Реальні цифри, не фантазії. Різні моделі монетизації.",
        }),
    ]

    created = []
    for jd in jobs_data:
        j = Job(
            id=new_id(), pipeline_id=jd["pipeline"].id,
            status=jd["status"],
            initial_input=json.dumps({
                "topic": jd["data"]["topic"],
                "keywords": jd["data"]["keywords"],
                "platform_id": jd["platform"].id,
                "extra": jd["data"].get("extra", ""),
            }),
        )
        db.add(j)
        created.append(j)

    db.commit()
    print(f"✓ {len(created)} задачі (pending — запускай у UI)")
    return created


# ── Schedules ─────────────────────────────────────────────────────────────────

def seed_schedules(tg_review, tg_auto, platforms):
    from croniter import croniter

    def next_run(expr):
        try:
            return croniter(expr, datetime.utcnow()).get_next(datetime)
        except Exception:
            return None

    platform = platforms[0]
    auto_platform = platforms[1]

    schedules = [
        Schedule(
            id=new_id(), name="Щоденний пост о 9:00",
            pipeline_id=tg_review.id,
            platform_id=platform.id,
            cron_expr="0 9 * * *",
            topic_template="Корисний пост на тему фрілансу — {date}",
            keywords="фриланс, поради, продуктивність",
            extra="Практичний тон, без теорії",
            active=False,
            next_run=next_run("0 9 * * *"),
        ),
        Schedule(
            id=new_id(), name="Автопілот Пн–Пт о 10:00",
            pipeline_id=tg_auto.id,
            platform_id=auto_platform.id,
            cron_expr="0 10 * * 1-5",
            topic_template="Ранковий пост для фрілансерів — {weekday} {date}",
            keywords="фриланс, ранок, мотивація",
            extra="Коротко і по суті. Бадьорий тон.",
            active=False,
            next_run=next_run("0 10 * * 1-5"),
        ),
    ]

    for s in schedules:
        db.add(s)
    db.commit()
    print(f"✓ {len(schedules)} розклади (вимкнені — активуй у UI)")
    return schedules


# ── Strategies ────────────────────────────────────────────────────────────────

def seed_strategies(tg_review, tg_auto, tg_story, platforms):
    platform = platforms[0]
    auto_platform = platforms[1]

    strategies = [
        Strategy(
            id=new_id(),
            name="Фриланс UA — з перевіркою",
            niche="фриланс та онлайн заробіток в Україні",
            audience="фрілансери-початківці та середній рівень, 22-35 років",
            content_pillars=json.dumps(["поради", "кейси", "типові помилки", "мотивація", "інструменти"]),
            tone="практичний, чесний, без мотиваційного спаму",
            pipeline_id=tg_review.id,
            platform_id=platform.id,
            frequency_per_week=3,
            auto_approve=False,
            active=True,
        ),
        Strategy(
            id=new_id(),
            name="AI та технології — автопілот",
            niche="штучний інтелект, нові технології, майбутнє роботи",
            audience="tech-спеціалісти та цікавці, 25-40 років",
            content_pillars=json.dumps(["новини AI", "практичне застосування", "порівняння інструментів", "прогнози"]),
            tone="інформативний, аналітичний, без хайпу",
            pipeline_id=tg_auto.id,
            platform_id=auto_platform.id,
            frequency_per_week=5,
            auto_approve=True,
            active=True,
        ),
        Strategy(
            id=new_id(),
            name="Telegram-маркетинг — сторітелінг",
            niche="розвиток Telegram каналів та монетизація",
            audience="власники каналів та маркетологи",
            content_pillars=json.dumps(["зростання аудиторії", "монетизація", "контент-стратегія", "кейси каналів"]),
            tone="від першої особи, реальні приклади, конкретні цифри",
            pipeline_id=tg_story.id,
            platform_id=platform.id,
            frequency_per_week=2,
            auto_approve=False,
            active=True,
        ),
    ]

    for s in strategies:
        db.add(s)
    db.flush()  # щоб отримати id до топіків

    # Тестові теми для першої стратегії
    freelance_topics = [
        ("Як підняти ставку клієнту якого маєш вже рік", "ставка, переговори, клієнт", "pending"),
        ("Upwork vs Fiverr vs своя аудиторія — де фрілансеру легше стартувати", "upwork, fiverr, платформи", "approved"),
        ("Портфоліо без кейсів: що показати клієнту коли ти новачок", "портфоліо, початок, кейси", "pending"),
        ("3 ознаки що клієнт буде проблемним — помічаємо на старті", "клієнт, червоні прапори, фриланс", "approved"),
        ("Скільки часу займає вийти на $1000/міс з нуля: реальна статистика", "1000 долар, дохід, фриланс", "launched"),
        ("Як я втратив найкращого клієнта через одну помилку в листі", "клієнт, комунікація, помилка", "rejected"),
    ]

    for topic, keywords, status in freelance_topics:
        db.add(TopicSuggestion(
            id=new_id(), strategy_id=strategies[0].id,
            topic=topic, keywords=keywords, status=status,
        ))

    # Тестові теми для другої стратегії (AI)
    ai_topics = [
        ("Claude 3.5 vs GPT-4o: яку модель обрати для щоденної роботи у 2025", "claude, gpt-4o, порівняння", "approved"),
        ("Як AI змінить ринок фрілансу за наступні 2 роки — чесний прогноз", "AI, фриланс, майбутнє", "pending"),
        ("Cursor vs GitHub Copilot: що насправді прискорює кодинг", "cursor, copilot, AI-кодинг", "launched"),
        ("Промпт-інженерія за 10 хвилин: головні прийоми що одразу дають результат", "промпт, AI, інструкція", "pending"),
    ]

    for topic, keywords, status in ai_topics:
        db.add(TopicSuggestion(
            id=new_id(), strategy_id=strategies[1].id,
            topic=topic, keywords=keywords, status=status,
        ))

    db.commit()
    print(f"✓ {len(strategies)} стратегії + {len(freelance_topics) + len(ai_topics)} тем")
    return strategies


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\nSeed...\n")
    clean()
    agents   = seed_agents()
    platforms = seed_platforms()
    tg_review, tg_auto, tg_story = seed_pipelines()

    try:
        seed_schedules(tg_review, tg_auto, platforms)
    except ImportError:
        print("⚠️  croniter не встановлено — розклади пропущено (pip install croniter)")

    seed_jobs(tg_review, tg_auto, tg_story, platforms)
    seed_strategies(tg_review, tg_auto, tg_story, platforms)
    db.close()

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    or_key   = os.getenv("OPENROUTER_API_KEY", "")

    print(f"""
═══════════════════════════════════════════════════════
✅ Готово!

АГЕНТИ:         5 (Дослідник, Автор, Редактор, SEO-дослідник, Сторітелер)
ПАЙПЛАЙНИ:      3 (з перевіркою / автопілот / сторітелінг)
ПЛАТФОРМИ:      2 (ручна + авто-публікація)
ЗАДАЧІ:         5 (pending — запускай у вкладці Задачі)
РОЗКЛАДИ:       2 (вимкнені — активуй у вкладці Розклад)
СТРАТЕГІЇ:      3 (з темами на різних статусах)

КЛЮЧІ:
  OPENROUTER_API_KEY  {"✅ знайдено" if or_key else "❌ НЕ встановлено — задачі не виконуватимуться!"}
  TELEGRAM_BOT_TOKEN  {"✅ знайдено" if tg_token else "⚠️  не встановлено — Telegram не працюватиме"}

ЗАПУСК:
  python main.py → http://localhost:8000

ЩО ЗРОБИТИ ДАЛІ:
  1. Вкладка «Задачі» → натисни Старт на будь-якій задачі
  2. Вкладка «Стратегія» → обери стратегію → «Генерувати 5 тем»
  3. Вкладка «Розклад» → активуй і натисни «▷ Зараз» для тесту
═══════════════════════════════════════════════════════
""")
