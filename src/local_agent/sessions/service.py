"""Сервис создаёт сессии и предоставляет их API без знания формата хранения."""

from datetime import datetime, timezone
from collections.abc import Callable
from uuid import uuid4

from local_agent.sessions.models import Session
from local_agent.sessions.repository import SessionRepository


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        delete_history: Callable[[str], None] | None = None,
    ) -> None:
        self._repository = repository
        self._delete_history = delete_history

    def create(
        self,
        *,
        title: str,
        agent_id: str,
        model: str,
        provider: str,
        workspace: str | None,
    ) -> Session:
        now = datetime.now(timezone.utc)
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
        self, session_id: str, *, provider: str, model: str, workspace: str | None
    ) -> Session | None:
        session = self._repository.get(session_id)
        if session is None:
            return None
        updated = session.model_copy(
            update={
                "provider": provider,
                "model": model,
                "workspace": workspace,
                "context_tokens": None if (provider, model) != (session.provider, session.model) else session.context_tokens,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self._repository.update(updated)

    def rename(self, session_id: str, title: str) -> Session | None:
        return self._repository.rename(session_id, title, only_if_default=False)

    def delete(self, session_id: str) -> bool:
        deleted = self._repository.delete(session_id)
        if deleted and self._delete_history is not None:
            self._delete_history(session_id)
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
