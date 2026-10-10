"""Сервис проектов создаёт и меняет проекты и связывает с ними сессии.

Каждая сессия живёт в проекте; системный проект «Черновики» принимает сессии, у которых своего нет.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from local_agent.projects.models import DRAFTS_PROJECT_ID, DRAFTS_PROJECT_NAME, Project
from local_agent.sessions.service import InvalidWorkspaceError, SessionService, validate_workspace
from local_agent.storage.json.projects import JsonProjectRepository

logger = logging.getLogger(__name__)

# Папку выбирают, чтобы агент мог её читать: чтение включено сразу. Запись и правка — только вручную.
DEFAULT_PROJECT_TOOLS = ("git", "list_files", "read_file", "search_files", "search_docs")


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

    def ensure_drafts(self, folder: Path) -> Project:
        """Создаёт «Черновики», если их ещё нет. Существующий проект не трогается: имя и папку
        пользователь мог поменять в настройках проекта."""
        if project := self._repository.get(DRAFTS_PROJECT_ID):
            return project
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        # Инструментов нет: в черновиках агент не читает файлы, пока пользователь сам их не включит.
        return self._repository.save(Project(
            id=DRAFTS_PROJECT_ID, name=DRAFTS_PROJECT_NAME, workspace=str(folder.resolve()),
            created_at=now, updated_at=now, enabled_tools=[],
        ))

    def update(self, project_id: str, *, name: str, workspace: str) -> Project | None:
        if self._repository.get(project_id) is None:
            return None
        return self._repository.patch(project_id, {
            "name": name,
            "workspace": self._workspace(workspace, ignore=project_id),
            "updated_at": datetime.now(UTC),
        })

    def delete(self, project_id: str) -> bool:
        """Сессии проекта не удаляются, а переходят в «Черновики» с их папкой и инструментами."""
        if project_id == DRAFTS_PROJECT_ID:
            raise ProjectError(f"Проект «{DRAFTS_PROJECT_NAME}» нельзя удалить")
        if self._repository.get(project_id) is None:
            return False
        for session in self._sessions.list():
            if session.project_id == project_id:
                self._sessions.assign_project(session.id, DRAFTS_PROJECT_ID)
        return self._repository.delete(project_id)

    def adopt_sessions(self) -> None:
        """Миграция при старте: у каждой сессии должен быть существующий проект.

        Сессия со своей папкой (созданная до проектов) переходит в проект этой папки, остальные —
        в «Черновики». Скрытая сессия агента с холста идёт за своей сессией-хозяйкой.
        """
        self._adopt_folders()
        known = {project.id for project in self._repository.list()}
        sessions = self._sessions.list()
        project_of = {session.id: session.project_id for session in sessions}
        # Сначала обычные сессии, затем скрытые: им нужен уже известный проект хозяйки.
        for session in sorted(sessions, key=lambda item: item.parent_id is not None):
            if session.project_id in known:
                continue
            parent_project = project_of.get(session.parent_id)
            project_id = parent_project if parent_project in known else DRAFTS_PROJECT_ID
            self._sessions.assign_project(session.id, project_id)
            project_of[session.id] = project_id

    def _adopt_folders(self) -> None:
        """Сессии со своей рабочей папкой переходят в проект этой папки; если его нет — он создаётся."""
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
