"""Контракт хранилища проектов: сервисы работают с ним, а не с конкретным форматом (storage/json)."""

from typing import Protocol

from projects.models import Project


class ProjectRepository(Protocol):
    def save(self, project: Project) -> Project: ...

    def get(self, project_id: str) -> Project | None: ...

    def patch(self, project_id: str, updates: dict[str, object]) -> Project | None: ...

    def delete(self, project_id: str) -> bool: ...

    def list(self) -> list[Project]: ...
