"""Проекты: папки, в которых работает агент. `data/projects/<id>.json` — {"id", "name", "path", "created_at"}."""

import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

from meepo.storage.sessions import SAFE_ID, now, write_json


def check_folder(path: str) -> Path:
    """Папка проекта — существующая папка, путь абсолютный."""
    folder = Path(path.strip()).expanduser()
    if not folder.is_absolute():
        raise ValueError("Укажите полный путь к папке")
    if not folder.is_dir():
        raise ValueError("Папка не найдена")
    return folder.resolve()


class ProjectStore:
    def __init__(self, data_path: Path) -> None:
        self.root = data_path / "projects"
        self._lock = RLock()

    def create(self, name: str, path: str) -> dict:
        folder = check_folder(path)
        project = {"id": uuid4().hex, "name": name.strip() or folder.name, "path": str(folder), "created_at": now()}
        with self._lock:
            write_json(self._path(project["id"]), project)
        return project

    def get(self, project_id: str | None) -> dict | None:
        if not project_id or not SAFE_ID.fullmatch(project_id):
            return None
        with self._lock:
            path = self._path(project_id)
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def list(self) -> list[dict]:
        with self._lock:
            if not self.root.is_dir():
                return []
            projects = [json.loads(path.read_text(encoding="utf-8")) for path in self.root.glob("*.json")]
        return sorted(projects, key=lambda item: item["name"].casefold())

    def update(self, project_id: str, name: str, path: str) -> dict | None:
        folder = check_folder(path)
        with self._lock:
            project = self.get(project_id)
            if project is None:
                return None
            project.update(name=name.strip() or folder.name, path=str(folder))
            write_json(self._path(project_id), project)
            return project

    def delete(self, project_id: str) -> bool:
        """Удаляет только запись о проекте; сама папка на диске остаётся."""
        with self._lock:
            path = self._path(project_id)
            if not path.is_file():
                return False
            path.unlink()
            return True

    def _path(self, project_id: str) -> Path:
        if not SAFE_ID.fullmatch(project_id):
            raise ValueError("Недопустимый ID проекта")
        return self.root / f"{project_id}.json"
