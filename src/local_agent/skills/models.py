"""Скилл: инструкции для типовой задачи и, если работа идёт по шагам, её шаги (steps.md) со скриптами."""

from dataclasses import dataclass
from pathlib import Path

from local_agent.steps.models import Steps


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    instructions: str
    # Шаги из steps.md; None — скилл без шагов, только инструкции.
    steps: Steps | None = None
    # Папка скилла: в её scripts/ лежат скрипты, которые запускают шаги.
    folder: Path | None = None

    def view(self) -> dict:
        """Скилл для интерфейса и карточки запуска: шаги — тем же видом, что в steps.md."""
        return {
            "id": self.id, "name": self.name, "description": self.description,
            **({"steps": None} if self.steps is None else {"steps": self.steps.view()}),
        }
