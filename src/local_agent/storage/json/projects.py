"""JSON-репозиторий хранит каждый проект в отдельном читаемом файле."""

from __future__ import annotations

import os
import re
from pathlib import Path
from threading import RLock
from uuid import uuid4

from local_agent.projects.models import Project

SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonProjectRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = RLock()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, project: Project) -> Project:
        path = self._path(project.id)
        with self._lock:
            self.initialize()
            if path.exists():
                raise ValueError("Проект уже существует")
            self._write(path, project)
        return project

    def get(self, project_id: str) -> Project | None:
        try:
            path = self._path(project_id)
        except ValueError:
            return None
        with self._lock:
            if not path.exists():
                return None
            return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def patch(self, project_id: str, updates: dict[str, object]) -> Project | None:
        with self._lock:
            project = self.get(project_id)
            if project is None:
                return None
            updated = project.model_copy(update=updates)
            self._write(self._path(project_id), updated)
            return updated

    def delete(self, project_id: str) -> bool:
        path = self._path(project_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            return True

    def list(self) -> list[Project]:
        with self._lock:
            self.initialize()
            projects = [
                Project.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self.root.glob("*.json")
                if not path.name.startswith(".")
            ]
        return sorted(projects, key=lambda item: (item.name.casefold(), item.id))

    def _path(self, project_id: str) -> Path:
        if not SAFE_PROJECT_ID.fullmatch(project_id):
            raise ValueError("Недопустимый ID проекта")
        return self.root / f"{project_id}.json"

    @staticmethod
    def _write(path: Path, project: Project) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(project.model_dump_json(indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
