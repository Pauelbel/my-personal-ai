"""Агент: идентичность, роль (системный промпт) и постоянные параметры поведения."""

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
    # None — агенту доступны все скиллы каталога.
    skills: tuple[str, ...] | None = None
    # None — общий лимит runtime.
    max_tool_rounds: int | None = None
    # Саб-агенты, которых этот агент может спросить посреди своей задачи (связи на холсте «Команда»).
    agents: tuple[str, ...] = ()
