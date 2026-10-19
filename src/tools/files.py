"""Файловые инструменты работают только с данными внутри выбранной рабочей папки."""

import asyncio
import fnmatch
import os
import re
from pathlib import Path

from core.tool import ToolResult
from shared.document import MAX_DOCUMENT_BYTES, outline, tokens
from shared.folders import MAX_SEARCH_FILE_BYTES, MAX_SEARCH_FILES, SKIPPED_DIRS

# Сколько read_file отдаёт за раз: файл больше — оглавлением, а читается кусками строк (offset, limit).
MAX_READ_BYTES = 64 * 1024
DEFAULT_READ_LINES = 400
MAX_WRITE_BYTES = 256 * 1024
MAX_SEARCH_MATCHES = 50


def _workspace_root(workspace: Path) -> Path:
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Рабочая папка не существует")
    return root


# Обёртка из Markdown, которую модель тащит в путь из текста задачи: «(`test_results.md`)» → «test_results.md`)».
WRAPPERS = "`'\"«»“”"


def _clean(path: str) -> str:
    """Срезает кавычки и непарные скобки по краям пути: файл «README.md`)» модели никогда не нужен."""
    previous = None
    while path != previous:
        previous = path
        path = path.strip().strip(WRAPPERS).rstrip(",;:")
        # Путь, скопированный из «крошек» («docs / api / index.md»): пробелы вокруг «/» — не часть имён.
        path = re.sub(r"\s*/\s*", "/", path)
        # Скобка за кавычкой — обёртка «(`путь`)», лишняя скобка — хвост фразы; «docs (old)» не трогаем.
        if path.endswith(")") and (path[-2:-1] in tuple(WRAPPERS) or path.count(")") > path.count("(")):
            path = path[:-1]
        if path.startswith("(") and (path[1:2] in tuple(WRAPPERS) or path.count("(") > path.count(")")):
            path = path[1:]
    return path


def _relative(relative_path: object) -> Path:
    if not isinstance(relative_path, str) or not _clean(relative_path):
        raise ValueError("Укажите относительный путь")
    relative_path = _clean(relative_path)
    if "`" in relative_path:
        raise ValueError("В пути не должно быть обратных кавычек")
    requested = Path(relative_path)
    # «C:/…» на Linux — обычный относительный путь, но модель так просит абсолютный путь Windows.
    if requested.is_absolute() or requested.drive or re.match(r"^[A-Za-z]:", relative_path):
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
    description = (
        "Прочитать текстовый файл внутри выбранной рабочей папки. Большой файл (больше 64 КБ) целиком не отдаётся: "
        "без offset придёт его оглавление с номерами строк, затем читай нужные места кусками — offset и limit"
    )
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Относительный путь к текстовому файлу"},
            "offset": {"type": "integer", "minimum": 1, "description": "С какой строки читать (с 1) — для больших файлов"},
            "limit": {"type": "integer", "minimum": 1,
                      "description": f"Сколько строк прочитать, по умолчанию {DEFAULT_READ_LINES}"},
        },
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
            if not target.is_file():
                raise ValueError("Это не файл")
            offset, limit = arguments.get("offset"), arguments.get("limit")
            for name, value in (("offset", offset), ("limit", limit)):
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                    raise ValueError(f"{name} — номер строки, целое число от 1")
            size = target.stat().st_size
            if size > MAX_DOCUMENT_BYTES:
                raise ValueError(f"Файл больше {MAX_DOCUMENT_BYTES // 1024 // 1024} МБ")
            content = target.read_bytes().decode("utf-8")
            if "\x00" in content:
                raise ValueError("Поддерживаются только текстовые файлы")
            relative = target.relative_to(root).as_posix()
            if offset is None and limit is None:
                if size <= MAX_READ_BYTES:
                    return ToolResult(content)
                return ToolResult(_overview(relative, content, size))
            return ToolResult(_lines(relative, content, offset or 1, limit or DEFAULT_READ_LINES))
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


def _overview(relative: str, content: str, size: int) -> str:
    """Большой файл: размер, оглавление и как читать дальше — вместо содержимого."""
    lines = content.splitlines()
    contents = outline(content)
    head = "\n".join(lines[:20]) if not contents else ""
    return "\n\n".join(part for part in (
        f"Файл {relative} большой: {len(lines)} строк, {size // 1024} КБ (≈{tokens(content)} токенов) — "
        "целиком в окно модели не помещается.",
        f"Оглавление (номер строки: заголовок):\n{contents}" if contents
        else f"Заголовков в файле нет. Начало файла:\n{head}",
        f"Читай нужные места кусками: read_file с offset (номер строки) и limit (до {DEFAULT_READ_LINES} строк); "
        "найти место по слову — search_files с path этого файла. Если задача про весь документ (тест-кейсы, "
        "выписка требований, конспект, поиск ошибок), обработай его целиком через process_document.",
    ) if part)


