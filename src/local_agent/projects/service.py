"""Сервис проектов создаёт и меняет проекты и связывает с ними сессии.

Каждая сессия живёт в проекте: без проекта сессию не создать, с удалением проекта удаляются и его сессии.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from local_agent.projects.models import Project
from local_agent.sessions.service import InvalidWorkspaceError, SessionService, validate_workspace
from local_agent.storage.json.projects import JsonProjectRepository

logger = logging.getLogger(__name__)
# Так выбор папки подписывает домашнюю папку. Раньше подпись становилась названием проекта,
# даже если потом выбрали папку глубже: такие названия при старте меняются на имя папки.
HOME_LABEL = "Домашняя папка ({})"
# Раньше сессии без проекта попадали в системный проект «Черновики». Пустой такой проект при старте удаляется.
LEGACY_DRAFTS_ID = "drafts"

# Папку выбирают, чтобы агент мог её читать: чтение включено сразу. Запись и правка — только вручную.


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
    ) -> Project:
        workspace = self._workspace(workspace)
        now = datetime.now(UTC)
        return self._repository.save(Project(
            id=uuid4().hex, name=name.strip() or folder_name(workspace), workspace=workspace, created_at=now, updated_at=now,
            agent_id=agent_id, provider=provider, model=model,
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

    def set_route(self, project_id: str, kind: str, agent_id: str) -> Project | None:
        project = self._repository.get(project_id)
        if project is None or not (kind := route_kind(kind)):
            return project
        return self._repository.patch(project_id, {"routes": {**project.routes, kind: agent_id}})

    def forget_route(self, project_id: str, kind: str) -> Project | None:
        project = self._repository.get(project_id)
        if project is None:
            return None
        return self._repository.patch(project_id, {"routes": {
            key: value for key, value in project.routes.items() if key != route_kind(kind)
        }})

    def delete(self, project_id: str) -> bool:
        """Проект удаляется вместе с сессиями и их историей; папка на диске остаётся."""
        if self._repository.get(project_id) is None:
            return False
        for session in self._sessions.list():
            if session.project_id == project_id and not session.parent_id:
                # Дочерние сессии саб-агентов удаляются вместе с основной.
                self._sessions.delete(session.id)
        return self._repository.delete(project_id)

    def adopt_sessions(self) -> None:
        """Миграция при старте: у каждой сессии должен быть существующий проект.

        Сессия со своей папкой (созданная до проектов) переходит в проект этой папки, остальные —
        в последний изменённый проект. Дочерняя сессия саб-агента идёт за сессией, которая поручила
        ему задачу. Пока проектов нет, такие сессии ждут: их подберёт старт после создания проекта.
        """
        self._adopt_folders()
        self._shorten_home_labels()
        self._drop_empty_drafts()
        projects = self._repository.list()
        known = {project.id for project in projects}
        fallback = max(projects, key=lambda project: project.updated_at).id if projects else None
        sessions = self._sessions.list()
        project_of = {session.id: session.project_id for session in sessions}
        # Сначала обычные сессии, затем скрытые: им нужен уже известный проект хозяйки.
        for session in sorted(sessions, key=lambda item: item.parent_id is not None):
            # Чат вики и личные чаты с агентами живут вне проектов: проект им не нужен.
            if session.project_id in known or session.kind != "chat":
                continue
            parent_project = project_of.get(session.parent_id)
            project_id = parent_project if parent_project in known else fallback
            if project_id is None:
                continue
            self._sessions.assign_project(session.id, project_id)
            project_of[session.id] = project_id

    def _drop_empty_drafts(self) -> None:
        if self._repository.get(LEGACY_DRAFTS_ID) is None:
            return
        if not any(session.project_id == LEGACY_DRAFTS_ID for session in self._sessions.list()):
            self._repository.delete(LEGACY_DRAFTS_ID)

    def _shorten_home_labels(self) -> None:
        label = HOME_LABEL.format(Path.home().name)
        for project in self._repository.list():
            if project.name == label and Path(project.workspace) != Path.home():
                self._repository.patch(project.id, {"name": folder_name(project.workspace)})

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


def route_kind(kind: str) -> str:
    """Тип задачи маршрута — коротко и в одном регистре: «Автотесты » и «автотесты» — один маршрут."""
    return " ".join(kind.split()).casefold()[:60]


def folder_name(workspace: str) -> str:
    """Название проекта по умолчанию — имя его папки."""
    return Path(workspace).name or workspace
