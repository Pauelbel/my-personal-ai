"""Модель сообщения представляет запись разговора независимо от хранилища и API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StoredToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class MessageStats(BaseModel):
    """Сводка хода под ответом: время модели и инструментов (без ожидания решений пользователя) и токены."""

    seconds: float
    # Сколько раз за ход звали модель: раунды с инструментами плюс финальный ответ.
    rounds: int = 1
    # Размер последнего запроса к модели и всё, что она написала за ход.
    input_tokens: int | None = None
    output_tokens: int | None = None
    tokens_per_second: float | None = None


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
    # Только у финального ответа хода.
    stats: MessageStats | None = None
    # Рассуждения модели перед этим ответом: только для показа в «ходе работы», в контекст модели не идут.
    reasoning: str | None = None
    # Служебные данные результата инструмента для интерфейса (например, какой текст — итог этапа плана).
    meta: dict | None = None
    # Картинки, которые пользователь вставил в реплику: имена файлов в data/attachments.
    attachments: list[str] = Field(default_factory=list)

    @property
    def from_user(self) -> bool:
        """Реплика человека: только её разбирает долговременная память."""
        return self.role == "user"

    @property
    def is_dialogue(self) -> bool:
        """Обычная реплика разговора, без служебных сообщений инструментов."""
        return self.role in ("user", "assistant") and not self.tool_calls and bool(self.content)
