"""Инструменты долговременной памяти основного агента: поиск по запросу и запись факта о пользователе.

Агенту вики они не выдаются: память — о пользователе, а вики — нет.
"""

import asyncio
from pathlib import Path

from core.tool import ToolResult
from memory.markdown import DEFAULT_DOCUMENTS
from memory.service import MemoryService, MemoryServiceError
from sessions.models import Session


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


def _project(session: Session) -> tuple[str, str]:
    key = session.project_id or session.workspace or ""
    name = Path(session.workspace).name if session.workspace else key
    return key, name


class QAMemorySearchTool:
    id = "qa_memory_search"
    name = "Поиск в QA-опыте"
    description = "Найти до пяти записей QA-опыта текущего проекта; observation помечается как непроверенная."
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Компонент, API, файл или тип проверки"}},
        "required": ["query"], "additionalProperties": False,
    }

    def __init__(self, memory: MemoryService, session: Session) -> None:
        self._memory, self._session = memory, session

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        query = arguments.get("query")
        project_id, _name = _project(self._session)
        if not project_id or not isinstance(query, str) or not query.strip():
            return ToolResult("Нужны текущий проект и поисковый запрос", is_error=True)
        found = await asyncio.to_thread(self._memory.qa.search, project_id, query, limit=5)
        if not found:
            return ToolResult("Для текущего проекта подходящего QA-опыта нет.")
        lines = []
        for item in found:
            warning = "НЕ ПОДТВЕРЖДЕНО; не использовать как правило. " if item.status == "observation" else ""
            lines.append(f"- [{item.status}] {warning}{item.compact()}")
        return ToolResult("\n".join(lines))


class RememberQAExperienceTool:
    id = "remember_qa_experience"
    name = "Запомнить QA-опыт"
    description = "Сохранить повторно применимый QA-опыт проекта; confirmed требует доказательства."
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "ID существующей записи при подтверждении или устаревании"},
            "task": {"type": "string", "description": "Что проверяли"},
            "source": {"type": "string", "description": "Jira, документ, файл, тест или сообщение пользователя"},
            "status": {"type": "string", "enum": ["observation", "confirmed", "obsolete"]},
            "applies_to": {"type": "string", "description": "Где этот опыт применим"},
            "actions": {"type": "string", "description": "Какие действия выполнили"},
            "result": {"type": "string", "description": "Что получилось"},
            "evidence": {"type": "string", "description": "Тест, лог, скриншот или документация"},
            "user_correction": {"type": "string", "description": "Какое исправление явно внёс пользователь"},
        },
        "required": ["task", "source", "status", "applies_to", "actions", "result"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MemoryService, session: Session) -> None:
        self._memory, self._session = memory, session

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        project_id, project = _project(self._session)
        if not project_id:
            return ToolResult("QA-опыт сохраняется только внутри проекта", is_error=True)
        try:
            item = await asyncio.to_thread(
                self._memory.qa.save,
                project_id=project_id, project=project,
                **{key: str(arguments.get(key, "")) for key in (
                    "task", "source", "status", "applies_to", "actions", "result",
                    "evidence", "user_correction", "record_id",
                )},
            )
        except ValueError as exc:
            return ToolResult(f"Не сохранил QA-опыт: {exc}", is_error=True)
        return ToolResult(f"QA-опыт сохранён: id={item.id}, status={item.status}, project={item.project}")
