"""Инструменты вики: поиск и чтение доступны основному агенту и саб-агентам, запись — с подтверждением."""

import asyncio
from pathlib import Path

from local_agent.tools.base import ToolResult
from local_agent.wiki.service import WikiService

MAX_RESULT_CHARS = 6000


class WikiSearchTool:
    id = "wiki_search"
    name = "Поиск по вики"
    description = "Найти в вики (заметки по статьям и ссылкам пользователя) разделы по ключевым словам"
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Ключевые слова"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, wiki: WikiService) -> None:
        self._wiki = wiki

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult("Укажите ключевые слова", is_error=True)
        rows = await asyncio.to_thread(self._wiki.search, query)
        if not rows:
            return ToolResult("В вики ничего не нашлось.")
        parts, budget = [], MAX_RESULT_CHARS
        for path, title, _line, text in rows:
            body = text[:budget]
            budget -= len(body)
            parts.append(f"## {path} — {title}\n{body}")
            if budget <= 0:
                break
        return ToolResult("\n\n".join(parts) + "\n\nСтраницу целиком читай через wiki_read.")


class WikiReadTool:
    id = "wiki_read"
    name = "Чтение страницы вики"
    description = "Прочитать страницу вики целиком по пути из результатов wiki_search"
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Путь страницы, например ai/rag.md"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, wiki: WikiService) -> None:
        self._wiki = wiki

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        try:
            return ToolResult(await asyncio.to_thread(self._wiki.read, str(arguments.get("path", ""))))
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


class WikiWriteTool:
    id = "wiki_write"
    name = "Запись в вики"
    description = (
        "Создать или обновить страницу вики: заметки по статье или ссылке в Markdown — заголовок «# …», "
        "источник, ключевые мысли списком, термины. Пользователь подтверждает запись"
    )
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь страницы: латиница, - и _, .md на конце, например ai/rag.md"},
            "content": {"type": "string", "description": "Полный текст страницы в Markdown"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, wiki: WikiService) -> None:
        self._wiki = wiki

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        return {"kind": "tool_call", "title": f"Записать страницу вики {arguments.get('path')}",
                "preview": str(arguments.get("content", ""))}

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        try:
            return ToolResult(await asyncio.to_thread(
                self._wiki.write, str(arguments.get("path", "")), str(arguments.get("content", "")),
            ))
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
