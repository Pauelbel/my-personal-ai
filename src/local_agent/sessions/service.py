"""Сервис создаёт сессии и предоставляет их API без знания формата хранения.

Каждая сессия принадлежит проекту и берёт из него рабочую папку и инструменты: сервис всегда
отдаёт её уже с этими значениями, поэтому runtime и инструменты о проектах не знают.
"""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from local_agent.projects.models import DRAFTS_PROJECT_ID, Project
from local_agent.sessions.models import Session
from local_agent.sessions.repository import SessionRepository
from local_agent.storage.json.projects import JsonProjectRepository
from local_agent.team.models import Canvas
from local_agent.tools.registry import normalize_tool_ids


class InvalidWorkspaceError(ValueError):
    """Рабочая папка сессии не существует или указана относительным путём."""


def validate_workspace(workspace: str) -> str:
    path = Path(workspace).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise InvalidWorkspaceError("Рабочая папка должна быть существующей папкой с абсолютным путём")
    return str(path.resolve())


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        *,
        projects: JsonProjectRepository | None = None,
        on_deleted: Iterable[Callable[[str], None]] = (),
    ) -> None:
        self._repository = repository
        self._projects = projects
        # Кто хранит данные сессии отдельно (история, checkpoint памяти), чистит их при удалении.
        self._on_deleted = list(on_deleted)

    def create(
        self,
        *,
        title: str,
        agent_id: str,
        model: str,
        provider: str,
        project_id: str | None = None,
        parent_id: str | None = None,
        node_id: str | None = None,
        hidden: bool = False,
    ) -> Session:
        # Своей папки у сессии нет: её задаёт проект. Без проекта сессия попадает в «Черновики».
        now = datetime.now(UTC)
        session = Session(
            id=uuid4().hex,
            title=title,
            created_at=now,
            updated_at=now,
            agent_id=agent_id,
            model=model,
            provider=provider,
            project_id=project_id or DRAFTS_PROJECT_ID,
            parent_id=parent_id,
            node_id=node_id,
            hidden=hidden,
        )
        return self._effective(self._repository.save(session))

    def get(self, session_id: str) -> Session | None:
        return self._effective(self._repository.get(session_id))

    def configure(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        agent_id: str | None = None,
    ) -> Session | None:
        session = self._repository.get(session_id)
        if session is None:
            return None
        project = self._project(session)
        updates: dict[str, object] = {
            "provider": provider,
            "model": model,
            "updated_at": datetime.now(UTC),
        }
        if (provider, model) != (session.provider, session.model):
            updates.update(context_tokens=None, context_window=None, tokens_per_second=None)
        if agent_id is not None:
            updates["agent_id"] = agent_id
        updated = self._repository.patch(session_id, updates)
        if project is not None and updated is not None:
            # Следующая новая сессия проекта начнёт с того же агента и модели.
            self._projects.patch(project.id, {
                "agent_id": updated.agent_id, "provider": provider, "model": model,
                "updated_at": datetime.now(UTC),
            })
        return self._effective(updated)

    def set_tool_enabled(self, session_id: str, tool_id: str, enabled: bool) -> Session | None:
        session = self.get(session_id)
        if session is None:
            return None
        tools = set(session.enabled_tools)
        if enabled:
            tools.add(tool_id)
        else:
            tools.discard(tool_id)
        # В проекте переключатель общий для всех его сессий.
        if project := self._project(session):
            self._projects.patch(project.id, {"enabled_tools": sorted(tools)})
            return self.get(session_id)
        return self._repository.patch(session_id, {"enabled_tools": sorted(tools)})

    def assign_project(self, session_id: str, project_id: str) -> Session | None:
        """Переносит сессию в проект; устаревшая своя папка сессии при этом стирается."""
        return self._effective(
            self._repository.patch(session_id, {"project_id": project_id, "workspace": None})
        )

    def _project(self, session: Session) -> Project | None:
        if not session.project_id or self._projects is None:
            return None
        return self._projects.get(session.project_id)

    def _effective(self, session: Session | None) -> Session | None:
        if session is None:
            return session
        project = self._project(session)
        return session.model_copy(
            update={
                "workspace": project.workspace if project else session.workspace,
                "enabled_tools": normalize_tool_ids(project.enabled_tools if project else session.enabled_tools),
            }
        )

    def reassign_agent(self, old_agent_id: str, new_agent_id: str) -> int:
        """Агента удалили: его сессии продолжают работу с другим агентом, а не падают на следующем сообщении."""
        moved = 0
        for session in self._repository.list():
            if session.agent_id == old_agent_id:
                self._repository.patch(session.id, {"agent_id": new_agent_id})
                moved += 1
        return moved

    def set_canvas(self, session_id: str, canvas: Canvas) -> Session | None:
        return self._effective(self._repository.patch(session_id, {"canvas": canvas}))

    def set_node_session(self, session_id: str, node_id: str, child_id: str) -> None:
        """Запоминает скрытую сессию узла холста, чтобы агент продолжал её в следующих ходах."""
        session = self._repository.get(session_id)
        if session is not None:
            self._repository.patch(session_id, {"node_sessions": {**session.node_sessions, node_id: child_id}})

    def set_summary(self, session_id: str, summary: str, until_message_id: str) -> None:
        self._repository.patch(
            session_id, {"summary": summary, "summary_until_message_id": until_message_id}
        )

    def rename(self, session_id: str, title: str) -> Session | None:
        return self._effective(self._repository.rename(session_id, title, only_if_default=False))

    def delete(self, session_id: str) -> bool:
        deleted = self._repository.delete(session_id)
        if deleted:
            for callback in self._on_deleted:
                callback(session_id)
            # Скрытые сессии агентов с холста живут, только пока жива их сессия.
            for child in self._repository.list():
                if child.parent_id == session_id:
                    self.delete(child.id)
        return deleted

    def set_usage(self, session_id: str, usage: dict[str, object]) -> None:
        self._repository.set_usage(session_id, usage)

    def touch(self, session_id: str, updated_at: datetime) -> None:
        self._repository.touch(session_id, updated_at)

    def name_from_first_message(
        self, session_id: str, content: str, message_count: int
    ) -> Session | None:
        if message_count != 1:
            return self._repository.get(session_id)
        title = " ".join(content.split())
        if len(title) > 48:
            title = title[:47].rstrip() + "…"
        return self._repository.rename(session_id, title, only_if_default=True)

    def list(self) -> list[Session]:
        return [self._effective(session) for session in self._repository.list()]
