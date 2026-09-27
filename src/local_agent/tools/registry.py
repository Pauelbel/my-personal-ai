"""Реестр находит инструменты по ID и ограничивает набор разрешённым списком агента."""

from collections.abc import Iterable

from local_agent.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.id in self._tools:
            raise ValueError(f"Tool already registered: {tool.id}")
        self._tools[tool.id] = tool

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(tool_id)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def resolve(self, allowed_ids: Iterable[str]) -> list[Tool]:
        return [self._tools[tool_id] for tool_id in allowed_ids]
