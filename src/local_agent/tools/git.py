"""Git-инструменты только читают историю коммитов рабочей папки и ничего не меняют в репозитории."""

import asyncio
import os
import re
import subprocess
from pathlib import Path

from local_agent.tools.base import ToolResult
from local_agent.tools.filesystem import _relative, _workspace_root

# ~6–8 тысяч токенов: больший дифф не поместится в окно небольшой локальной модели.
MAX_OUTPUT_CHARS = 24 * 1024
MAX_LOG_COMMITS = 50
GIT_TIMEOUT_SECONDS = 15
COMMIT = re.compile(r"^[0-9a-fA-F]{4,40}$")
# Конфигурация и .gitattributes репозитория могут подключить внешние программы (diff, textconv,
# pager, fsmonitor). Отключаем их, чтобы чтение истории не запускало ничего постороннего.
SAFE_OPTIONS = (
    "--no-pager",
    # Без магии путей: «:/файл» иначе значит «от корня репозитория» и уводит за рабочую папку.
    "--literal-pathspecs",
    "-c", "core.quotePath=false",
    "-c", "core.pager=cat",
    "-c", "core.fsmonitor=false",
    "-c", "diff.external=",
    "-c", "log.showSignature=false",
)
# LC_ALL=C: сообщения git на английском, чтобы распознавать ошибки независимо от языка системы.
SAFE_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}


def _git(workspace: Path, *arguments: str) -> str:
    """Запускает только переданную подкоманду чтения; аргументы модели сюда попадают уже проверенными."""
    root = _workspace_root(workspace)
    try:
        completed = subprocess.run(
            ["git", *SAFE_OPTIONS, *arguments],
            cwd=root,
            env={**os.environ, **SAFE_ENV},
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ValueError("git не установлен") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git не ответил за отведённое время") from exc
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode != 0:
        if "not a git repository" in stderr:
            raise ValueError("Рабочая папка не является git-репозиторием")
        raise ValueError(f"git завершился ошибкой: {stderr[:300] or completed.returncode}")
    output = completed.stdout.decode("utf-8", "replace")
    if len(output) > MAX_OUTPUT_CHARS:
        output = (
            output[:MAX_OUTPUT_CHARS]
            + f"\n… (вывод обрезан до {MAX_OUTPUT_CHARS // 1024} КБ; чтобы увидеть остальное, "
            "вызовите git_show с тем же commit и path нужного файла из списка выше)"
        )
    return output


def _pathspec(workspace: Path, value: object) -> list[str]:
    """Фильтр по пути всегда после --; без него — вся рабочая папка, но не остальной репозиторий."""
    if value is None or value == "":
        return ["--", "."]
    requested = _relative(value)
    root = _workspace_root(workspace)
    if not (root / requested).resolve().is_relative_to(root):
        raise ValueError("Путь выходит за пределы рабочей папки")
    return ["--", requested.as_posix()]


class GitLogTool:
    id = "git_log"
    name = "История коммитов"
    description = (
        "Показать последние коммиты репозитория рабочей папки: хеш, дату, автора и заголовок. "
        "Только чтение."
    )
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_COMMITS, "description": "Сколько коммитов показать, по умолчанию 20"},
            "path": {"type": "string", "description": "Только коммиты, затронувшие этот файл или папку"},
        },
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            limit = arguments.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= MAX_LOG_COMMITS:
                raise ValueError(f"limit должен быть целым числом от 1 до {MAX_LOG_COMMITS}")
            output = _git(
                workspace, "log", f"--max-count={limit}", "--date=short",
                "--format=%h %ad %an: %s", *_pathspec(workspace, arguments.get("path")),
            )
            return ToolResult(output.strip() or "Коммитов нет")
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)


class GitShowTool:
    id = "git_show"
    name = "Содержимое коммита"
    description = (
        "Показать один коммит: автора, дату, сообщение, список файлов и изменения (diff). "
        "Хеш берите из git_log. Только чтение."
    )
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "commit": {"type": "string", "description": "Хеш коммита, от 4 до 40 шестнадцатеричных символов"},
            "path": {"type": "string", "description": "Показать изменения только этого файла или папки"},
        },
        "required": ["commit"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        return await asyncio.to_thread(self._run, arguments, workspace)

    @staticmethod
    def _run(arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            commit = arguments.get("commit")
            # Только хеш: имена веток и выражения вроде HEAD~1 или --output=… сюда не проходят.
            if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
                raise ValueError("commit должен быть хешем из 4–40 шестнадцатеричных символов")
            output = _git(
                workspace, "show", "--no-ext-diff", "--no-textconv", "--stat", "--patch",
                "--format=fuller", commit, *_pathspec(workspace, arguments.get("path")),
            )
            return ToolResult(output.strip())
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
