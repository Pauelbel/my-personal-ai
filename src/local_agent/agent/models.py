"""Сущность агента хранит его идентичность и постоянные параметры поведения."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    system_prompt: str
    llm_provider: str
    model: str
    tools: tuple[str, ...] = ()
