"""Поиск по документации: полнотекстовый индекс SQLite FTS5 по разделам Markdown-файлов рабочей папки."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path

from local_agent.core.tool import ToolResult
from local_agent.shared.folders import MAX_SEARCH_FILES
from local_agent.shared.markdown_index import DEFAULT_LIMIT, MarkdownIndex, terms
from local_agent.tools.files import _inside_workspace, _workspace_root

MAX_LIMIT = 10
MAX_RESULT_CHARS = 8000


class SearchDocsTool:
    id = "search_docs"
    name = "Поиск по документации"
    description = (
        "Найти в Markdown-документации рабочей папки разделы по ключевым словам. "
        "Возвращает путь, строку начала, заголовки раздела и его текст; соседние разделы дочитывайте через read_file."
    )
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Ключевые слова: термины, названия полей и экранов, тексты ошибок. Слова соединяются через ИЛИ",
            },
            "path": {"type": "string", "description": "Искать только в этой папке; по умолчанию вся рабочая папка"},
            "limit": {
                "type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                "description": f"Сколько разделов вернуть, по умолчанию {DEFAULT_LIMIT}",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, index_root: Path) -> None:
        self._index_root = index_root

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace)

    def _run(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("Укажите ключевые слова для поиска")
            stems, short = terms(query)
            limit = arguments.get("limit", DEFAULT_LIMIT)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
                raise ValueError(f"limit — целое число от 1 до {MAX_LIMIT}")
            root = _workspace_root(workspace)
            start = _inside_workspace(workspace, arguments.get("path") or ".")
            if not start.is_dir():
                raise ValueError("Это не папка")
            prefix = start.relative_to(root).as_posix()
            name = hashlib.sha256(str(root).encode()).hexdigest()[:16]
            index = MarkdownIndex(root, self._index_root / f"{name}.sqlite")
            rows, skipped = index.query(stems, short, "" if prefix == "." else prefix, limit)
            return ToolResult(_format(rows, skipped))
        except (OSError, ValueError, sqlite3.Error) as exc:
            return ToolResult(str(exc), is_error=True)


def _format(rows: list[tuple[str, str, int, str]], skipped: bool) -> str:
    notes = [f"Проиндексировано максимум {MAX_SEARCH_FILES} файлов, уточните папку"] if skipped else []
    if not rows:
        return "\n".join([*notes, "Ничего не найдено. Попробуйте синонимы, названия полей или термины из документации"])
    parts: list[str] = []
    budget = MAX_RESULT_CHARS
    for path, title, line, text in rows:
        if budget <= 0:
            parts.append("Остальные разделы не поместились: уточните запрос или уменьшите limit")
            break
        body = text if len(text) <= budget else text[:budget] + "…"
        budget -= len(body)
        parts.append(f"## {path}:{line} — {title}\n{body}")
    return "\n\n".join([*notes, *parts])
