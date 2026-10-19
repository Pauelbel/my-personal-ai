"""Агент: идентичность, роль, модель и собственный рабочий процесс."""

from dataclasses import dataclass
from pathlib import Path

from steps.models import Steps


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    system_prompt: str
    llm_provider: str
    model: str
    description: str = ""
    # Пользовательские специализированные агенты хранятся папками в data/agents.
    steps: Steps | None = None
    folder: Path | None = None
    tools: tuple[str, ...] = ()

    def as_workflow(self):
        from steps.workflow import Workflow

        return Workflow(self.id, self.name, self.description, self.system_prompt, self.steps, self.folder)

    def view(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "tools": list(self.tools),
            "steps": self.steps.view() if self.steps else None,
        }
