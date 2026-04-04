"""
Obsidian Knowledge Base Integration.

Автоматично записує виходи агентів у Obsidian vault у вигляді
пов'язаних Markdown-нотаток з backlinks для граф-вью.

Конфігурація (.env):
    OBSIDIAN_VAULT_PATH=C:/Users/username/Documents/MyVault

Якщо OBSIDIAN_VAULT_PATH не вказано — всі методи повертають None без помилок.

Структура у vault:
    AI Orchestrator/
    ├── _Index.md
    ├── Jobs/         — один .md на Job
    ├── Published/    — кожна публікація
    ├── Topics/       — автоматичні MOC-ноди
    └── Pipelines/    — шаблони пайплайнів
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Іконки для типів агентів
_AGENT_ICONS = {
    "researcher": "🔍",
    "writer":     "✍️",
    "editor":     "✂️",
    "publisher":  "📤",
    "image":      "🖼",
    "voice":      "🎙",
    "custom":     "⚙️",
}

# Символи що не можна використовувати у назвах файлів (Windows + Unix)
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(text: str, max_len: int = 60) -> str:
    """Зробити безпечну назву файлу з довільного рядка."""
    text = _UNSAFE_CHARS.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len].rstrip(". ")


def _tags_from_keywords(keywords: str) -> list[str]:
    """Перетворити рядок ключових слів у список тегів Obsidian."""
    if not keywords:
        return []
    tags = []
    for kw in keywords.split(","):
        kw = kw.strip().lower()
        kw = re.sub(r"\s+", "-", kw)
        kw = re.sub(r"[^\w\u0400-\u04ff-]", "", kw)  # літери + кирилиця + дефіс
        if kw:
            tags.append(kw)
    return tags


def _topic_links(keywords: str) -> list[str]:
    """Повернути список [[Topics/Тег]] посилань з ключових слів."""
    links = []
    for kw in keywords.split(","):
        kw = kw.strip()
        if kw:
            safe = _safe_filename(kw, 50)
            links.append(f"[[Topics/{safe}]]")
    return links


class ObsidianWriter:
    def __init__(self):
        vault_path = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
        self.enabled = bool(vault_path)
        if self.enabled:
            self.base = Path(vault_path) / "AI Orchestrator"
            self._ensure_dirs()
        else:
            self.base = None

    def _ensure_dirs(self):
        for sub in ("Jobs", "Published", "Topics", "Pipelines"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)

    # ─────────────────────────── Public API ──────────────────────────────────

    def write_job_note(self, db, job_id: str) -> str | None:
        """
        Створити / оновити нотатку для завершеного Job.
        Повертає відносний шлях всередині vault або None.
        """
        if not self.enabled:
            return None
        try:
            from database import Job, Task, Pipeline, Platform, Agent
            job   = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                return None
            tasks = db.query(Task).filter(Task.job_id == job_id).order_by(Task.created_at).all()

            initial   = json.loads(job.initial_input or "{}")
            topic     = initial.get("topic", "Без теми")
            keywords  = initial.get("keywords", "")
            extra     = initial.get("extra", "")
            platform_id = initial.get("platform_id", "")

            pipeline = db.query(Pipeline).filter(Pipeline.id == job.pipeline_id).first()
            pipeline_name = pipeline.name if pipeline else "?"

            platform_type = ""
            if platform_id:
                pl = db.query(Platform).filter(Platform.id == platform_id).first()
                platform_type = pl.type if pl else ""

            date_str  = (job.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
            fname     = f"{date_str} {_safe_filename(topic)}.md"
            fpath     = self.base / "Jobs" / fname
            rel_path  = f"Jobs/{fname}"

            # Frontmatter tags
            tags = ["ai-content"]
            if platform_type:
                tags.append(platform_type)
            tags.extend(_tags_from_keywords(keywords))

            # Topic backlinks
            topic_links = _topic_links(keywords)
            topic_line  = " · ".join(topic_links) if topic_links else "_без тегів_"

            # Pipeline safe name
            pip_safe = _safe_filename(pipeline_name, 80)

            # Build tasks sections
            task_sections = []
            for task in tasks:
                agent = db.query(Agent).filter(Agent.id == task.agent_id).first() if task.agent_id else None
                icon   = _AGENT_ICONS.get(task.agent_type, "•")
                ts_str = ""
                if task.completed_at:
                    ts_str = task.completed_at.strftime("%Y-%m-%d %H:%M")

                review_mark = ""
                if task.status == "approved":
                    review_mark = " · ✅ схвалено"
                elif task.status == "error":
                    review_mark = " · ❌ помилка"

                meta_parts = []
                if agent:
                    meta_parts.append(agent.name)
                    meta_parts.append(f"`{agent.model}`")
                if ts_str:
                    meta_parts.append(ts_str)
                meta_line = " · ".join(meta_parts) + review_mark

                output_text = ""
                if task.output_data:
                    data = json.loads(task.output_data)
                    output_text = data.get("result", "")

                error_block = ""
                if task.error_message:
                    error_block = f"\n> [!error] Помилка\n> {task.error_message}\n"

                section = f"## {icon} {task.step_name}\n*{meta_line}*\n{error_block}\n{output_text}"
                task_sections.append(section)

            tasks_md = "\n\n---\n\n".join(task_sections) if task_sections else "_Кроків ще немає_"

            # Completed timestamp
            done_str = ""
            if job.completed_at:
                done_str = job.completed_at.strftime("%Y-%m-%d %H:%M")

            content = f"""---
