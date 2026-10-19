"""search_files: поиск текста или регулярного выражения по файлам проекта; строки с номерами."""

import fnmatch
import os
import re
from pathlib import Path

from meepo.agent.tools.base import SKIPPED_DIRS, Tool, ToolContext, ToolResult, inside, relative_to

MAX_MATCHES = 50
MAX_FILES = 5000
# Большие файлы в обходе папки пропускаются, но модель узнаёт о них и может поискать в каждом отдельно.
MAX_FILE_BYTES = 1024 * 1024
MAX_LINE_CHARS = 200


def search_files(args: dict, ctx: ToolContext) -> ToolResult:
    query = args["query"]
    if not query.strip():
        raise ValueError("Укажите текст для поиска")
    if args.get("regex"):
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Некорректное регулярное выражение: {exc}") from None
        matches_line = pattern.search
    else:
        needle = query.casefold()
        matches_line = lambda line: needle in line.casefold()  # noqa: E731
    mask = args.get("glob") or "*"
    start = inside(ctx.project, args.get("path") or ".")
    found: list[str] = []
    large: list[str] = []
    if start.is_file():
        files = [start]
    else:
        files = []
        for directory, dirs, names in os.walk(start):
            dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS)
            files += [Path(directory) / name for name in sorted(names) if fnmatch.fnmatch(name, mask)]
            if len(files) > MAX_FILES:
                break
    for path in files[:MAX_FILES]:
        if path is not start and path.stat().st_size > MAX_FILE_BYTES:
            large.append(relative_to(ctx.project, path))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        name = relative_to(ctx.project, path)
        for number, line in enumerate(text.splitlines(), start=1):
            if matches_line(line):
                found.append(f"{name}:{number}: {line.strip()[:MAX_LINE_CHARS]}")
                if len(found) >= MAX_MATCHES:
                    found.append(f"Показаны первые {MAX_MATCHES} совпадений — уточните запрос, папку или маску")
                    return ToolResult(_report(found, large))
    if len(files) > MAX_FILES:
        found.append(f"Просмотрено {MAX_FILES} файлов — уточните папку или маску")
    return ToolResult(_report(found, large))


def _report(found: list[str], large: list[str]) -> str:
    text = "\n".join(found) or "Совпадений нет"
    if large:
        names = ", ".join(large[:10]) + (f" и ещё {len(large) - 10}" if len(large) > 10 else "")
        text += f"\n\nБольшие файлы (больше 1 МБ) пропущены: {names}. Чтобы искать в таком файле, укажи его в path."
    return text


TOOLS = [
    Tool("search_files", "Найти строки с текстом (без учёта регистра) или регулярным выражением в файлах проекта. "
         "Возвращает путь, номер строки и саму строку.",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "Искомый текст или регулярное выражение"},
             "regex": {"type": "boolean", "description": "true — query это регулярное выражение"},
             "path": {"type": "string", "description": "Папка или файл для поиска относительно проекта; по умолчанию ."},
             "glob": {"type": "string", "description": "Маска имени файла, например *.py"},
         }, "required": ["query"], "additionalProperties": False},
         search_files),
]
