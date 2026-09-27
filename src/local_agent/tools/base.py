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

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult: ...
