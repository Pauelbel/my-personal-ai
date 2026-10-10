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
    # Устарело: своя папка была у сессий до проектов, поле читается только из старых JSON для миграции.
    # Сервис подставляет сюда папку проекта для runtime и инструментов; не сохраняется и не уходит в API.
    workspace: str | None = Field(default=None, exclude=True)
    # Каждая сессия принадлежит проекту; None бывает только у старых записей до миграции при старте.
    project_id: str | None = None
    # Последний запрос: входные токены, размер окна модели и скорость генерации (токенов в секунду).
    context_tokens: int | None = None
    context_window: int | None = None
    tokens_per_second: float | None = None
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
