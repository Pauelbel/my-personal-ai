"""Сервис проектов создаёт и меняет проекты и связывает с ними сессии."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from local_agent.projects.models import Project
from local_agent.sessions.service import InvalidWorkspaceError, SessionService, validate_workspace
from local_agent.storage.json.projects import JsonProjectRepository

logger = logging.getLogger(__name__)

# Папку выбирают, чтобы агент мог её читать: чтение включено сразу. Запись и правка — только вручную.
DEFAULT_PROJECT_TOOLS = ("git_log", "git_show", "list_files", "read_file", "search_files")


class ProjectError(ValueError):
    """Проект нельзя создать или изменить с такими параметрами."""


class ProjectService:
    def __init__(self, repository: JsonProjectRepository, sessions: SessionService) -> None:
        self._repository = repository
        self._sessions = sessions

    def list(self) -> list[Project]:
        return self._repository.list()

    def get(self, project_id: str) -> Project | None:
        return self._repository.get(project_id)

    def create(
        self, *, name: str, workspace: str, agent_id: str, provider: str, model: str,
        enabled_tools: list[str] | None = None,
    ) -> Project:
        workspace = self._workspace(workspace)
        now = datetime.now(UTC)
        return self._repository.save(Project(
            id=uuid4().hex, name=name, workspace=workspace, created_at=now, updated_at=now,
            agent_id=agent_id, provider=provider, model=model,
            enabled_tools=list(DEFAULT_PROJECT_TOOLS) if enabled_tools is None else enabled_tools,
        ))

    def for_workspace(self, workspace: str, *, agent_id: str, provider: str, model: str) -> Project:
        """Проект этой папки; если его нет — создаётся с именем папки."""
        try:
            resolved = validate_workspace(workspace)
        except InvalidWorkspaceError as exc:
            raise ProjectError(str(exc)) from exc
        for project in self._repository.list():
            if project.workspace == resolved:
                return project
        return self.create(
            name=Path(resolved).name or resolved, workspace=resolved,
            agent_id=agent_id, provider=provider, model=model,
        )

    def update(self, project_id: str, *, name: str, workspace: str) -> Project | None:
        if self._repository.get(project_id) is None:
            return None
        return self._repository.patch(project_id, {
            "name": name,
            "workspace": self._workspace(workspace, ignore=project_id),
            "updated_at": datetime.now(UTC),
        })

    def delete(self, project_id: str) -> bool:
        """Сессии проекта не удаляются: они остаются в разделе «Без проекта», но без доступа к папке."""
        if self._repository.get(project_id) is None:
            return False
        for session in self._sessions.list():
            if session.project_id == project_id:
                self._sessions.assign_project(session.id, None)
        return self._repository.delete(project_id)

    def adopt_sessions(self) -> None:
        """Сессии со своей рабочей папкой (созданные до проектов) переходят в проект этой папки."""
        by_folder = {project.workspace: project for project in self._repository.list()}
        for session in self._sessions.list():
            if session.project_id or not session.workspace:
                continue
            project = by_folder.get(session.workspace)
            if project is None:
                try:
                    # Список сессий отсортирован от новых к старым: настройки берём у самой свежей.
                    project = self.create(
                        name=Path(session.workspace).name or session.workspace,
                        workspace=session.workspace, agent_id=session.agent_id,
                        provider=session.provider, model=session.model,
                        enabled_tools=list(session.enabled_tools),
                    )
                except ProjectError:
                    logger.warning("Папка сессии недоступна, проект не создан session_id=%s", session.id)
                    continue
                by_folder[project.workspace] = project
            self._sessions.assign_project(session.id, project.id)

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
