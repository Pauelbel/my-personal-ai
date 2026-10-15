"""Как агент наращивает возможности: предложение → карточка у пользователя → утверждение → файлы на диске.

propose показывает пользователю предложение целиком (что, зачем, код или текст) через interrupt.
Пользователь утверждает, отклоняет или отвечает текстом — тогда агент дорабатывает предложение
и предлагает заново. archive убирает ненужное в архив и возвращает из него, тоже с утверждением.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

from local_agent.agent.loader import AGENT_ID, SKILL_ID
from local_agent.agent.main_agent import MAIN_AGENT_ID
from local_agent.agent.models import Agent
from local_agent.capabilities.scripts import TOOL_ID
from local_agent.capabilities.service import KINDS, CapabilityService
from local_agent.tools.base import ToolResult

KIND_NAMES = {"tool": "инструмент", "skill": "скилл", "agent": "саб-агента"}
MAX_DESCRIPTION = 200
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class ProposeTool:
    id = "propose"
    name = "Предложить возможность"
    requires_approval = True
    description = (
        "Предложить пользователю новую возможность или доработку существующей: tool — Python-скрипт "
        "(аргументы JSON-объектом в stdin, результат в stdout; только стандартная библиотека Python — ставить "
        "пакеты нельзя, а схему или граф скрипт выводит текстом Mermaid), skill — инструкция под тип задач, "
        "agent — саб-агент с постоянной ролью. Чтобы доработать существующее, передай его id в name: "
        "предложение заменит его целиком, копию под новым именем не создавай. Пользователь увидит предложение "
        "целиком и утвердит, отклонит или ответит; при ответе доработай и предложи заново"
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "name": {"type": "string", "description": "id латиницей: tool — snake_case, skill и agent — kebab-case"},
            "title": {"type": "string", "description": "Название по-русски"},
            "description": {"type": "string", "description": "Что делает, одна строка: по ней ты потом выбираешь возможность"},
            "reason": {"type": "string", "description": "Зачем: какую задачу пользователя это решает"},
            "content": {"type": "string", "description": "tool — код script.py; skill — инструкции по шагам; agent — его системный промпт"},
            "parameters": {"type": "object", "description": "Только tool: JSON Schema аргументов (type: object)"},
            "requires_approval": {"type": "boolean", "description": "Только tool: true, если меняет файлы или систему"},
            "network": {"type": "boolean", "description": "Только tool: нужен ли доступ в интернет"},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "Только agent: id инструментов; при доработке без поля остаются прежние"},
            "skills": {"type": "array", "items": {"type": "string"},
                       "description": "Только agent: id скиллов; при доработке без поля остаются прежние"},
        },
        "required": ["kind", "name", "title", "description", "reason", "content"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    def validate(self, arguments: dict[str, object]) -> str | None:
        _repair(arguments)
        kind, item_id = arguments.get("kind"), arguments.get("name")
        if kind not in KINDS:
            return "kind — tool, skill или agent"
        for key in ("title", "description", "reason", "content"):
            if not isinstance(arguments.get(key), str) or not arguments[key].strip():
                return f"поле {key} пустое"
        description = arguments["description"]
        if "\n" in description.strip() or len(description) > MAX_DESCRIPTION:
            return f"description — одна строка до {MAX_DESCRIPTION} символов"
        pattern = {"tool": TOOL_ID, "skill": SKILL_ID, "agent": AGENT_ID}[kind]
        if not isinstance(item_id, str) or not pattern.fullmatch(item_id):
            return "name: tool — латиница в нижнем регистре, цифры и _ (от 3 символов); skill и agent — латиница, цифры, - и _"
        if kind == "tool":
            if self._capabilities.exists("tool", item_id) and not self._capabilities.is_script_tool(item_id):
                return f"{item_id} — встроенный инструмент, его нельзя заменить; выбери другое имя"
            try:
                compile(arguments["content"], "script.py", "exec")
            except SyntaxError as exc:
                return f"ошибка синтаксиса в коде, строка {exc.lineno}: {exc.msg}"
            schema = arguments.get("parameters") or EMPTY_SCHEMA
            if not isinstance(schema, dict) or schema.get("type") != "object":
                return "parameters — JSON Schema с type: object"
        if kind == "agent":
            if item_id == MAIN_AGENT_ID:
                return "имя main занято основным агентом"
            known_tools = {tool.id for tool in self._capabilities.tools.all()}
            if unknown := sorted(set(_strings(arguments.get("tools"))) - known_tools):
                return f"таких инструментов нет: {', '.join(unknown)}; сначала предложи их создать"
            known_skills = {skill.id for skill in self._capabilities.skills.all()}
            if unknown := sorted(set(_strings(arguments.get("skills"))) - known_skills):
                return f"таких скиллов нет: {', '.join(unknown)}; сначала предложи их создать"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        _repair(arguments)
        kind, item_id = arguments["kind"], arguments["name"]
        update = self._capabilities.exists(kind, item_id)
        proposal = {
            "kind": kind, "name": item_id, "title": arguments["title"], "description": arguments["description"],
            "reason": arguments["reason"], "content": arguments["content"],
        }
        if kind == "tool":
            proposal |= {
                "parameters": arguments.get("parameters") or EMPTY_SCHEMA,
                "requires_approval": bool(arguments.get("requires_approval", True)),
                "network": bool(arguments.get("network", False)),
            }
        if kind == "agent":
            current = self._capabilities.agents.get(item_id)
            proposal |= {
                "tools": _strings(arguments["tools"]) if "tools" in arguments or current is None else list(current.tools),
                "skills": _strings(arguments["skills"]) if "skills" in arguments or current is None
                else list(current.skills or ()),
            }
        if update:
            # Карточка показывает и текущую версию: видно, что именно меняется.
            proposal["previous"] = self._current_text(kind, item_id)
        verb = "Изменить" if update else "Создать"
        return {
            "kind": "proposal",
            "title": f"{verb} {KIND_NAMES[kind]} «{arguments['title']}» ({item_id})",
            "update": update,
            "proposal": proposal,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        problem = self.validate(arguments)
        if problem:
            return ToolResult(f"Предложение некорректно: {problem}", is_error=True)
        proposal = self.approval_payload(arguments)["proposal"]
        try:
            created = await asyncio.to_thread(self._create, proposal)
        except (OSError, ValueError) as exc:
            return ToolResult(f"Не удалось создать: {exc}", is_error=True)
        return ToolResult(created)

    def _create(self, proposal: dict) -> str:
        kind, item_id = proposal["kind"], proposal["name"]
        if kind == "tool":
            tool = self._capabilities.save_tool(
                item_id, name=proposal["title"], description=proposal["description"], reason=proposal["reason"],
                parameters=proposal["parameters"], code=proposal["content"],
                requires_approval=proposal["requires_approval"], network=proposal["network"],
            )
            return (f"Инструмент {tool.id} создан ({tool.folder}). Он уже в реестре: подключи через activate "
                    "и проверь на деле.")
        if kind == "skill":
            skill = self._capabilities.save_skill(item_id, proposal["title"], proposal["description"], proposal["content"])
            return f"Скилл {skill.id} сохранён. Загружай его через use_skill, когда задача подходит."
        fields = dict(
            name=proposal["title"], system_prompt=proposal["content"], description=proposal["description"],
            tools=tuple(proposal["tools"]), skills=tuple(proposal["skills"]),
        )
        current = self._capabilities.agents.get(item_id)
        # Доработка меняет роль, а связи с холста, закреплённая модель и лимит раундов остаются.
        agent = self._capabilities.save_agent(
            replace(current, **fields) if current else Agent(id=item_id, llm_provider="lm_studio", model="", **fields)
        )
        return f"Саб-агент {agent.id} {'обновлён' if current else 'готов'}. Поручай ему задачи через delegate."

    def _current_text(self, kind: str, item_id: str) -> str:
        if kind == "agent":
            agent = self._capabilities.agents.get(item_id)
            return agent.system_prompt if agent else ""
        if kind == "skill":
            skill = self._capabilities.skills.get(item_id)
            return skill.instructions if skill else ""
        script = self._capabilities.paths.tools / item_id / "script.py"
        return script.read_text(encoding="utf-8") if script.is_file() else ""


class ArchiveTool:
    id = "archive"
    name = "Архив возможностей"
    requires_approval = True
    description = (
        "Убрать ненужный инструмент (созданный агентом), скилл или саб-агента в архив либо вернуть из архива. "
        "Файлы не удаляются. Пользователь подтверждает"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["archive", "restore"]},
            "kind": {"type": "string", "enum": list(KINDS)},
            "name": {"type": "string", "description": "id возможности"},
            "reason": {"type": "string", "description": "Почему"},
        },
        "required": ["action", "kind", "name", "reason"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    def validate(self, arguments: dict[str, object]) -> str | None:
        action, kind, item_id = arguments.get("action"), arguments.get("kind"), arguments.get("name")
        if action not in {"archive", "restore"} or kind not in KINDS or not isinstance(item_id, str):
            return "нужны action (archive или restore), kind и name"
        if action == "archive":
            if not self._capabilities.exists(kind, item_id):
                return f"{item_id} нет среди активных"
            if kind == "tool" and not (self._capabilities.is_script_tool(item_id) or self._capabilities.is_extra(item_id)):
                return "встроенные инструменты в архив не убираются"
        elif self._capabilities.archived(kind, item_id) is None:
            return f"{item_id} нет в архиве"
        elif self._capabilities.exists(kind, item_id):
            return f"{item_id} уже активен"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        verb = "Убрать в архив" if arguments["action"] == "archive" else "Вернуть из архива"
        return {
            "kind": "proposal",
            "title": f"{verb} {KIND_NAMES[arguments['kind']]} {arguments['name']}",
            "proposal": {"kind": arguments["kind"], "name": arguments["name"], "action": arguments["action"],
                         "reason": arguments.get("reason", "")},
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        kind, item_id = arguments["kind"], arguments["name"]
        try:
            if arguments["action"] == "archive":
                await asyncio.to_thread(self._capabilities.archive, kind, item_id)
                return ToolResult(f"{item_id} убран в архив. Вернуть можно через archive с action restore.")
            await asyncio.to_thread(self._capabilities.restore, kind, self._capabilities.archived(kind, item_id))
            return ToolResult(f"{item_id} снова активен.")
        except (OSError, ValueError) as exc:
            return ToolResult(f"Не получилось: {exc}", is_error=True)


def _repair(arguments: dict[str, object]) -> None:
    """Мелкие промахи маленькой модели чиним сами, а не гоняем её по кругу:
    нет описания — берём название, кривой required в схеме — выводим из properties."""
    for key in ("title", "description"):
        if not isinstance(arguments.get(key), str) or not arguments[key].strip():
            fallback = arguments.get("title") if key == "description" else arguments.get("name")
            if isinstance(fallback, str) and fallback.strip():
                arguments[key] = fallback.strip()
    if isinstance(arguments.get("description"), str):
        arguments["description"] = " ".join(arguments["description"].split())[:MAX_DESCRIPTION]
    schema = arguments.get("parameters")
    if isinstance(schema, dict):
        properties = schema.get("properties")
        schema["properties"] = properties if isinstance(properties, dict) else {}
        schema.setdefault("type", "object")
        required = schema.get("required")
        if required is not None and not (isinstance(required, list) and all(isinstance(item, str) for item in required)):
            schema["required"] = list(schema["properties"])


def _strings(value: object) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []

