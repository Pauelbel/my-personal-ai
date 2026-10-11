"""Готовые дополнения: встроенные в приложение инструменты, которые по умолчанию выключены.

Агент видит их в реестре среди архива и может предложить вернуть (archive restore) — с утверждением
пользователя, как любую другую возможность. Включённые хранятся в data/tools/extras.json.
"""

import json
from collections.abc import Callable
from pathlib import Path

from local_agent.extras.testgen.tool import GenerateTestCasesTool
from local_agent.graph.agent_graph import CURRENT_TURN
from local_agent.llm.registry import LLMRegistry
from local_agent.tools.base import Tool, ToolResult


class TestCasesExtra(GenerateTestCasesTool):
    """Генерация тест-кейсов идёт на модели того хода, в котором её вызвали."""

    def __init__(self, providers: LLMRegistry) -> None:
        super().__init__()
        self._providers = providers

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        turn = CURRENT_TURN.get() or {}
        provider = self._providers.get(turn.get("provider_id", ""))
        if provider is None or not turn.get("model"):
            return ToolResult("Генерации нужна модель текущей сессии", is_error=True)
        return await GenerateTestCasesTool(provider, turn["model"]).execute(arguments, workspace)


def extra_factories(providers: LLMRegistry) -> dict[str, tuple[str, Callable[[], Tool]]]:
    """id → (описание, фабрика)."""
    return {
        GenerateTestCasesTool.id: (
            "Генерация ручных тест-кейсов по Markdown-документации раздела постранично",
            lambda: TestCasesExtra(providers),
        ),
    }


class ExtrasStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def enabled(self) -> set[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()

    def set(self, tool_id: str, enabled: bool) -> None:
        items = self.enabled()
        items.add(tool_id) if enabled else items.discard(tool_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(items), ensure_ascii=False) + "\n", encoding="utf-8")
