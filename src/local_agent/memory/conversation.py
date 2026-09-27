"""Сервис истории создаёт записи разговора и получает их из хранилища."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
from uuid import uuid4

from local_agent.memory.base import ConversationStore
from local_agent.memory.models import Message


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        on_saved: Callable[[Message], None] | None = None,
    ) -> None:
        self._store = store
        self._on_saved = on_saved

    def add_user_message(self, session_id: str, content: str) -> Message:
        return self._add_message(session_id, "user", content)

    def add_assistant_message(self, session_id: str, content: str) -> Message:
        return self._add_message(session_id, "assistant", content)

    def _add_message(self, session_id: str, role: str, content: str) -> Message:
        message = Message(
            id=uuid4().hex,
            session_id=session_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        saved = self._store.save(message)
        if self._on_saved is not None:
            self._on_saved(saved)
        return saved

    def list(self, session_id: str) -> list[Message]:
        return self._store.list(session_id)

    def recent(self, session_id: str, limit: int) -> list[Message]:
        return self._store.recent(session_id, limit)

    def count(self, session_id: str) -> int:
        return self._store.count(session_id)

    def after(self, session_id: str, message_id: str | None) -> list[Message]:
        return self._store.after(session_id, message_id)
