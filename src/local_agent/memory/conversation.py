"""Сервис истории создаёт записи разговора и получает их из хранилища."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from local_agent.llm.models import ToolCall
from local_agent.memory.base import ConversationStore
from local_agent.memory.models import Message, StoredToolCall


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        on_saved: Callable[[Message], None] | None = None,
    ) -> None:
        self._store = store
        self._on_saved = on_saved

    def add_user_message(self, session_id: str, content: str) -> Message:
        return self._add(session_id, role="user", content=content)

    def add_assistant_message(
        self, session_id: str, content: str, tool_calls: tuple[ToolCall, ...] = ()
    ) -> Message:
        return self._add(
            session_id,
            role="assistant",
            content=content,
            tool_calls=[
                StoredToolCall(id=call.id, name=call.name, arguments=call.arguments)
                for call in tool_calls
            ],
        )

    def add_tool_message(
        self, session_id: str, *, call_id: str, name: str, content: str, is_error: bool
    ) -> Message:
        return self._add(
            session_id,
            role="tool",
            content=content,
            tool_call_id=call_id,
            tool_name=name,
            is_error=is_error,
        )

    def _add(self, session_id: str, **fields) -> Message:
        message = Message(
            id=uuid4().hex,
            session_id=session_id,
            created_at=datetime.now(UTC),
            **fields,
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
