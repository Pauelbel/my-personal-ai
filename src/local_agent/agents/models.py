"""Агент: идентичность, роль (системный промпт) и модель."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    system_prompt: str
    llm_provider: str
    model: str
    description: str = ""
    # None — агенту доступны все скиллы каталога, пустой кортеж — ни одного.
    skills: tuple[str, ...] | None = None
