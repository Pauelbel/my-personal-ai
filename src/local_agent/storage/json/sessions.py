"""JSON-репозиторий хранит метаданные каждой сессии в отдельном читаемом файле."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from local_agent.sessions.models import DEFAULT_SESSION_TITLE, Session

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonSessionRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = RLock()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, session: Session) -> Session:
        path = self._path(session.id)
        with self._lock:
            self.initialize()
            if path.exists():
                raise ValueError("Сессия уже существует")
            self._write(path, session)
        return session

    def get(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        with self._lock:
            if not path.exists():
                return None
            return Session.model_validate_json(path.read_text(encoding="utf-8"))

    def update(self, session: Session) -> Session:
        path = self._path(session.id)
        with self._lock:
            if not path.exists():
                raise ValueError("Сессия не найдена")
            self._write(path, session)
        return session

    def patch(self, session_id: str, updates: dict[str, object]) -> Session | None:
        """Читает и перезаписывает сессию под одной блокировкой, чтобы параллельные правки не терялись."""
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return None
            return self.update(session.model_copy(update=updates))

    def rename(
        self, session_id: str, title: str, only_if_default: bool
    ) -> Session | None:
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return None
            if only_if_default and session.title != DEFAULT_SESSION_TITLE:
                return session
            return self.update(session.model_copy(update={
                "title": title,
                "updated_at": datetime.now(UTC),
            }))

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            return True

    def set_context_tokens(self, session_id: str, count: int | None) -> None:
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return
            self.update(session.model_copy(update={
                "context_tokens": count,
                "updated_at": datetime.now(UTC),
            }))

    def touch(self, session_id: str, updated_at: datetime) -> None:
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return
            self.update(session.model_copy(update={"updated_at": updated_at}))

    def list(self) -> list[Session]:
        with self._lock:
            self.initialize()
            sessions = [
                Session.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self.root.glob("*.json")
                if not path.name.startswith(".")
            ]
        return sorted(sessions, key=lambda item: (item.updated_at, item.id), reverse=True)

    def _path(self, session_id: str) -> Path:
        if not SAFE_SESSION_ID.fullmatch(session_id):
            raise ValueError("Недопустимый ID сессии")
        return self.root / f"{session_id}.json"

    @staticmethod
    def _write(path: Path, session: Session) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                session.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
