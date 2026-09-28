"""Реестр находит инструменты по ID."""

from collections.abc import Iterable

from local_agent.tools.base import Tool


def normalize_tool_ids(tool_ids: Iterable[str]) -> list[str]:
    """Старые разрешения чтения Git относятся к единому инструменту Git."""
    return list(dict.fromkeys("git" if item in {"git_log", "git_show"} else item for item in tool_ids))


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.id in self._tools:
            raise ValueError(f"Инструмент уже зарегистрирован: {tool.id}")
        self._tools[tool.id] = tool

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(tool_id)

    def all(self) -> list[Tool]:
        return list(self._tools.values())
