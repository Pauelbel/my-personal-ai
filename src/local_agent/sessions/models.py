"""Модель сессии хранит настройки разговора независимо от HTTP и формата хранения."""

from datetime import datetime
from typing import Literal

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
    # Устарело: своя папка была у сессий до проектов, поле читается только из старых JSON для миграции.
    # Сервис подставляет сюда папку проекта для runtime и инструментов; не сохраняется и не уходит в API.
    workspace: str | None = Field(default=None, exclude=True)
    # Разговор принадлежит проекту; None — у служебной сессии вики и у старых записей до миграции при старте.
    project_id: str | None = None
    # Последний запрос: входные токены, размер окна модели и скорость генерации (токенов в секунду).
    context_tokens: int | None = None
    context_window: int | None = None
    tokens_per_second: float | None = None
    # Резюме сообщений, которые уже не помещаются в окно модели, и последнее из них.
    summary: str = ""
    summary_until_message_id: str | None = None
    # Ход остановлен на подтверждении: поток графа в чекпоинтах и что именно ждёт решения пользователя.
    pending_thread: str | None = None
    pending: dict | None = None
    # У дочерней сессии саб-агента — сессия, которая поручила ему работу.
    parent_id: str | None = None
    # Задача саб-агента в дочерней сессии: agent_id, agent_name, title, status (queued, running, done, failed), error.
    task: dict | None = None
    # Дочерние сессии не показываются в общем списке.
    hidden: bool = False
    # Группы инструментов (MCP-серверы, например browser), которым пользователь доверил эту сессию:
    # их вызовы идут без карточек подтверждения до конца сессии.
    trusted: list[str] = []
    # План задачи из нескольких частей: [{"text", "done", "note"}]; plan_request — ход, в котором его меняли:
    # только в этом ходе движок не даёт закончить ответ, пока есть несделанные пункты.
    plan: list[dict] = []
    plan_request: str | None = None
    # chat — разговор в проекте; wiki — служебная сессия вики (lint): без проекта, видна только на вкладке «Вики»;
    # agent — личный чат с саб-агентом вне проектов: один на агента, открывается из блока «Агенты».
    kind: Literal["chat", "wiki", "agent"] = "chat"
