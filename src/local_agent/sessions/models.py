"""Модель сессии хранит настройки разговора независимо от HTTP и SQLite."""

from datetime import datetime

from pydantic import BaseModel

DEFAULT_SESSION_TITLE = "New Session"


class Session(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    agent_id: str
    model: str
    provider: str
    workspace: str | None
    context_tokens: int | None = None
