"""Исполняемый процесс: роль, шаги и папка со скриптами."""

from dataclasses import dataclass
from pathlib import Path

from steps.models import Steps


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    description: str
    instructions: str
    steps: Steps | None = None
    folder: Path | None = None

    def view(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            **({"steps": None} if self.steps is None else {"steps": self.steps.view()}),
        }
