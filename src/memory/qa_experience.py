"""Структурированная память повторно применимого QA-опыта по проектам."""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

STATUSES = ("observation", "confirmed", "obsolete")
WORDS = re.compile(r"[\w./:-]+", re.UNICODE)


class QAExperience(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    project: str
    date: str
    task: str
    source: str
    status: str
    applies_to: str
    actions: str
    result: str
    evidence: str = ""
    user_correction: str = ""

    def compact(self) -> str:
        proof = self.evidence or self.user_correction
        return (
            f"{self.task}. Применимо: {self.applies_to}. Действия: {self.actions}. "
            f"Результат: {self.result}. Подтверждение: {proof}. Источник: {self.source}; {self.date}; id={self.id}"
        )


class QAExperienceStore:
    """SQLite-хранилище; выборка всегда ограничена проектом и пятью результатами."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS experience (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project TEXT NOT NULL, date TEXT NOT NULL,
                    task TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, applies_to TEXT NOT NULL,
                    actions TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT NOT NULL,
                    user_correction TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experience_project_status
                    ON experience(project_id, status, date DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS experience_identity
                    ON experience(project_id, task COLLATE NOCASE, applies_to COLLATE NOCASE);
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _item(row: sqlite3.Row | None) -> QAExperience | None:
        return QAExperience.model_validate(dict(row)) if row is not None else None

    def save(
        self, *, project_id: str, project: str, task: str, source: str, status: str,
        applies_to: str, actions: str, result: str, evidence: str = "", user_correction: str = "",
        record_id: str = "",
    ) -> QAExperience:
        values = {
            "project_id": project_id, "project": project, "task": task, "source": source,
            "applies_to": applies_to, "actions": actions, "result": result,
        }
        if any(not value.strip() for value in values.values()):
            raise ValueError("Все обязательные поля QA-опыта должны быть заполнены")
        if status not in STATUSES:
            raise ValueError(f"Неизвестный статус: {status}")
        if status == "confirmed" and not (evidence.strip() or user_correction.strip()):
            raise ValueError("confirmed требует доказательство или явное исправление пользователя")
        with self._lock, self._connect() as connection:
            existing = self._item(connection.execute(
                "SELECT * FROM experience WHERE id = ? AND project_id = ?", (record_id, project_id),
            ).fetchone()) if record_id else self._item(connection.execute(
                "SELECT * FROM experience WHERE project_id = ? AND task = ? COLLATE NOCASE "
                "AND applies_to = ? COLLATE NOCASE",
                (project_id, task.strip(), applies_to.strip()),
            ).fetchone())
            if record_id and existing is None:
                raise ValueError("Запись QA-опыта этого проекта не найдена")
            item = QAExperience(
                id=existing.id if existing else uuid4().hex,
                date=date.today().isoformat(), status=status,
                evidence=evidence.strip(), user_correction=user_correction.strip(),
                **{key: value.strip() for key, value in values.items()},
            )
            columns = tuple(item.model_dump())
            values_list = tuple(item.model_dump().values())
            connection.execute(
                f"INSERT OR REPLACE INTO experience ({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                values_list,
            )
            return item

    def search(
        self, project_id: str, query: str, *, limit: int = 5, confirmed_only: bool = False,
    ) -> list[QAExperience]:
        terms = {word.casefold() for word in WORDS.findall(query) if len(word) > 2}
        status = "AND status = 'confirmed'" if confirmed_only else "AND status != 'obsolete'"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM experience WHERE project_id = ? {status}", (project_id,),
            ).fetchall()
        candidates = []
        for row in rows:
            item = self._item(row)
            text = " ".join((item.task, item.source, item.applies_to, item.actions, item.result)).casefold()
            score = sum(1 for term in terms if term in text)
            if score or not terms:
                candidates.append((score, item.date, item))
        candidates.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [item for _score, _date, item in candidates[:max(0, min(limit, 5))]]
