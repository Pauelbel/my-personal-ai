"""Набор инструментов хода: что модель видит одной строкой, а чьи схемы подключены к запросу.

Окно локальной модели маленькое, поэтому схемы всех инструментов в запрос не попадают:
- базовые (activate, use_skill, propose, …) подключены всегда;
- остальные модель видит в реестре одной строкой и подключает через activate,
  не больше MAX_ACTIVE_TOOLS одновременно — самый давний подключённый уходит первым.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from local_agent.tools.base import Tool, ToolResult, summary_line

MAX_ACTIVE_TOOLS = 5
# В проекте, где ещё ничего не подключали, чтение файлов под рукой сразу: без лишнего раунда activate.
DEFAULT_ACTIVE_TOOLS = ("list_files", "read_file")


@dataclass
class Toolbox:
    core: list[Tool]
    catalog: list[Tool]
    active: list[str]
    # Сохраняет новый список подключённых инструментов сессии.
    on_active_change: Callable[[list[str]], None] = lambda active: None
    # Саб-агенту подключены сразу все его инструменты: их немного, а activate ему не нужен.
    all_active: bool = False
    _by_id: dict[str, Tool] = field(init=False)

    def __post_init__(self) -> None:
        self._by_id = {tool.id: tool for tool in [*self.catalog, *self.core]}
        known = {tool.id for tool in self.catalog}
        self.active = [tool_id for tool_id in self.active if tool_id in known]

    def get(self, name: str) -> Tool | None:
        return self._by_id.get(name)

    def is_active(self, name: str) -> bool:
        return self.all_active or name in self._with_kits() or any(tool.id == name for tool in self.core)

    def bound(self) -> list[Tool]:
        if self.all_active:
            return [*self.core, *self.catalog]
        active = self._with_kits()
        return [*self.core, *(tool for tool in self.catalog if tool.id in active)]

    def _with_kits(self) -> set[str]:
        """Подключённые и их наборы: инструмент с kit (основные действия браузера) подключает весь набор,
        а набор при этом слотов не занимает."""
        active = set(self.active)
        for tool_id in self.active:
            active.update(getattr(self._by_id.get(tool_id), "kit", ()))
        return active

    def definitions(self) -> list[dict[str, object]]:
        return [
            {"type": "function", "function": {
                "name": tool.id, "description": tool.description, "parameters": tool.parameters,
            }}
            for tool in self.bound()
        ]

    def activate(self, names: Iterable[str]) -> tuple[list[str], list[str]]:
        """Подключает инструменты реестра; возвращает (подключённые, вытесненные)."""
        known = {tool.id for tool in self.catalog}
        added = [name for name in dict.fromkeys(names) if name in known]
        active = [name for name in self.active if name not in added] + added
        evicted = active[:-MAX_ACTIVE_TOOLS] if len(active) > MAX_ACTIVE_TOOLS else []
        self.active = active[-MAX_ACTIVE_TOOLS:]
        if not self.all_active:
            self.on_active_change(self.active)
        return added, evicted

    def catalog_lines(self) -> list[str]:
        return [
            f"- {tool.id}{' (подключён)' if self.is_active(tool.id) else ''} — {summary_line(tool.description)}"
            for tool in self.catalog
        ]


class ActivateTool:
    """Подключает схемы инструментов из реестра к следующим запросам модели."""

    id = "activate"
    name = "Подключение инструментов"
    requires_approval = False

    def __init__(self, toolbox: Toolbox) -> None:
        self._toolbox = toolbox

    @property
    def description(self) -> str:
        return (
            "Подключить инструменты из реестра возможностей, чтобы вызывать их. "
            f"Одновременно подключено не больше {MAX_ACTIVE_TOOLS}; лишние отключаются, начиная с давних."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "items": {"type": "string", "enum": [tool.id for tool in self._toolbox.catalog] or [""]},
                    "description": "id инструментов из реестра",
                },
            },
            "required": ["names"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        names = arguments.get("names")
        if not isinstance(names, list) or not names:
            return ToolResult("Укажите id инструментов из реестра", is_error=True)
        unknown = [name for name in names if self._toolbox.get(str(name)) is None]
        added, evicted = self._toolbox.activate(str(name) for name in names)
        lines = [f"Подключено: {', '.join(added)}." if added else "Ничего не подключено."]
        for name in added:
            tool = self._toolbox.get(name)
            lines.append(f"- {name}: {tool.description}")
        if evicted:
            lines.append(f"Отключены, чтобы не переполнять контекст: {', '.join(evicted)}.")
        if unknown:
            lines.append(
                f"Таких инструментов нет: {', '.join(map(str, unknown))}. Если без них задачу не решить, "
                "скажи пользователю, чего не хватает, и предложи создать инструмент."
            )
        return ToolResult("\n".join(lines), is_error=not added)
