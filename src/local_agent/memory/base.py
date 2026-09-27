"""Контракт истории позволяет заменить способ хранения сообщений без изменений сервиса."""

from __future__ import annotations

from typing import Protocol

from local_agent.memory.models import Message


class ConversationStore(Protocol):
    def save(self, message: Message) -> Message: ...

    def list(self, session_id: str) -> list[Message]: ...

    def recent(self, session_id: str, limit: int) -> list[Message]: ...

    def count(self, session_id: str) -> int: ...
