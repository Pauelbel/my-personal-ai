"""Управление расширяемыми возможностями: скриптовые инструменты и специализированные агенты."""

import asyncio
from pathlib import Path

from agents.revisions import source_revision
from catalog.service import KINDS, CapabilityService
from connectors.pyenv import check_requirements
from connectors.scripts import MAX_HINT_CHARS, TOOL_ID, WHERE
from connectors.secrets import check_secret_names
from core.tool import ToolResult
from shared.params import parameter_lines, parameter_schema, parse_items

KIND_NAMES = {"tool": "инструмент", "agent": "агент"}
MAX_DESCRIPTION = 200


class ProposeTool:
    id = "propose"
    name = "Предложить инструмент"
    requires_approval = True
    description = (
        "Предложить новый скриптовый инструмент или доработать существующий. Скрипт принимает JSON из stdin и "
        "пишет результат в stdout. Сторонние пакеты перечисляй в requires, секреты читай из os.environ и перечисляй "
        "в secrets. Перед доработкой прочитай текущую версию через read_capability. Пользователь увидит предложение "
        "целиком и сможет утвердить, отклонить или попросить исправить его"
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["tool"]},
            "name": {"type": "string", "description": "id латиницей в snake_case"},
            "title": {"type": "string", "description": "Название по-русски"},
            "description": {"type": "string", "description": "Что делает, одна строка"},
            "reason": {"type": "string", "description": "Какую задачу пользователя решает"},
            "content": {"type": "string", "description": "Код script.py целиком"},
            "parameters": {"type": "string", "description": "Аргументы по строке «- имя — что это»"},
            "hint": {"type": "string", "description": "Короткая подсказка модели по использованию"},
            "requires_approval": {"type": "boolean", "description": "Меняет ли инструмент файлы или систему"},
            "network": {"type": "boolean", "description": "Нужен ли интернет"},
            "requires": {"type": "array", "items": {"type": "string"}, "description": "Пакеты PyPI"},
            "secrets": {"type": "array", "items": {"type": "string"}, "description": "Имена секретов"},
        },
        "required": ["kind", "name", "title", "description", "reason", "content"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    def validate(self, arguments: dict[str, object]) -> str | None:
        _repair(arguments)
        if arguments.get("kind") != "tool":
            return "kind — tool"
        for key in ("title", "description", "reason", "content"):
            if not isinstance(arguments.get(key), str) or not arguments[key].strip():
                return f"поле {key} пустое"
        description = arguments["description"]
        if "\n" in description.strip() or len(description) > MAX_DESCRIPTION:
            return f"description — одна строка до {MAX_DESCRIPTION} символов"
        item_id = arguments.get("name")
        if not isinstance(item_id, str) or not TOOL_ID.fullmatch(item_id):
            return "name — латиница в нижнем регистре, цифры и _ (от 3 символов)"
        if self._capabilities.exists("tool", item_id) and not self._capabilities.is_script_tool(item_id):
            return f"{item_id} — встроенный инструмент, его нельзя заменить"
        try:
            compile(arguments["content"], "script.py", "exec")
        except SyntaxError as exc:
            return f"ошибка синтаксиса в коде, строка {exc.lineno}: {exc.msg}"
        if arguments.get("requires") and (problem := check_requirements(arguments["requires"])):
            return f"requires: {problem}"
        if arguments.get("secrets") and (problem := check_secret_names(arguments["secrets"])):
            return f"secrets: {problem}"
        if arguments.get("parameters") is not None:
            if not isinstance(arguments["parameters"], str):
                return "parameters — текст: по строке «- имя — что это»"
            try:
                parameter_schema(parse_items(arguments["parameters"].splitlines(), "parameters", "Параметры"), "parameters")
            except ValueError as exc:
                return str(exc)
        hint = arguments.get("hint")
        if hint is not None and (not isinstance(hint, str) or len(hint.strip()) > MAX_HINT_CHARS):
            return f"hint — текст до {MAX_HINT_CHARS} символов"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        _repair(arguments)
        item_id = arguments["name"]
        update = self._capabilities.exists("tool", item_id)
        current = self._capabilities.tools.get(item_id) if update else None
        parameters, hint = arguments.get("parameters"), arguments.get("hint")
        proposal = {
            "kind": "tool", "name": item_id, "title": arguments["title"],
            "description": arguments["description"], "reason": arguments["reason"], "content": arguments["content"],
            "parameters": (parameters if parameters is not None else parameter_lines(getattr(current, "parameters", None))).strip(),
            "hint": (hint if hint is not None else getattr(current, "hint", "")).strip(),
            "requires_approval": bool(arguments.get("requires_approval", True)),
            "network": bool(arguments.get("network", False)),
            "requires": _strings(arguments.get("requires")), "secrets": _strings(arguments.get("secrets")),
        }
        if self._capabilities.secrets and proposal["secrets"]:
            stored = set(self._capabilities.secrets.names())
            proposal["missing_secrets"] = [name for name in proposal["secrets"] if name not in stored]
        if self._capabilities.env and proposal["requires"]:
            proposal["missing"] = self._capabilities.env.missing(proposal["requires"])
        if update:
            proposal["previous"] = current_text(self._capabilities, item_id)
            proposal["previous_parameters"] = parameter_lines(current.parameters)
            proposal["previous_hint"] = current.hint
        verb = "Изменить" if update else "Создать"
        return {"kind": "proposal", "title": f"{verb} инструмент «{arguments['title']}» ({item_id})", "update": update, "proposal": proposal}

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(f"Предложение некорректно: {problem}", is_error=True)
        proposal = self.approval_payload(arguments)["proposal"]
        try:
            tool = await asyncio.to_thread(
                self._capabilities.save_tool, proposal["name"], name=proposal["title"],
                description=proposal["description"], reason=proposal["reason"], parameters=proposal["parameters"],
                hint=proposal["hint"], code=proposal["content"], requires_approval=proposal["requires_approval"],
                network=proposal["network"], requires=proposal["requires"], secrets=proposal["secrets"],
            )
        except (OSError, ValueError) as exc:
            return ToolResult(f"Не удалось создать: {exc}", is_error=True)
        missing = self._capabilities.env.missing(list(tool.requires)) if self._capabilities.env else []
        note = f" Сначала установи пакеты: {missing}." if missing else ""
        if absent := proposal.get("missing_secrets"):
            note += f" Заполни доступы ({', '.join(absent)}): {WHERE.replace('настроить', f'{tool.id} → настроить')}."
        return ToolResult(f"Инструмент {tool.id} сохранён ({tool.folder}).{note} Подключи его через activate и проверь.")


class ReadCapabilityTool:
    id = "read_capability"
    name = "Текст возможности"
    requires_approval = False
    description = "Прочитать файлы агента или текущую версию скриптового инструмента перед доработкой"
    parameters = {"type": "object", "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "name": {"type": "string"}}, "required": ["kind", "name"], "additionalProperties": False}

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        kind, item_id = arguments.get("kind"), arguments.get("name")
        if kind not in KINDS or not isinstance(item_id, str) or not self._capabilities.exists(kind, item_id):
            return ToolResult(f"{KIND_NAMES.get(kind, 'возможности')} {item_id} нет среди активных", is_error=True)
        if kind == "agent":
            folder, files = self._capabilities.source("agent", item_id)
            lines = [
                f"Агент {item_id} ({folder}). Файлы можно править в разделе «Возможности → Агенты» "
                "или предложить исправление через propose_agent_patch.",
                f"revision: {source_revision(files)}",
            ]
            for name, content in files.items():
                lines += ["", f"{name}:", content.rstrip()]
            return ToolResult("\n".join(lines))
        tool = self._capabilities.tools.get(item_id)
        if not self._capabilities.is_script_tool(item_id):
            return ToolResult(f"{item_id} — встроенный инструмент; через propose он не меняется. Описание: {tool.description}")
        lines = [f"Инструмент {item_id}.", f"title: {tool.name}", f"description: {tool.description}",
                 "parameters:", parameter_lines(tool.parameters) or "(нет)", "hint:", tool.hint or "(нет)",
                 f"requires_approval: {str(tool.requires_approval).lower()}", f"network: {str(tool.network).lower()}",
                 f"requires: {', '.join(tool.requires) or 'нет'}", f"secrets: {', '.join(tool.secrets) or 'нет'}",
                 "", "content:", current_text(self._capabilities, item_id)]
        return ToolResult("\n".join(lines))


def current_text(capabilities: CapabilityService, item_id: str) -> str:
    script = capabilities.paths.tools / item_id / "script.py"
    return script.read_text(encoding="utf-8") if script.is_file() else ""


class ArchiveTool:
    id = "archive"
    name = "Архив возможностей"
    requires_approval = True
    description = "Убрать агента или скриптовый инструмент в архив либо вернуть его; файлы не удаляются"
    parameters = {"type": "object", "properties": {"action": {"type": "string", "enum": ["archive", "restore"]}, "kind": {"type": "string", "enum": list(KINDS)}, "name": {"type": "string"}, "reason": {"type": "string"}}, "required": ["action", "kind", "name", "reason"], "additionalProperties": False}

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    def validate(self, arguments: dict[str, object]) -> str | None:
        action, kind, item_id = arguments.get("action"), arguments.get("kind"), arguments.get("name")
        if action not in {"archive", "restore"} or kind not in KINDS or not isinstance(item_id, str):
            return "нужны action (archive или restore), kind и name"
        if action == "archive":
            if not self._capabilities.exists(kind, item_id):
                return f"{item_id} нет среди активных"
            if kind == "tool" and not self._capabilities.is_script_tool(item_id):
                return "встроенные инструменты и MCP в архив не убираются"
        elif self._capabilities.exists(kind, item_id):
            return f"{item_id} уже активен"
        elif self._capabilities.archived(kind, item_id) is None and not self._capabilities.in_library(kind, item_id):
            return f"{item_id} нет ни в архиве, ни среди готовых"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        restore = arguments["action"] == "restore"
        ready = restore and self._capabilities.archived(arguments["kind"], arguments["name"]) is None
        verb = "Включить готовый" if ready else "Вернуть из архива" if restore else "Убрать в архив"
        return {"kind": "proposal", "title": f"{verb} {KIND_NAMES[arguments['kind']]} {arguments['name']}", "proposal": {"kind": arguments["kind"], "name": arguments["name"], "action": arguments["action"], "reason": arguments.get("reason", "")}}

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        kind, item_id = arguments["kind"], arguments["name"]
        try:
            if arguments["action"] == "archive":
                await asyncio.to_thread(self._capabilities.archive, kind, item_id)
                return ToolResult(f"{item_id} убран в архив.")
            if (entry := self._capabilities.archived(kind, item_id)) is not None:
                await asyncio.to_thread(self._capabilities.restore, kind, entry)
            else:
                await asyncio.to_thread(self._capabilities.enable, kind, item_id)
            return ToolResult(f"{item_id} снова активен.")
        except (OSError, ValueError) as exc:
            return ToolResult(f"Не получилось: {exc}", is_error=True)


def _repair(arguments: dict[str, object]) -> None:
    for key in ("title", "description"):
        if not isinstance(arguments.get(key), str) or not arguments[key].strip():
            fallback = arguments.get("title") if key == "description" else arguments.get("name")
            if isinstance(fallback, str) and fallback.strip():
                arguments[key] = fallback.strip()
    if isinstance(arguments.get("description"), str):
        arguments["description"] = " ".join(arguments["description"].split())[:MAX_DESCRIPTION]
    if isinstance(arguments.get("parameters"), dict):
        arguments["parameters"] = parameter_lines(arguments["parameters"])


def _strings(value: object) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


class InstallPackagesTool:
    """Ставит пакеты PyPI в общее окружение песочницы, где их видят скрипты инструментов. Каждый раз — с подтверждением."""

    id = "install_packages"
    name = "Установка Python-пакетов"
    requires_approval = True
    description = (
        "Установить пакеты PyPI (например pypdf для PDF, openpyxl для xlsx) в общее окружение песочницы, "
        "чтобы скрипты инструментов и агентов могли их импортировать. Пользователь подтверждает каждую установку. "
        "XML, JSON, CSV и zip разбирает стандартная библиотека — для них пакеты не нужны"
    )
    parameters = {
        "type": "object",
        "properties": {
            "packages": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                         "description": "Имена пакетов PyPI, можно с версией: pypdf, lxml>=5"},
            "reason": {"type": "string", "description": "Зачем: какому инструменту или задаче нужны"},
        },
        "required": ["packages", "reason"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    def validate(self, arguments: dict[str, object]) -> str | None:
        if self._capabilities.env is None:
            return "окружение песочницы не настроено"
        return check_requirements(arguments.get("packages"))

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        packages = _strings(arguments.get("packages"))
        return {"kind": "tool_call", "title": f"Установить в песочницу: {', '.join(packages)}"}

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        return await self._capabilities.env.install(_strings(arguments["packages"]))
