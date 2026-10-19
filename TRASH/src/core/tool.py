"""Контракт инструмента описывает вызов, не привязывая его к конкретной реализации."""

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Чей ход сейчас выполняет инструмент: сессия, провайдер и модель. Ядро ставит его перед вызовом;
# нужно инструментам, которые сами зовут модель или помечают состояние хода.
CURRENT_TURN: ContextVar[dict | None] = ContextVar("current_turn", default=None)


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    # Служебные данные для интерфейса: сохраняются с результатом в истории, модели не отправляются.
    meta: dict | None = None


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


def model_description(tool: Tool) -> str:
    """Описание для схемы вызова: строка description и, если есть, подсказка модели — как пользоваться
    инструментом. В реестре одной строкой подсказки нет: она нужна, когда инструмент подключён."""
    hint = getattr(tool, "hint", "")
    return f"{tool.description}\n\n{hint}" if hint else tool.description
