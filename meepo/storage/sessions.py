"""Сессии и их история без базы данных.

- `data/sessions/<id>.json` — сессия: заголовок, проект, доверенные группы, план, ожидающая карточка, заполненность окна.
- `data/conversations/<id>.jsonl` — история: одна строка — одно сообщение.

Сообщение: {"id", "role", "content", "created_at"} и по необходимости "tool_calls" (у ответа модели),
"tool_call_id" и "tool_name" (у результата инструмента), "is_error", "meta" (данные для UI, модели не уходят),
"reasoning", "stats".
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_TITLE = "Новая сессия"
TITLE_CHARS = 48


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, data: dict) -> None:
    """Запись через временный файл: оборванная запись не портит прежний файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionStore:
    def __init__(self, data_path: Path) -> None:
        self.sessions_dir = data_path / "sessions"
        self.history_dir = data_path / "conversations"
        self._lock = RLock()

    def create(self, project_id: str | None, title: str = DEFAULT_TITLE) -> dict:
        stamp = now()
        session = {
            "id": uuid4().hex, "title": title, "project_id": project_id, "created_at": stamp, "updated_at": stamp,
            "trusted": [], "plan": [], "plan_request": None, "pending": None,
            "context_tokens": None, "context_window": None,
        }
        with self._lock:
            write_json(self._path(session["id"]), session)
        return session

    def get(self, session_id: str) -> dict | None:
        try:
            path = self._path(session_id)
        except ValueError:
            return None
        with self._lock:
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        with self._lock:
            if not self.sessions_dir.is_dir():
                return []
            sessions = [json.loads(path.read_text(encoding="utf-8")) for path in self.sessions_dir.glob("*.json")]
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)

    def update(self, session_id: str, **fields) -> dict | None:
        """Читает и перезаписывает сессию под одной блокировкой, чтобы параллельные правки не терялись."""
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return None
            session.update(fields, updated_at=now())
            write_json(self._path(session_id), session)
            return session

    def delete(self, session_id: str) -> bool:
        with self._lock:
            path = self._path(session_id)
            if not path.is_file():
                return False
            path.unlink()
            self._history(session_id).unlink(missing_ok=True)
            return True

    def trust(self, session_id: str, group: str) -> None:
        with self._lock:
            session = self.get(session_id)
            if session is not None and group not in session["trusted"]:
                self.update(session_id, trusted=[*session["trusted"], group])

    def add_message(self, session_id: str, role: str, content: str = "", **extra) -> dict:
        message = {"id": uuid4().hex, "role": role, "content": content, "created_at": now()}
        message.update({key: value for key, value in extra.items() if value not in (None, "", [], {}, False)})
        with self._lock:
            path = self._history(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(message, ensure_ascii=False) + "\n")
            session = self.get(session_id)
            if session is not None:
                fields = {}
                # Заголовок новой сессии — по первой реплике пользователя.
                if role == "user" and session["title"] == DEFAULT_TITLE and content.strip():
                    title = " ".join(content.split())
                    fields["title"] = title if len(title) <= TITLE_CHARS else title[:TITLE_CHARS - 1].rstrip() + "…"
                self.update(session_id, **fields)
        return message

    def messages(self, session_id: str) -> list[dict]:
        with self._lock:
            path = self._history(session_id)
            if not path.is_file():
                return []
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _path(self, session_id: str) -> Path:
        if not SAFE_ID.fullmatch(session_id):
            raise ValueError("Недопустимый ID сессии")
        return self.sessions_dir / f"{session_id}.json"

    def _history(self, session_id: str) -> Path:
        if not SAFE_ID.fullmatch(session_id):
            raise ValueError("Недопустимый ID сессии")
        return self.history_dir / f"{session_id}.jsonl"
