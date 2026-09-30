"""Сущности агента и навыка: идентичность, постоянные параметры поведения и инструкции."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    system_prompt: str
    llm_provider: str
    model: str
    tools: tuple[str, ...] = ()
    # Кратко о роли: его видят соседи по графу, когда выбирают, кому написать.
    description: str = ""
    # None — агенту доступны все навыки каталога.
    skills: tuple[str, ...] | None = None
    # None — общий лимит runtime.
    max_tool_rounds: int | None = None


@dataclass(frozen=True)
class Skill:
    """Навык: инструкции для типовой задачи, которые модель подгружает по id, когда они нужны."""

    id: str
    name: str
    description: str
    instructions: str
