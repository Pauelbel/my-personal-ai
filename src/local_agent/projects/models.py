"""Проект — рабочая папка с общими настройками для всех её сессий."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Project(BaseModel):
    id: str
    name: str
    # Абсолютный путь к существующей папке; файловые и git-инструменты работают только внутри неё.
    workspace: str
    created_at: datetime
    updated_at: datetime
    # Модель последней сессии проекта: новая сессия в проекте начинает с неё.
    provider: str = "lm_studio"
    model: str = ""
    # full — все инструменты; read — только чтение: инструменты, меняющие файлы или систему, агенту недоступны.
    mode: Literal["full", "read"] = "full"
    # Подключённые к модели инструменты запоминаются на проект; None — ещё не выбирали (подключено чтение файлов).
    active_tools: list[str] | None = None
