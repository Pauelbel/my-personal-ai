"""Исполнитель инструментов решает, какие инструменты доступны сессии, и безопасно их вызывает."""

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from local_agent.agent.models import Agent
from local_agent.llm.models import ToolCall
from local_agent.sessions.models import Session
from local_agent.tools.base import Tool, ToolResult
from local_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(
        self, tools: list[Tool], workspace: Path | None, request_id: str, builtins: Sequence[Tool] = ()
    ) -> None:
        self._tools = {tool.id: tool for tool in [*tools, *builtins]}
        # Встроенным инструментам (навыки) не нужны ни рабочая папка, ни включение в сессии.
        self._builtins = {tool.id for tool in builtins}
        self._workspace = workspace
        self._request_id = request_id

    @classmethod
    def for_session(
        cls,
        registry: ToolRegistry,
        agent: Agent,
        session: Session,
        request_id: str,
        builtins: Sequence[Tool] = (),
    ) -> "ToolExecutor":
        tools = cls.allowed_tools(registry, agent, session)
        return cls(tools, Path(session.workspace) if session.workspace else None, request_id, builtins)

    @staticmethod
    def allowed_tools(registry: ToolRegistry, agent: Agent, session: Session) -> list[Tool]:
        """Инструмент доступен, только если его разрешает агент, он включён в сессии и выбрана рабочая папка."""
        if not session.workspace:
            return []
        return [
            tool for tool in registry.all()
            if tool.id in agent.tools and tool.id in session.enabled_tools
        ]

    def definitions(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.id,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @staticmethod
    def parse_arguments(call: ToolCall) -> dict[str, object] | None:
        try:
            arguments = json.loads(call.arguments)
        except ValueError:
            return None
        return arguments if isinstance(arguments, dict) else None

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None or (self._workspace is None and call.name not in self._builtins):
            return ToolResult("Инструмент не разрешён для этой сессии", is_error=True)
        arguments = self.parse_arguments(call)
        if arguments is None:
            return ToolResult("Аргументы должны быть JSON-объектом", is_error=True)
        try:
            return await tool.execute(arguments, self._workspace)
        except Exception:
            # Сбой одного инструмента не должен ронять весь ход: модель получает ошибку как результат.
            logger.exception("Инструмент завершился ошибкой request_id=%s tool=%s", self._request_id, call.name)
            return ToolResult("Внутренняя ошибка инструмента", is_error=True)