job_id: {job_id}
title: "{topic}"
topic: "{topic}"
keywords: "{keywords}"
pipeline: "{pipeline_name}"
platform: "{platform_type}"
status: {job.status}
created: {date_str}
completed: "{done_str}"
tags: [{', '.join(tags)}]
---

# {topic}

**Pipeline:** [[Pipelines/{pip_safe}]]
**Topics:** {topic_line}
"""
            if extra:
                content += f"**Контекст:** {extra}\n"

            content += f"""
---

{tasks_md}
"""

            fpath.write_text(content, encoding="utf-8")
            logger.info("📝 Obsidian: Job note → %s", rel_path)

            # Оновити Topic MOC-и
            self._update_topic_notes(keywords, rel_path, topic, date_str)

            # Оновити Pipeline note
            self._ensure_pipeline_note(pipeline_name, rel_path, topic, date_str)

            # Оновити Index
            self._update_index(db)

            return rel_path

        except Exception:
            logger.exception("Obsidian write_job_note failed for job %s", job_id[:8])
            return None

    def write_published_note(self, job_id: str, channel_id: str, content: str) -> str | None:
        """Записати нотатку про публікацію в Telegram/іншій платформі."""
        if not self.enabled:
            return None
        try:
            from database import SessionLocal, Job
            db = SessionLocal()
            try:
                job = db.query(Job).filter(Job.id == job_id).first()
                initial = json.loads(job.initial_input or "{}") if job else {}
            finally:
                db.close()

            topic    = initial.get("topic", "Без теми")
            keywords = initial.get("keywords", "")
            now      = datetime.utcnow()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M")
            channel_safe = _safe_filename(str(channel_id), 30)
            fname    = f"{date_str} {channel_safe}.md"
            fpath    = self.base / "Published" / fname
            rel_path = f"Published/{fname}"

            # Job note backlink
            job_date  = date_str
            job_fname = f"{job_date} {_safe_filename(topic)}.md"
            job_link  = f"[[Jobs/{job_fname[:-3]}]]"

            topic_links = _topic_links(keywords)
            topic_line  = " · ".join(topic_links) if topic_links else ""

            note = f"""---
published: "{date_str} {time_str}"
channel: "{channel_id}"
job_id: {job_id}
topic: "{topic}"
tags: [published, telegram]
---

# Публікація {date_str}

**Канал:** `{channel_id}`
**Час:** {date_str} {time_str}
**Джерело:** {job_link}
{f'**Topics:** {topic_line}' if topic_line else ''}

---

