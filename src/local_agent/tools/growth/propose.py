"""Как агент наращивает возможности: предложение → карточка у пользователя → утверждение → файлы на диске.

read_capability отдаёт текущую версию скилла (все его файлы) или инструмента — с неё начинается доработка.
propose показывает пользователю предложение целиком (что, зачем, код или текст) через interrupt.
Пользователь утверждает, отклоняет или отвечает текстом — тогда агент дорабатывает предложение
и предлагает заново. archive убирает ненужное в архив и возвращает из него, тоже с утверждением.
"""

import asyncio
import json
from pathlib import Path

from local_agent.catalog.service import KINDS, CapabilityService
from local_agent.connectors.pyenv import check_requirements
from local_agent.connectors.scripts import TOOL_ID, WHERE
from local_agent.connectors.secrets import check_secret_names
from local_agent.core.tool import ToolResult
from local_agent.skills import files as skill_files
from local_agent.steps.files import SCRIPT_NAME, parse_steps

KIND_NAMES = {"tool": "инструмент", "skill": "скилл"}
# Формат шагов: коротко — в описании steps (схема propose всегда в запросе), целиком — в ответе на ошибку.
STEPS_HINT = (
    "«## Нужно от вас» со строками «- имя — что это», затем «## Название шага» и в нём «модель: задание», "
    "«агент: задание» + «инструменты: браузер, файлы», «команда: …» или «скрипт: имя.py» + «параметры: {json}»; "
    "{имя} — значение"
)
STEPS_FORMAT = (
    "steps — шаги в Markdown: раздел «## Нужно от вас» со строками «- имя — что это», затем на каждый шаг раздел "
    "«## Название шага» и в нём строка вида: «модель: задание» (один ответ модели), «агент: задание» + "
    "«инструменты: браузер, файлы» (модель с инструментами), «команда: pytest -q», «инструмент: id» + "
    "«параметры: {json}» или «скрипт: имя.py» + «параметры: {json}» (скрипт из scripts этого скилла). Ещё строки "
    "шага: «файлы: пути» — что прочитать, «сохранить: путь» — куда записать итог, «при ошибке: название шага, 3» — "
    "вернуться к шагу и повторить. {имя} подставляет значение из «Нужно от вас»"
)
MAX_DESCRIPTION = 200
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class ProposeTool:
    id = "propose"
    name = "Предложить возможность"
    requires_approval = True
    description = (
        "Предложить пользователю новую возможность или доработку существующей: tool — Python-скрипт "
        "(аргументы JSON-объектом в stdin, результат в stdout; стандартная библиотека Python, а сторонние пакеты — "
        "только перечисленные в requires: их ставит install_packages с согласия пользователя; схему или граф скрипт "
        "выводит текстом Mermaid; токены и адреса — только из os.environ по именам из secrets, в код их не пиши), "
        "skill — инструкции под тип задач (content), а работа по шагам — ещё steps и scripts. Чтобы доработать "
        "существующее, прочитай его через read_capability и передай его id в name: content заменит текущий целиком, "
        "а steps и scripts скилла меняются, только если ты их передал. Копию под новым именем не создавай. "
        "Пользователь увидит предложение целиком и утвердит, отклонит или ответит; при ответе доработай и предложи заново"
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "name": {"type": "string", "description": "id латиницей: tool — snake_case, skill — kebab-case"},
            "title": {"type": "string", "description": "Название по-русски"},
            "description": {"type": "string", "description": "Что делает, одна строка: по ней ты потом выбираешь возможность"},
            "reason": {"type": "string", "description": "Зачем: какую задачу пользователя это решает"},
            "content": {"type": "string", "description": "tool — код script.py; skill — инструкции SKILL.md: когда "
                        "скилл нужен и как делать работу"},
            "steps": {"type": "string", "description": f"Только skill, необязательно — работа по шагам (steps.md): {STEPS_HINT}"},
            "scripts": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "Только skill: скрипты для шагов «скрипт: имя.py» — имя файла → код Python "
                        "(аргументы JSON в stdin, итог в stdout, ход работы в stderr); при доработке — только изменённые"},
            "parameters": {"type": "object", "description": "Только tool: JSON Schema аргументов (type: object)"},
            "requires_approval": {"type": "boolean", "description": "Только tool: true, если меняет файлы или систему"},
            "network": {"type": "boolean", "description": "Только tool: нужен ли доступ в интернет"},
            "requires": {"type": "array", "items": {"type": "string"},
                         "description": "Только tool: пакеты PyPI, которые импортирует скрипт, например [\"pypdf\"]"},
            "secrets": {"type": "array", "items": {"type": "string"},
                        "description": "Только tool: имена секретов (JIRA_TOKEN…), скрипт читает их из os.environ"},
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
            return "kind — tool или skill"
        for key in ("title", "description", "reason", "content"):
            if not isinstance(arguments.get(key), str) or not arguments[key].strip():
                return f"поле {key} пустое"
        description = arguments["description"]
        if "\n" in description.strip() or len(description) > MAX_DESCRIPTION:
            return f"description — одна строка до {MAX_DESCRIPTION} символов"
        pattern = TOOL_ID if kind == "tool" else skill_files.SKILL_ID
        if not isinstance(item_id, str) or not pattern.fullmatch(item_id):
            return "name: tool — латиница в нижнем регистре, цифры и _ (от 3 символов); skill — латиница, цифры, - и _"
        if kind == "skill":
            return self._check_skill(arguments)
        if self._capabilities.exists("tool", item_id) and not self._capabilities.is_script_tool(item_id):
            return f"{item_id} — встроенный инструмент, его нельзя заменить; выбери другое имя"
        try:
            compile(arguments["content"], "script.py", "exec")
        except SyntaxError as exc:
            return f"ошибка синтаксиса в коде, строка {exc.lineno}: {exc.msg}"
        if arguments.get("requires") and (problem := check_requirements(arguments["requires"])):
            return f"requires: {problem}"
        if arguments.get("secrets") and (problem := check_secret_names(arguments["secrets"])):
            return f"secrets: {problem}"
        schema = arguments.get("parameters") or EMPTY_SCHEMA
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return "parameters — JSON Schema с type: object"
        return None

    def _check_skill(self, arguments: dict[str, object]) -> str | None:
        steps, scripts = arguments.get("steps"), arguments.get("scripts")
        if steps is not None and not isinstance(steps, str):
            return "steps — текст steps.md"
        if scripts is not None and (not isinstance(scripts, dict) or not all(
            isinstance(name, str) and isinstance(code, str) for name, code in scripts.items()
        )):
            return "scripts — объект: имя файла → код Python"
        for name, code in (scripts or {}).items():
            if not SCRIPT_NAME.fullmatch(name):
                return f"имя скрипта «{name}» — файл .py без папок, например testcases.py"
            try:
                compile(code, name, "exec")
            except SyntaxError as exc:
                return f"ошибка синтаксиса в {name}, строка {exc.lineno}: {exc.msg}"
        if isinstance(steps, str) and steps.strip():
            try:
                parsed = parse_steps(steps)
            except ValueError as exc:
                return f"шаги не разобрались: {exc}. Формат: {STEPS_FORMAT}"
            current = self._capabilities.skills.get(arguments["name"])
            have = set(scripts or {}) | {
                name.removeprefix("scripts/") for name in (skill_files.files_of(current.folder) if current and current.folder else {})
            }
            for step in parsed.steps:
                if step.kind == "script" and step.text not in have:
                    return f"шаг «{step.title}» запускает scripts/{step.text}, а такого скрипта нет — передай его в scripts"
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
                "requires": _strings(arguments.get("requires")),
                "secrets": _strings(arguments.get("secrets")),
            }
            if self._capabilities.secrets and proposal["secrets"]:
                stored = set(self._capabilities.secrets.names())
                proposal["missing_secrets"] = [name for name in proposal["secrets"] if name not in stored]
            if self._capabilities.env and proposal["requires"]:
                # Карточка сразу говорит, каких пакетов ещё нет: их поставят отдельным подтверждением.
                proposal["missing"] = self._capabilities.env.missing(proposal["requires"])
        else:
            steps = arguments.get("steps")
            if isinstance(steps, str) and steps.strip():
                # Карточка рисует шаги по порядку и что они будут делать с проектом.
                parsed = parse_steps(steps)
                proposal |= {"steps": steps.strip(), "plan": parsed.view(), "effects": parsed.effects()}
            if arguments.get("scripts"):
                proposal["scripts"] = dict(arguments["scripts"])
        if update:
            # Карточка показывает и текущую версию меняемых частей: видно, что именно меняется.
            proposal["previous"] = current_text(self._capabilities, kind, item_id)
            if kind == "skill":
                files = skill_files.files_of(self._capabilities.skills.get(item_id).folder)
                if "steps" in proposal and skill_files.STEPS_FILE in files:
                    proposal["previous_steps"] = files[skill_files.STEPS_FILE]
                previous = {name: files.get(f"{skill_files.SCRIPTS}/{name}") for name in proposal.get("scripts", {})}
                if previous := {name: code for name, code in previous.items() if code is not None}:
                    proposal["previous_scripts"] = previous
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
                requires=proposal["requires"], secrets=proposal["secrets"],
            )
            missing = self._capabilities.env.missing(list(tool.requires)) if self._capabilities.env else []
            install = f" Сначала попроси установить пакеты: install_packages {missing}." if missing else ""
            if absent := proposal.get("missing_secrets"):
                install += f" Попроси пользователя заполнить доступы ({', '.join(absent)}): {WHERE.replace('настроить', f'{tool.id} → настроить')}."
            return (f"Инструмент {tool.id} создан ({tool.folder}).{install} Он уже в реестре: подключи через activate "
                    "и проверь на деле.")
        skill = self._capabilities.save_skill(
            item_id, proposal["title"], proposal["description"], proposal["content"],
            proposal.get("steps"), proposal.get("scripts"),
        )
        if skill.steps is not None:
            return f"Скилл {skill.id} сохранён. Работу по шагам запускай через run_skill, инструкции — через use_skill."
        return f"Скилл {skill.id} сохранён. Загружай его через use_skill, когда задача подходит."


