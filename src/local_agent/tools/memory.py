"""Инструменты долговременной памяти основного агента: поиск по запросу и запись факта о пользователе.

Агенту вики они не выдаются: память — о пользователе, а вики — нет.
"""

import asyncio
from pathlib import Path

from local_agent.core.tool import ToolResult
from local_agent.memory.markdown import DEFAULT_DOCUMENTS
from local_agent.memory.service import MemoryService, MemoryServiceError


class MemorySearchTool:
    id = "memory_search"
    name = "Поиск в памяти"
    description = (
        "Найти в долговременной памяти факты о пользователе: имя, привычки, навыки, предпочтения, "
        "проекты, принятые решения. Используй, когда ответ зависит от того, что пользователь говорил раньше."
    )
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Ключевые слова"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult("Укажите ключевые слова", is_error=True)
        found = await asyncio.to_thread(self._memory.search, query, 8)
        if not found:
            return ToolResult("В памяти ничего не нашлось. Не выдумывай: спроси пользователя.")
        return ToolResult("\n".join(f"- {entry} ({file}: {section})" for file, section, entry in found))


class RememberTool:
    id = "remember"
    name = "Запомнить"
    description = (
        "Сразу запомнить устойчивый факт о пользователе из его последней реплики: имя, привычку, навык, "
        "предпочтение, проект или решение. Одна запись — один факт, коротко, в третьем лице."
    )
    requires_approval = False

    def __init__(self, memory: MemoryService, session_id: str) -> None:
        self._memory = memory
        self._session_id = session_id

    @property
    def parameters(self) -> dict[str, object]:
        files = sorted({document.name for document in self._memory.list_documents()} | set(DEFAULT_DOCUMENTS))
        return {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string", "enum": files,
                    "description": "profile.md — имя и кто он, preferences.md — привычки и предпочтения, "
                                   "projects.md — проекты, decisions.md — решения",
                },
                "section": {"type": "string", "description": "Раздел внутри файла, например «Обращение» или «Навыки»"},
                "content": {"type": "string", "description": "Факт одной строкой, например «Зовут Павел»"},
            },
            "required": ["file", "section", "content"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        values = [arguments.get(key) for key in ("file", "section", "content")]
        if not all(isinstance(value, str) and value.strip() for value in values):
            return ToolResult("Нужны file, section и content", is_error=True)
        try:
            return ToolResult(await asyncio.to_thread(self._memory.remember, self._session_id, *values))
        except MemoryServiceError as exc:
            return ToolResult(f"Не запомнил: {exc}", is_error=True)
