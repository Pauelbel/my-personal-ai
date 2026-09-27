"""Сервис создаёт сессии и предоставляет их API без знания формата хранения."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from local_agent.sessions.models import Session
from local_agent.sessions.repository import SessionRepository


class InvalidWorkspaceError(ValueError):
    """Рабочая папка сессии не существует или указана относительным путём."""


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        on_deleted: Iterable[Callable[[str], None]] = (),
    ) -> None:
        self._repository = repository
        # Кто хранит данные сессии отдельно (история, checkpoint памяти), чистит их при удалении.
        self._on_deleted = list(on_deleted)

    def create(
        self,
        *,
        title: str,
        agent_id: str,
        model: str,
        provider: str,
        workspace: str | None,
    ) -> Session:
        workspace = self._validate_workspace(workspace)
        now = datetime.now(UTC)
        session = Session(
            id=uuid4().hex,
            title=title,
            created_at=now,
            updated_at=now,
            agent_id=agent_id,
            model=model,
            provider=provider,
            workspace=workspace,
        )
        return self._repository.save(session)

    def get(self, session_id: str) -> Session | None:
        return self._repository.get(session_id)

    def configure(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        workspace: str | None,
        agent_id: str | None = None,
    ) -> Session | None:
        session = self._repository.get(session_id)
        if session is None:
            return None
        workspace = self._validate_workspace(workspace)
        updates: dict[str, object] = {
            "provider": provider,
            "model": model,
            "workspace": workspace,
            "updated_at": datetime.now(UTC),
        }
        if (provider, model) != (session.provider, session.model):
            updates["context_tokens"] = None
        if agent_id is not None:
            updates["agent_id"] = agent_id
        return self._repository.patch(session_id, updates)

    @staticmethod
    def _validate_workspace(workspace: str | None) -> str | None:
        if not workspace:
            return None
        path = Path(workspace).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise InvalidWorkspaceError("Рабочая папка должна быть существующей папкой с абсолютным путём")
        return str(path.resolve())

    def set_tool_enabled(self, session_id: str, tool_id: str, enabled: bool) -> Session | None:
        session = self._repository.get(session_id)
        if session is None:
            return None
        tools = set(session.enabled_tools)
        if enabled:
            tools.add(tool_id)
        else:
            tools.discard(tool_id)
        return self._repository.patch(session_id, {"enabled_tools": sorted(tools)})

    def set_summary(self, session_id: str, summary: str, until_message_id: str) -> None:
        self._repository.patch(
            session_id, {"summary": summary, "summary_until_message_id": until_message_id}
        )

    def rename(self, session_id: str, title: str) -> Session | None:
        return self._repository.rename(session_id, title, only_if_default=False)

    def delete(self, session_id: str) -> bool:
        deleted = self._repository.delete(session_id)
        if deleted:
            for callback in self._on_deleted:
                callback(session_id)
        return deleted

    def set_context_tokens(self, session_id: str, count: int | None) -> None:
        self._repository.set_context_tokens(session_id, count)

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
        return self._repository.list()
