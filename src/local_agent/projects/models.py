"""Проект — рабочая папка с общими настройками для всех её сессий."""

from datetime import datetime

from pydantic import BaseModel, Field


class Project(BaseModel):
    id: str
    name: str
    # Абсолютный путь к существующей папке; файловые и git-инструменты работают только внутри неё.
    workspace: str
    created_at: datetime
    updated_at: datetime
    # Выбор последней сессии проекта: новая сессия в проекте начинает с него.
    agent_id: str = "default"
    provider: str = "lm_studio"
    model: str = ""
    # Инструменты включаются один раз на проект и действуют во всех его сессиях.
    enabled_tools: list[str] = Field(default_factory=list)