{content}
"""
            fpath.write_text(note, encoding="utf-8")
            logger.info("📝 Obsidian: Published note → %s", rel_path)

            # Додати backlink у Job note
            self._append_published_to_job(job_id, rel_path, channel_id, f"{date_str} {time_str}")

            return rel_path

        except Exception:
            logger.exception("Obsidian write_published_note failed for job %s", job_id[:8])
            return None

    # ─────────────────────────── Internal helpers ─────────────────────────────

    def _update_topic_notes(self, keywords: str, job_rel_path: str, topic: str, date_str: str):
        """Додати посилання на Job у кожен відповідний Topic MOC."""
        for kw in keywords.split(","):
            kw = kw.strip()
            if not kw:
                continue
            safe  = _safe_filename(kw, 50)
            fpath = self.base / "Topics" / f"{safe}.md"
            link_line = f"- [[{job_rel_path[:-3]}]] — {topic} ({date_str})\n"

            if fpath.exists():
                existing = fpath.read_text(encoding="utf-8")
                # Не дублювати якщо вже є
                if job_rel_path[:-3] in existing:
                    continue
                fpath.write_text(existing + link_line, encoding="utf-8")
            else:
                fpath.write_text(
                    f"---\ntitle: \"{kw}\"\ntype: topic\ntags: [topic]\n---\n\n"
                    f"# {kw}\n\n## Матеріали\n\n{link_line}",
                    encoding="utf-8",
                )

    def _ensure_pipeline_note(self, pipeline_name: str, job_rel_path: str, topic: str, date_str: str):
        """Додати запуск у Pipeline note."""
        safe  = _safe_filename(pipeline_name, 80)
        fpath = self.base / "Pipelines" / f"{safe}.md"
        link_line = f"- [[{job_rel_path[:-3]}]] — {topic} ({date_str})\n"

        if fpath.exists():
            existing = fpath.read_text(encoding="utf-8")
            if job_rel_path[:-3] in existing:
                return
            fpath.write_text(existing + link_line, encoding="utf-8")
        else:
            fpath.write_text(
                f"---\ntitle: \"{pipeline_name}\"\ntype: pipeline\ntags: [pipeline]\n---\n\n"
                f"# {pipeline_name}\n\n## Запуски\n\n{link_line}",
                encoding="utf-8",
            )

    def _append_published_to_job(self, job_id: str, pub_rel_path: str, channel_id: str, ts: str):
        """Додати секцію 'Опубліковано' у кінець Job note після публікації."""
        try:
            from database import SessionLocal, Job
            db = SessionLocal()
            try:
                job = db.query(Job).filter(Job.id == job_id).first()
                initial = json.loads(job.initial_input or "{}") if job else {}
            finally:
                db.close()

            topic    = initial.get("topic", "")
            date_str = (datetime.utcnow()).strftime("%Y-%m-%d")
            fname    = f"{date_str} {_safe_filename(topic)}.md"
            fpath    = self.base / "Jobs" / fname

            if not fpath.exists():
                return

            existing = fpath.read_text(encoding="utf-8")
            pub_link = f"[[{pub_rel_path[:-3]}]]"
            if pub_link in existing:
                return

            addition = f"\n\n---\n\n## 📤 Опубліковано\n*Канал: `{channel_id}` · {ts}*\n\n{pub_link}\n"
            fpath.write_text(existing + addition, encoding="utf-8")
        except Exception:
            logger.exception("Obsidian _append_published_to_job failed")

    def _update_index(self, db):
        """Оновити _Index.md з актуальною статистикою."""
        try:
            from database import Job, Task
            total_jobs  = db.query(Job).count()
            done_jobs   = db.query(Job).filter(Job.status == "done").count()
            total_tasks = db.query(Task).count()
            now_str     = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

            recent_jobs = (
                db.query(Job)
                .filter(Job.status == "done")
                .order_by(Job.completed_at.desc())
                .limit(10)
                .all()
            )
            recent_lines = []
            for j in recent_jobs:
                inp   = json.loads(j.initial_input or "{}")
                topic = inp.get("topic", "?")
                d     = (j.completed_at or j.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
                safe  = _safe_filename(topic)
                recent_lines.append(f"- [[Jobs/{d} {safe}]] — {topic}")

            recent_md = "\n".join(recent_lines) or "_нічого_"

            content = f"""---
updated: "{now_str}"
tags: [index, ai-orchestrator]
---

# AI Orchestrator — Сховище знань

> Оновлено: {now_str}

## Статистика

| Показник | Значення |
|---|---|
| Задач завершено | {done_jobs} / {total_jobs} |
| Кроків виконано | {total_tasks} |

## Останні матеріали

{recent_md}

## Навігація

- [[Topics/]] — всі теми (граф-вью)
- [[Pipelines/]] — шаблони пайплайнів
- [[Published/]] — публікації

"""
            (self.base / "_Index.md").write_text(content, encoding="utf-8")
        except Exception:
            logger.exception("Obsidian _update_index failed")

    def sync_all_jobs(self, db) -> int:
        """Синхронізувати всі завершені jobs у vault. Повертає кількість записаних."""
        if not self.enabled:
            return 0
        from database import Job
        jobs = db.query(Job).filter(Job.status == "done").all()
        count = 0
        for job in jobs:
            result = self.write_job_note(db, job.id)
            if result:
                count += 1
        return count


# Singleton
_writer: ObsidianWriter | None = None


def get_writer() -> ObsidianWriter:
    global _writer
    if _writer is None:
        _writer = ObsidianWriter()
    return _writer
