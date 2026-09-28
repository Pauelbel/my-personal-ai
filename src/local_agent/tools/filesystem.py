"""Файловые инструменты работают только с данными внутри выбранной рабочей папки."""

import asyncio
import fnmatch
import os
from pathlib import Path

from local_agent.tools.base import ToolResult

MAX_READ_BYTES = 64 * 1024
MAX_WRITE_BYTES = 256 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_FILES = 5000
MAX_SEARCH_MATCHES = 50
SKIPPED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}


def _workspace_root(workspace: Path) -> Path:
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Рабочая папка не существует")
    return root


def _relative(relative_path: object) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Укажите относительный путь")
    requested = Path(relative_path)
    if requested.is_absolute() or requested.drive:
        raise ValueError("Нужен путь относительно рабочей папки")
    return requested


def _inside_workspace(workspace: Path, relative_path: object) -> Path:
    root = _workspace_root(workspace)
    target = (root / _relative(relative_path)).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("Путь выходит за пределы рабочей папки")
    return target


def _deny_protected_write(target: Path, protected_root: Path | None) -> None:
    if protected_root is not None and target.resolve().is_relative_to(protected_root.resolve()):
        raise ValueError("Файлы долговременной памяти меняются только через раздел «Память»")


class ListFilesTool:
    id = "list_files"
    name = "Список файлов"
    description = "Показать содержимое папки внутри выбранной рабочей папки."
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Относительный путь к папке; для корня используйте ."}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            root = _workspace_root(workspace)
            target = _inside_workspace(workspace, arguments.get("path"))
            if not target.is_dir():
                raise ValueError("Это не папка")
            entries = sorted(target.iterdir(), key=lambda item: item.name.casefold())
            visible = []
            for item in entries:
                try:
                    if item.resolve(strict=True).is_relative_to(root):
                        visible.append(item)
                except OSError:
                    continue
            lines = [f"{'[папка]' if item.is_dir() else '[файл]'} {item.name}" for item in visible[:100]]
            if len(visible) > 100:
                lines.append("Показаны первые 100 элементов")
            return ToolResult("\n".join(lines) or "Папка пуста")
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


class ReadFileTool:
    id = "read_file"
    name = "Чтение файла"
    description = "Прочитать небольшой текстовый файл внутри выбранной рабочей папки."
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Относительный путь к текстовому файлу"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            target = _inside_workspace(workspace, arguments.get("path"))
            if not target.is_file():
                raise ValueError("Это не файл")
            with target.open("rb") as handle:
                raw = handle.read(MAX_READ_BYTES + 1)
            if len(raw) > MAX_READ_BYTES:
                raise ValueError("Файл больше 64 КБ")
            content = raw.decode("utf-8")
            if "\x00" in content:
                raise ValueError("Поддерживаются только текстовые файлы")
            return ToolResult(content)
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


class SearchFilesTool:
    id = "search_files"
    name = "Поиск по файлам"
    description = (
        "Найти строки с текстом во всех текстовых файлах рабочей папки. "
        "Возвращает путь, номер строки и саму строку."
    )
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Искомый текст без учёта регистра"},
            "path": {"type": "string", "description": "Относительная папка для поиска; по умолчанию ."},
            "glob": {"type": "string", "description": "Маска имени файла, например *.py"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("Укажите текст для поиска")
            pattern = arguments.get("glob") or "*"
            if not isinstance(pattern, str):
                raise ValueError("glob должен быть строкой")
            root = _workspace_root(workspace)
            start = _inside_workspace(workspace, arguments.get("path") or ".")
            if not start.is_dir():
                raise ValueError("Это не папка")
            needle = query.casefold()
            matches: list[str] = []
            scanned = 0
            for directory, dirs, files in os.walk(start):
                dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS)
                for file_name in sorted(files):
                    if not fnmatch.fnmatch(file_name, pattern):
                        continue
                    path = Path(directory) / file_name
                    scanned += 1
                    if scanned > MAX_SEARCH_FILES:
                        matches.append(f"Просмотрено максимум {MAX_SEARCH_FILES} файлов, уточните папку или маску")
                        return ToolResult("\n".join(matches))
                    try:
                        if not path.resolve(strict=True).is_relative_to(root) or path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                            continue
                        text = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        continue
                    relative = path.relative_to(root).as_posix()
                    for number, line in enumerate(text.splitlines(), start=1):
                        if needle in line.casefold():
                            matches.append(f"{relative}:{number}: {line.strip()[:200]}")
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                matches.append(f"Показаны первые {MAX_SEARCH_MATCHES} совпадений")
                                return ToolResult("\n".join(matches))
            return ToolResult("\n".join(matches) or "Совпадений нет")
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


