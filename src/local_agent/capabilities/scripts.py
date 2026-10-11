"""Инструменты, которые написал агент: папка data/tools/<id>/ с тремя файлами, их можно править руками.

- tool.md — заголовок с параметрами (name, description, requires_approval, network, timeout)
  и текст: зачем инструмент нужен;
- parameters.json — JSON Schema аргументов;
- script.py — сам скрипт: аргументы приходят JSON-объектом в stdin, результат — в stdout.

Архив — data/tools/.deleted/<id>.<uuid>/, как у навыков и агентов.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from local_agent.capabilities.sandbox import DEFAULT_TIMEOUT_SECONDS, SandboxPolicy, run_script
from local_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)

TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
ARCHIVED = re.compile(r"(?P<id>[a-z][a-z0-9_]{2,40})\.[0-9a-f]{32}")
TOOL_KEYS = {"name", "description", "requires_approval", "network", "timeout"}
MAX_TIMEOUT = 300


@dataclass
class ScriptTool:
    id: str
    name: str
    description: str
    parameters: dict
    requires_approval: bool
    network: bool
    timeout: int
    folder: Path
    reason: str = ""

    @property
    def script(self) -> Path:
        return self.folder / "script.py"

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        policy = SandboxPolicy(self.timeout, self.network, writable_workspace=self.requires_approval)
        return await run_script(self.script, arguments, workspace, policy)


def load_script_tools(root: Path) -> list[ScriptTool]:
    """Битая папка инструмента не роняет приложение: она пропускается с предупреждением в логе."""
    root.mkdir(parents=True, exist_ok=True)
    tools = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        try:
            tools.append(read_script_tool(folder))
        except (OSError, ValueError) as exc:
            logger.warning("Инструмент %s не загружен: %s", folder.name, exc)
    return tools


def read_script_tool(folder: Path, tool_id: str | None = None) -> ScriptTool:
    tool_id = tool_id or folder.name
    if not TOOL_ID.fullmatch(tool_id):
        raise ValueError("id инструмента — латиница в нижнем регистре, цифры и _, от 3 символов")
    text = (folder / "tool.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        raise ValueError("tool.md должен начинаться с блока --- параметры ---")
    header, body = text[4:].split("\n---\n", 1)
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or key.strip() not in TOOL_KEYS:
            raise ValueError(f"неизвестный параметр «{line.strip()}»")
        fields[key.strip()] = value.strip()
    if not fields.get("description"):
        raise ValueError("нет description")
    parameters = json.loads((folder / "parameters.json").read_text(encoding="utf-8"))
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ValueError("parameters.json — JSON Schema с type: object")
    if not (folder / "script.py").is_file():
        raise ValueError("нет script.py")
    timeout = int(fields.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    return ScriptTool(
        id=tool_id,
        name=fields.get("name") or tool_id,
        description=fields["description"],
        parameters=parameters,
        requires_approval=fields.get("requires_approval", "true").lower() != "false",
        network=fields.get("network", "false").lower() == "true",
        timeout=max(1, min(timeout, MAX_TIMEOUT)),
        folder=folder,
        reason=body.strip(),
    )


def write_script_tool(
    root: Path, tool_id: str, *, name: str, description: str, reason: str, parameters: dict, code: str,
    requires_approval: bool, network: bool, timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> ScriptTool:
    """Создаёт или перезаписывает папку инструмента; файлы пишутся во временную папку и подменяют старую разом."""
    if not TOOL_ID.fullmatch(tool_id):
        raise ValueError("Недопустимый id инструмента")
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{tool_id}.{uuid4().hex}"
    staging.mkdir()
    header = [
        f"name: {_one_line(name)}", f"description: {_one_line(description)}",
        f"requires_approval: {'true' if requires_approval else 'false'}",
        f"network: {'true' if network else 'false'}", f"timeout: {timeout}",
    ]
    (staging / "tool.md").write_text("---\n" + "\n".join(header) + "\n---\n" + reason.strip() + "\n", encoding="utf-8")
    (staging / "parameters.json").write_text(json.dumps(parameters, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (staging / "script.py").write_text(code.rstrip() + "\n", encoding="utf-8")
    target = root / tool_id
    if target.exists():
        archive_script_tool(root, tool_id)
    staging.rename(target)
    return read_script_tool(target)


def archive_script_tool(root: Path, tool_id: str) -> None:
    if not TOOL_ID.fullmatch(tool_id) or not (root / tool_id).is_dir():
        raise FileNotFoundError("Инструмент не найден")
    archive = root / ".deleted"
    archive.mkdir(exist_ok=True)
    (root / tool_id).rename(archive / f"{tool_id}.{uuid4().hex}")


def archived_script_tools(root: Path) -> list[tuple[str, str]]:
    """(id записи архива, id инструмента)."""
    archive = root / ".deleted"
    if not archive.is_dir():
        return []
    return [
        (path.name, match["id"]) for path in sorted(archive.iterdir())
        if path.is_dir() and (match := ARCHIVED.fullmatch(path.name))
    ]


def restore_script_tool(root: Path, archive_id: str) -> ScriptTool:
    match = ARCHIVED.fullmatch(archive_id)
    source = root / ".deleted" / archive_id
    if match is None or not source.is_dir():
        raise FileNotFoundError("Записи архива нет")
    target = root / match["id"]
    if target.exists():
        raise FileExistsError("Инструмент с таким id уже есть")
    source.rename(target)
    return read_script_tool(target)


def _one_line(value: str) -> str:
    return " ".join(value.split())
