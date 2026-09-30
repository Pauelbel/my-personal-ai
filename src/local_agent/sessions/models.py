"""Модель сессии хранит настройки разговора независимо от HTTP и формата хранения."""

from datetime import datetime

from pydantic import BaseModel, Field

from local_agent.team.models import Canvas

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
    # Холст: агенты, которым агент сессии может поручать работу. Пустой холст — обычный чат.
    canvas: Canvas = Field(default_factory=Canvas)
    # id узла холста → его скрытая сессия, где агент ведёт свою часть работы.
    node_sessions: dict[str, str] = Field(default_factory=dict)
    # У скрытой сессии узла: чья она и какой это узел холста.
    parent_id: str | None = None
    node_id: str | None = None
    # Сессия узла не показывается в общем списке, но открывается с его карточки на холсте.
    hidden: bool = False