def _lines(relative: str, content: str, offset: int, limit: int) -> str:
    """Кусок файла строками offset…: не больше limit строк и не больше MAX_READ_BYTES."""
    lines = content.splitlines()
    if offset > len(lines):
        raise ValueError(f"В файле {len(lines)} строк — offset больше")
    taken: list[str] = []
    used = 0
    for line in lines[offset - 1:offset - 1 + limit]:
        used += len(line.encode("utf-8")) + 1
        if taken and used > MAX_READ_BYTES:
            break
        taken.append(line)
    end = offset + len(taken) - 1
    tail = f"\n\n(дальше — read_file с offset {end + 1})" if end < len(lines) else ""
    return f"{relative}, строки {offset}–{end} из {len(lines)}:\n" + "\n".join(taken) + tail


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
            "path": {"type": "string", "description": "Относительная папка или файл для поиска; по умолчанию ."},
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
            needle = query.casefold()
            matches: list[str] = []
            if start.is_file():
                # Один файл ищется при любом размере до MAX_DOCUMENT_BYTES: так находят место в большом документе.
                if start.stat().st_size > MAX_DOCUMENT_BYTES:
                    raise ValueError(f"Файл больше {MAX_DOCUMENT_BYTES // 1024 // 1024} МБ")
                _search_text(start, root, needle, matches)
                return ToolResult(_found(matches, []))
            if not start.is_dir():
                raise ValueError("Это не папка и не файл")
            scanned = 0
            # Большие файлы в обходе папки пропускаются, но модель узнаёт о них — и может поискать в каждом отдельно.
            large: list[str] = []
            for directory, dirs, files in os.walk(start):
                dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS)
                for file_name in sorted(files):
                    if not fnmatch.fnmatch(file_name, pattern):
                        continue
                    path = Path(directory) / file_name
                    scanned += 1
                    if scanned > MAX_SEARCH_FILES:
                        matches.append(f"Просмотрено максимум {MAX_SEARCH_FILES} файлов, уточните папку или маску")
                        return ToolResult(_found(matches, large))
                    try:
                        if not path.resolve(strict=True).is_relative_to(root):
                            continue
                        if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                            large.append(path.relative_to(root).as_posix())
                            continue
                    except OSError:
                        continue
                    if _search_text(path, root, needle, matches):
                        return ToolResult(_found(matches, large))
            return ToolResult(_found(matches, large))
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


def _search_text(path: Path, root: Path, needle: str, matches: list[str]) -> bool:
    """Добавляет совпадения файла; True — набралось MAX_SEARCH_MATCHES и искать дальше не нужно."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    relative = path.relative_to(root).as_posix()
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line.casefold():
            matches.append(f"{relative}:{number}: {line.strip()[:200]}")
            if len(matches) >= MAX_SEARCH_MATCHES:
                matches.append(f"Показаны первые {MAX_SEARCH_MATCHES} совпадений")
                return True
    return False


def _found(matches: list[str], large: list[str]) -> str:
    text = "\n".join(matches) or "Совпадений нет"
    if large:
        names = ", ".join(large[:10]) + (f" и ещё {len(large) - 10}" if len(large) > 10 else "")
        text += (f"\n\nБольшие файлы (больше {MAX_SEARCH_FILE_BYTES // 1024 // 1024} МБ) в поиске по папке пропущены: "
                 f"{names}. Чтобы искать в таком файле, укажи его в path.")
    return text


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
        "Чтобы добавить строки, не меняя существующие, передайте insert_after: true. "
        "Пользователь подтверждает каждую правку."
    )
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Относительный путь к файлу"},
            "old_text": {"type": "string", "description": "Точный фрагмент, который нужно заменить"},
            "new_text": {"type": "string", "description": "Новый текст вместо фрагмента; пустая строка удаляет его"},
            "insert_after": {
                "type": "boolean",
                "description": "true — вставить new_text новыми строками после строки с old_text, ничего не заменяя",
            },
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
            insert_after = arguments.get("insert_after", False)
            if not isinstance(insert_after, bool):
                raise ValueError("insert_after должен быть true или false")
            if insert_after and not new_text.strip("\r\n"):
                raise ValueError("Для вставки нужен непустой new_text")
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
            if insert_after:
                # Вставка идёт после всей строки с фрагментом: модели не нужно повторять старый текст,
                # и вставка не разрежет строку, даже если old_text — только её часть.
                start = content.index(old_text) + len(old_text)
                if not old_text.endswith(newline):
                    line_end = content.find(newline, start)
                    start = len(content) if line_end == -1 else line_end + len(newline)
                added = new_text.strip("\r\n") + newline
                if start == len(content) and not content.endswith(newline):
                    added = newline + added.removesuffix(newline)
                data = (content[:start] + added + content[start:]).encode("utf-8")
            else:
                data = content.replace(old_text, new_text, 1).encode("utf-8")
            if len(data) > MAX_WRITE_BYTES:
                raise ValueError("После правки файл больше 256 КБ")
            target.write_bytes(data)
            return ToolResult(f"Файл {target.relative_to(root).as_posix()} изменён, {len(data)} байт")
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