class WriteFileTool:
    id = "write_file"
    name = "Запись файла"
    description = (
        "Создать или полностью перезаписать текстовый файл внутри рабочей папки. "
        "Пользователь подтверждает каждую запись."
    )
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Относительный путь к файлу"},
            "content": {"type": "string", "description": "Полное новое содержимое файла"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, protected_root: Path | None = None) -> None:
        self._protected_root = protected_root

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace, self._protected_root)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path, protected_root: Path | None = None) -> ToolResult:
        try:
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ValueError("content должен быть строкой")
            data = content.encode("utf-8")
            if len(data) > MAX_WRITE_BYTES:
                raise ValueError("Содержимое больше 256 КБ")
            root = _workspace_root(workspace)
            # Файла может ещё не быть, поэтому resolve без strict; существующие symlink всё равно раскрываются.
            target = (root / _relative(arguments.get("path"))).resolve()
            if not target.is_relative_to(root) or target == root:
                raise ValueError("Путь выходит за пределы рабочей папки")
            _deny_protected_write(target, protected_root)
            if target.is_dir():
                raise ValueError("По этому пути находится папка")
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            action = "перезаписан" if existed else "создан"
            return ToolResult(f"Файл {target.relative_to(root).as_posix()} {action}, {len(data)} байт")
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


class EditFileTool:
    id = "edit_file"
    name = "Правка файла"
    description = (
        "Заменить фрагмент текста в существующем файле рабочей папки. old_text должен встречаться "
        "в файле ровно один раз — добавьте соседние строки, чтобы фрагмент стал уникальным. "
        "Пользователь подтверждает каждую правку."
    )
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Относительный путь к файлу"},
            "old_text": {"type": "string", "description": "Точный фрагмент, который нужно заменить"},
            "new_text": {"type": "string", "description": "Новый текст вместо фрагмента; пустая строка удаляет его"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, protected_root: Path | None = None) -> None:
        self._protected_root = protected_root

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace, self._protected_root)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path, protected_root: Path | None = None) -> ToolResult:
        try:
            old_text, new_text = arguments.get("old_text"), arguments.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                raise ValueError("old_text должен быть непустой строкой")
            if not isinstance(new_text, str):
                raise ValueError("new_text должен быть строкой")
            root = _workspace_root(workspace)
            target = _inside_workspace(workspace, arguments.get("path"))
            _deny_protected_write(target, protected_root)
            if not target.is_file():
                raise ValueError("Это не файл")
            if target.stat().st_size > MAX_WRITE_BYTES:
                raise ValueError("Файл больше 256 КБ")
            content = target.read_bytes().decode("utf-8")
            # Модель пишет переводы строк как \n; в файле с CRLF ищем и вставляем в его формате.
            newline = "\r\n" if "\r\n" in content else "\n"
            old_text, new_text = (
                text.replace("\r\n", "\n").replace("\n", newline) for text in (old_text, new_text)
            )
            count = content.count(old_text)
            if count == 0:
                raise ValueError("Фрагмент old_text не найден в файле; сверьтесь с read_file")
            if count > 1:
                raise ValueError(f"Фрагмент old_text встречается {count} раз(а); добавьте соседние строки")
            data = content.replace(old_text, new_text, 1).encode("utf-8")
            if len(data) > MAX_WRITE_BYTES:
                raise ValueError("После правки файл больше 256 КБ")
            target.write_bytes(data)
            return ToolResult(f"Файл {target.relative_to(root).as_posix()} изменён, {len(data)} байт")
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
