"""Контракт инструмента описывает вызов, не привязывая его к конкретной реализации."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


class Tool(Protocol):
    id: str
    name: str
    description: str
    parameters: dict[str, object]
    # Инструменты, которые что-то меняют, выполняются только после подтверждения пользователем в UI.
    requires_approval: bool

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult: ...


def summary_line(text: str, limit: int = 110) -> str:
    """Одна строка описания для компактного реестра: первое предложение, без переносов."""
    line = " ".join(text.split())
    for mark in (". ", "; "):
        if mark in line:
            line = line.split(mark, 1)[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"
