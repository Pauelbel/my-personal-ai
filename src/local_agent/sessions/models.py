"""Модель сессии хранит настройки разговора независимо от HTTP и формата хранения."""

from datetime import datetime

from pydantic import BaseModel, Field

DEFAULT_SESSION_TITLE = "Новая сессия"


class Session(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    agent_id: str
    model: str
    provider: str
    # Своя папка — только у сессии без проекта; у сессии проекта её задаёт проект.
    workspace: str | None
    project_id: str | None = None
    context_tokens: int | None = None
    # Инструменты включаются для каждой сессии отдельно; новая сессия начинает без доступа к файлам.
    enabled_tools: list[str] = Field(default_factory=list)
    # Резюме сообщений, которые уже не помещаются в окно модели, и последнее из них.
    summary: str = ""
    summary_until_message_id: str | None = None