class ReadCapabilityTool:
    """Без текущей версии агенту нечего дорабатывать: в реестре у возможности только строка описания."""

    id = "read_capability"
    name = "Текст возможности"
    requires_approval = False
    description = (
        "Прочитать текущую версию скилла (инструкции, шаги и скрипты) или инструмента из реестра — перед тем как "
        "дорабатывать его через propose. Текст у пользователя не спрашивай: прочитай здесь"
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "name": {"type": "string", "description": "id из реестра"},
        },
        "required": ["kind", "name"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        kind, item_id = arguments.get("kind"), arguments.get("name")
        if kind not in KINDS or not isinstance(item_id, str) or not self._capabilities.exists(kind, item_id):
            return ToolResult(f"{KIND_NAMES.get(kind, 'возможности')} {item_id} нет среди активных: сверь id с реестром",
                              is_error=True)
        if kind == "tool" and not self._capabilities.is_script_tool(item_id):
            return ToolResult(
                f"{item_id} — встроенный инструмент: его код в приложении, через propose он не меняется. "
                f"Описание: {self._capabilities.tools.get(item_id).description}"
            )
        if kind == "skill":
            return ToolResult(self._skill(item_id))
        tool = self._capabilities.tools.get(item_id)
        lines = [
            f"Инструмент {item_id}.", f"title: {tool.name}", f"description: {tool.description}",
            f"parameters: {json.dumps(tool.parameters, ensure_ascii=False)}",
            f"requires_approval: {str(tool.requires_approval).lower()}", f"network: {str(tool.network).lower()}",
            f"requires: {', '.join(tool.requires) or 'нет'}", f"secrets: {', '.join(tool.secrets) or 'нет'}",
            f"Чтобы изменить: propose с kind tool, name {item_id}, title, description и content — код ниже "
            "целиком, с правками пользователя.",
            "", "content:", current_text(self._capabilities, kind, item_id),
        ]
        return ToolResult("\n".join(lines))

    def _skill(self, item_id: str) -> str:
        skill = self._capabilities.skills.get(item_id)
        files = skill_files.files_of(skill.folder)
        lines = [
            f"Скилл {item_id}.", f"title: {skill.name}", f"description: {skill.description}",
            f"Чтобы изменить: propose с kind skill, name {item_id}, title, description и content — инструкции ниже "
            "целиком, с правками пользователя; steps — шаги целиком, только если меняешь их; scripts — только "
            "изменённые скрипты (имя файла → код целиком).",
            "", "content:", skill.instructions,
        ]
        if skill_files.STEPS_FILE in files:
            lines += ["", "steps:", files[skill_files.STEPS_FILE].strip()]
        for name, code in files.items():
            if name.startswith(f"{skill_files.SCRIPTS}/"):
                lines += ["", f"scripts → {name.removeprefix(skill_files.SCRIPTS + '/')}:", code.rstrip()]
        return "\n".join(lines)


def current_text(capabilities: CapabilityService, kind: str, item_id: str) -> str:
    """То, что propose принимает в content: инструкции скилла или код инструмента."""
    if kind == "skill":
        skill = capabilities.skills.get(item_id)
        return skill.instructions if skill else ""
    script = capabilities.paths.tools / item_id / "script.py"
    return script.read_text(encoding="utf-8") if script.is_file() else ""


class ArchiveTool:
    id = "archive"
    name = "Архив возможностей"
    requires_approval = True
    description = (
        "Убрать ненужный скилл или инструмент-скрипт в архив либо вернуть из архива; restore включает и готовую "
        "заготовку (помечены «готовый» в реестре). Файлы не удаляются. Пользователь подтверждает"
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
            if kind == "tool" and not self._capabilities.is_script_tool(item_id):
                return "встроенные инструменты и инструменты MCP в архив не убираются"
        elif self._capabilities.exists(kind, item_id):
            return f"{item_id} уже активен"
        elif self._capabilities.archived(kind, item_id) is None and not self._capabilities.in_library(kind, item_id):
            return f"{item_id} нет ни в архиве, ни среди готовых"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        restore = arguments["action"] == "restore"
        ready = restore and self._capabilities.archived(arguments["kind"], arguments["name"]) is None
        verb = "Включить готовый" if ready else "Вернуть из архива" if restore else "Убрать в архив"
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
            if (entry := self._capabilities.archived(kind, item_id)) is not None:
                await asyncio.to_thread(self._capabilities.restore, kind, entry)
            else:
                await asyncio.to_thread(self._capabilities.enable, kind, item_id)
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



class InstallPackagesTool:
    """Ставит пакеты PyPI в общее окружение песочницы, где их видят скрипты инструментов. Каждый раз — с подтверждением."""

    id = "install_packages"
    name = "Установка Python-пакетов"
    requires_approval = True
    description = (
        "Установить пакеты PyPI (например pypdf для PDF, openpyxl для xlsx) в общее окружение песочницы, "
        "чтобы скрипты инструментов и скиллов могли их импортировать. Пользователь подтверждает каждую установку. "
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
