"""Сервис проектов создаёт и меняет проекты и связывает с ними сессии.

Каждая сессия живёт в проекте: без проекта сессию не создать, с удалением проекта удаляются и его сессии.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from projects.models import Project
from projects.repository import ProjectRepository

# Папку выбирают, чтобы агент мог её читать: чтение включено сразу. Запись и правка — только вручную.


class ProjectError(ValueError):
    """Проект нельзя создать или изменить с такими параметрами."""


class InvalidWorkspaceError(ValueError):
    """Рабочая папка не существует или указана относительным путём."""


def validate_workspace(workspace: str) -> str:
    path = Path(workspace).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise InvalidWorkspaceError("Рабочая папка должна быть существующей папкой с абсолютным путём")
    return str(path.resolve())


class Sessions(Protocol):
    """Что сервису проектов нужно от сессий: проекты лежат слоем ниже и сам пакет sessions не импортируют."""

    def list(self) -> list: ...

    def delete(self, session_id: str) -> bool: ...



class ProjectService:
    def __init__(self, repository: ProjectRepository, sessions: Sessions) -> None:
        self._repository = repository
        self._sessions = sessions

    def list(self) -> list[Project]:
        return self._repository.list()

    def get(self, project_id: str) -> Project | None:
        return self._repository.get(project_id)

    def create(
        self, *, name: str, workspace: str, provider: str, model: str,
    ) -> Project:
        workspace = self._workspace(workspace)
        now = datetime.now(UTC)
        return self._repository.save(Project(
            id=uuid4().hex, name=name.strip() or folder_name(workspace), workspace=workspace, created_at=now, updated_at=now,
            provider=provider, model=model,
        ))

    def update(self, project_id: str, *, name: str, workspace: str, mode: str | None = None) -> Project | None:
        if self._repository.get(project_id) is None:
            return None
        resolved = self._workspace(workspace, ignore=project_id)
        updates: dict[str, object] = {
            "name": name.strip() or folder_name(resolved),
            "workspace": resolved,
            "updated_at": datetime.now(UTC),
        }
        if mode is not None:
            updates["mode"] = mode
        return self._repository.patch(project_id, updates)

    def delete(self, project_id: str) -> bool:
        """Проект удаляется вместе с сессиями и их историей; папка на диске остаётся."""
        if self._repository.get(project_id) is None:
            return False
        for session in self._sessions.list():
            if session.project_id == project_id:
                self._sessions.delete(session.id)
        return self._repository.delete(project_id)

    def _workspace(self, workspace: str, ignore: str | None = None) -> str:
        try:
            resolved = validate_workspace(workspace)
        except InvalidWorkspaceError as exc:
            raise ProjectError(str(exc)) from exc
        # Две записи на одну папку путали бы, к какому проекту относится сессия.
        for project in self._repository.list():
            if project.workspace == resolved and project.id != ignore:
                raise ProjectError(f"Для этой папки уже есть проект «{project.name}»")
        return resolved


def folder_name(workspace: str) -> str:
    """Название проекта по умолчанию — имя его папки."""
    return Path(workspace).name or workspace
