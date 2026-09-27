"""Файловые инструменты читают только данные внутри выбранной рабочей папки."""

from pathlib import Path

from local_agent.tools.base import ToolResult


def _inside_workspace(workspace: Path, relative_path: object) -> Path:
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Рабочая папка не существует")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Укажите относительный путь")
    requested = Path(relative_path)
    if requested.is_absolute() or requested.drive:
        raise ValueError("Нужен путь относительно рабочей папки")
    target = (root / requested).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("Путь выходит за пределы рабочей папки")
    return target


class ListFilesTool:
    id = "list_files"
    name = "Список файлов"
    description = "Показать содержимое папки внутри выбранной рабочей папки."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Относительный путь к папке; для корня используйте ."}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            target = _inside_workspace(workspace, arguments.get("path"))
            if not target.is_dir():
                raise ValueError("Это не папка")
            entries = sorted(target.iterdir(), key=lambda item: item.name.casefold())
            visible = []
            for item in entries:
                try:
                    if item.resolve(strict=True).is_relative_to(workspace.resolve()):
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
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Относительный путь к текстовому файлу"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            target = _inside_workspace(workspace, arguments.get("path"))
            if not target.is_file():
                raise ValueError("Это не файл")
            with target.open("rb") as handle:
                raw = handle.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError("Файл больше 64 КБ")
            content = raw.decode("utf-8")
            if "\x00" in content:
                raise ValueError("Поддерживаются только текстовые файлы")
            return ToolResult(content)
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
