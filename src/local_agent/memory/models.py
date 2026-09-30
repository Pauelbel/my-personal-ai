"""Модель сообщения представляет запись разговора независимо от хранилища и API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StoredToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class Message(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    created_at: datetime
    # Ответ assistant с вызовами инструментов и результаты этих вызовов тоже хранятся в истории,
    # чтобы на следующих ходах модель помнила, что уже прочитала.
    tool_calls: list[StoredToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    # Реплику «user» написал не человек, а агент с холста сессии: здесь его имя.
    sender: str | None = None

    @property
    def from_user(self) -> bool:
        """Реплика человека: только её разбирает долговременная память."""
        return self.role == "user" and self.sender is None

    @property
    def is_dialogue(self) -> bool:
        """Обычная реплика разговора, без служебных сообщений инструментов."""
        return self.role in ("user", "assistant") and not self.tool_calls and bool(self.content)
