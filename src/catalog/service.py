"""Каталог возможностей: специализированные агенты и инструменты.

Всё рабочее — файлы на диске (data/agents, data/tools), которые можно править руками; реестры в памяти обновляются
сразу, без перезапуска. Готовые заготовки лежат в коде рядом со своим видом:
«включить» копирует заготовку в data/ — дальше это обычная возможность.
"""

import shutil
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from agents import files as agent_files
from agents.registry import AgentRegistry
from agents.revisions import agent_revision
from connectors import scripts
from connectors.pyenv import PythonEnv
from connectors.secrets import PUBLIC_SUFFIXES, SecretStore
from core.registry import ToolRegistry
from core.tool import Tool, summary_line
from steps.engine import TOOL_SETS, Toolkit, problems
from steps.workflow import Workflow

KINDS = ("tool", "agent")
AGENT_TEMPLATE = """---
name: Название агента
description: Какую законченную задачу выполняет агент
tools: read_file, search_files
---
Опиши узкую роль, ограничения и требования к результату.
"""
STEPS_TEMPLATE = """Пара слов о том, что получится в итоге.

## Нужно от вас
- папка — где лежит документация
- заметки — что учесть (необязательно)

## Прочитать и выписать главное
модель: Прочитай документацию и выпиши, что в ней описано: функции, ограничения, ошибки.
файлы: {папка}
сохранить: .meepo/summary.md

## Сделать результат
модель: По выписке составь результат. Учти: {заметки}
файлы: .meepo/summary.md
сохранить: result.md
"""
TOOL_FILES = ("tool.md", "script.py")


@dataclass(frozen=True)
class CapabilityPaths:
    tools: Path
    agents: Path | None = None


class CapabilityService:
    def __init__(
        self, tools: ToolRegistry, paths: CapabilityPaths,
        env: PythonEnv | None = None, secrets: SecretStore | None = None,
        agents: AgentRegistry | None = None,
    ) -> None:
        self.tools = tools
        self.paths = paths
        self.agents = agents or AgentRegistry()
        # UI сохраняет файлы в потоке, инструменты — в event loop: проверка версии и запись должны быть едины.
        self._source_lock = RLock()
        # Общее окружение песочницы: в нём пакеты, которые пользователь разрешил поставить скриптам.
        self.env = env
        self.secrets = secrets

    def load(self) -> None:
        """Инструменты-скрипты подхватываются при старте."""
        for tool in scripts.load_script_tools(self.paths.tools):
            self.tools.replace(self._bind(tool))

    # ---------- проверки ----------

    def exists(self, kind: str, item_id: str) -> bool:
        if kind == "tool":
            return self.tools.get(item_id) is not None
        if kind == "agent":
            return self.agents.get(item_id) is not None
        return False

    def toolkit(self, read_only: bool = False) -> Toolkit:
        """Чем пользуются шаги агентов: все инструменты реестра и окружение песочницы для скриптов."""
        return Toolkit(tool=self.tools.get, tools=self.tools.all, read_only=read_only, env=self.env)

    def is_script_tool(self, tool_id: str) -> bool:
        return isinstance(self.tools.get(tool_id), scripts.ScriptTool)

    def archived(self, kind: str, item_id: str) -> str | None:
        """Самая свежая запись архива для id, если есть."""
        entries = [archive_id for archive_id, archived_id, _ in self._archive(kind) if archived_id == item_id]
        return entries[-1] if entries else None

    # ---------- изменения ----------

    def save_tool(self, tool_id: str, **fields) -> scripts.ScriptTool:
        tool = self._bind(scripts.write_script_tool(self.paths.tools, tool_id, **fields))
        self.tools.replace(tool)
        return tool

    def _bind(self, tool: scripts.ScriptTool) -> scripts.ScriptTool:
        tool.env = self.env
        tool.secret_store = self.secrets
        return tool

    def create(self, kind: str, item_id: str, text: str) -> str:
        if kind != "agent" or self.paths.agents is None:
            raise ValueError("Так создаются только агенты")
        with self._source_lock:
            self.agents.replace(agent_files.save_files(
                self.paths.agents, item_id, {agent_files.AGENT_FILE: text}, create=True,
            ))
        return item_id

    @staticmethod
    def templates() -> dict[str, str]:
        return {"agent": AGENT_TEMPLATE, "steps": STEPS_TEMPLATE}

    # ---------- готовые заготовки ----------

    def library(self) -> list[dict]:
        """Готовые, но не включённые: агенты и коннекторы."""
        items = [
            {"kind": "agent", **self._agent_view(agent, ready=True)}
            for agent in agent_files.library_agents() if self.agents.get(agent.id) is None
        ]
        items += [
            {"kind": "tool", "id": tool.id, "name": tool.name, "description": summary_line(tool.description, 200)}
            for tool in scripts.library_tools() if self.tools.get(tool.id) is None
        ]
        return items

    def in_library(self, kind: str, item_id: str) -> bool:
        return any(item["kind"] == kind and item["id"] == item_id for item in self.library())

    def enable(self, kind: str, item_id: str) -> str:
        """Включает готовую заготовку: её папка копируется в data/ и дальше правится как своя."""
        with self._source_lock if kind == "agent" else nullcontext():
            if not self.in_library(kind, item_id):
                raise FileNotFoundError("Такой готовой заготовки нет или она уже включена")
            if kind == "agent":
                library, root = agent_files.LIBRARY, self.paths.agents
            else:
                library, root = scripts.LIBRARY, self.paths.tools
            target = root / item_id
            if target.exists():
                raise FileExistsError("Возможность с таким id уже есть")
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(library / item_id, target, ignore=shutil.ignore_patterns("__pycache__"))
            if kind == "agent":
                self.agents.replace(agent_files.read_agent(target))
            else:
                self.tools.replace(self._bind(scripts.read_script_tool(target)))
        return item_id

    # ---------- архив ----------

    def archive(self, kind: str, item_id: str) -> None:
        if kind == "tool":
            if not self.is_script_tool(item_id):
                raise ValueError("В архив можно убрать только инструмент-скрипт: встроенные и MCP идут с приложением")
            scripts.archive_script_tool(self.paths.tools, item_id)
            self.tools.unregister(item_id)
            return
        if kind == "agent":
            with self._source_lock:
                if self.agents.get(item_id) is None or self.paths.agents is None:
                    raise FileNotFoundError("Агента нет")
                agent_files.archive_agent(self.paths.agents, item_id)
                self.agents.remove(item_id)
            return
        raise ValueError("Неизвестный вид возможности")

    def restore(self, kind: str, archive_id: str) -> str:
        if kind == "tool":
            tool = self._bind(scripts.restore_script_tool(self.paths.tools, archive_id))
            self.tools.replace(tool)
            return tool.id
        if kind == "agent":
            with self._source_lock:
                agent = agent_files.restore_agent(self.paths.agents, archive_id)
                self.agents.replace(agent)
            return agent.id
        raise ValueError("Неизвестный вид возможности")

    def purge(self, kind: str, archive_id: str) -> None:
        """Окончательно удаляет запись архива: вернуть её будет нельзя."""
        if not any(entry == archive_id for entry, _, _ in self._archive(kind)):
            raise FileNotFoundError("Записи архива нет")
        root = self.paths.tools if kind == "tool" else self.paths.agents
        shutil.rmtree(root / ".deleted" / archive_id)

    # ---------- файлы ----------

    def source(self, kind: str, item_id: str) -> tuple[Path, dict[str, str]]:
        """Папка возможности и её файлы как есть: их можно открыть в редакторе и поправить руками."""
        if kind == "tool" and self.is_script_tool(item_id):
            folder = self.paths.tools / item_id
            return folder, {name: (folder / name).read_text(encoding="utf-8") for name in TOOL_FILES}
        if kind == "agent":
            with self._source_lock:
                if (agent := self.agents.get(item_id)) is not None and agent.folder is not None:
                    return agent.folder, agent_files.files_of(agent.folder)
        raise FileNotFoundError("Возможность не найдена или встроена в приложение")

    def save_source(
        self, kind: str, item_id: str, files: dict[str, str | None], *, expected_revision: str | None = None,
    ) -> None:
        """Правка из окна просмотра: файлы сначала проверяются копией, на диск попадают, только если они рабочие."""
        if kind == "tool":
            self._save_tool_source(item_id, files)
            return
        if kind == "agent":
            with self._source_lock:
                current = self.agents.get(item_id)
                if current is None or self.paths.agents is None:
                    raise FileNotFoundError("Агент не найден")
                if expected_revision is not None and agent_revision(current) != expected_revision:
                    raise ValueError("Агент изменился. Прочитай текущую версию и предложи исправление заново")
                self.agents.replace(agent_files.save_files(self.paths.agents, item_id, files))
            return
        raise FileNotFoundError("Возможность не найдена или встроена в приложение")

    def _save_tool_source(self, tool_id: str, files: dict[str, str | None]) -> None:
        if not self.is_script_tool(tool_id):
            raise FileNotFoundError("Править можно только инструмент-скрипт")
        if not files or not set(files) <= set(TOOL_FILES) or any(text is None for text in files.values()):
            raise ValueError(f"Файлы инструмента: {', '.join(TOOL_FILES)}")
        if "script.py" in files:
            try:
                compile(files["script.py"], "script.py", "exec")
            except SyntaxError as exc:
                raise ValueError(f"script.py, строка {exc.lineno}: {exc.msg}") from None
        folder = self.paths.tools / tool_id
        with tempfile.TemporaryDirectory(dir=self.paths.tools, prefix=".edit-") as scratch:
            draft = Path(scratch) / tool_id
            shutil.copytree(folder, draft)
            for name, text in files.items():
                (draft / name).write_text(text, encoding="utf-8", newline="\n")
            try:
                scripts.read_script_tool(draft, tool_id)
            except (OSError, ValueError) as exc:
                raise ValueError(str(exc).replace(str(draft), str(folder))) from None
            for name in files:
                (draft / name).replace(folder / name)
        self.tools.replace(self._bind(scripts.read_script_tool(folder, tool_id)))

    # ---------- обзор для UI ----------

    def overview(self) -> dict:
        tools = [
            {
                "id": tool.id, "name": tool.name, "description": summary_line(tool.description, 200),
                "origin": self._origin(tool),
                "requires_approval": tool.requires_approval,
                "path": str(tool.folder) if isinstance(tool, scripts.ScriptTool) else None,
                **(self._connector(tool) if self._origin(tool) == "connector" else {}),
            }
            for tool in self.tools.all()
        ]
        archive = [
            {"kind": kind, "archive_id": archive_id, "id": item_id, "name": name}
            for kind in KINDS for archive_id, item_id, name in self._archive(kind)
        ]
        packages = self.env.packages() if self.env else {}
        # Пакет нужен инструменту, если он в его requires или в их зависимостях: иначе у зависимостей
        # markitdown стояло бы «не используется» с кнопкой «удалить».
        users: dict[str, list[str]] = {name: [] for name in packages}
        for tool in self.tools.all():
            if isinstance(tool, scripts.ScriptTool) and tool.requires and self.env:
                for name in self.env.needed(list(tool.requires)) & set(users):
                    users[name].append(tool.id)
        python = {
            "path": str(self.env.path) if self.env else None,
            "packages": [{"name": name, "version": version, "tools": users[name]} for name, version in packages.items()],
        }
        agents = [self._agent_view(agent) for agent in self.agents.all()]
        return {
            "agents": agents, "tools": tools, "archive": archive,
            "library": self.library(), "python": python,
        }

    def _agent_view(self, agent, ready: bool = False) -> dict:
        workflow = agent.as_workflow()
        view = {
            **agent.view(), "path": str(agent.folder) if agent.folder else None,
            "files": list(agent_files.files_of(agent.folder)) if agent.folder else [],
            "uses": [] if ready else [{"id": tool_id, "status": self._tool_status(tool_id)} for tool_id in uses(workflow)],
        }
        if agent.steps:
            view |= {"effects": agent.steps.effects(), "problems": [] if ready else problems(workflow, self.toolkit())}
        return view

    def _tool_status(self, tool_id: str) -> str:
        """ready — можно звать; configure — не заданы доступы; missing — инструмента нет (не включён или в архиве)."""
        tool = self.tools.get(tool_id)
        if tool is None:
            return "missing"
        if isinstance(tool, scripts.ScriptTool) and not self._connector(tool)["configured"]:
            return "configure"
        return "ready"

    def _origin(self, tool: Tool) -> str:
        """Три группы инструментов: встроенные, коннекторы (скрипты из data/tools) и MCP."""
        if isinstance(tool, scripts.ScriptTool):
            return "connector"
        return "mcp" if getattr(tool, "server", None) else "builtin"

    def _connector(self, tool: scripts.ScriptTool) -> dict:
        """Поля формы «настроить»: какие доступы нужны и заданы ли (у скрипта без доступов полей нет).
        Адрес и логин не тайна — их значения видны."""
        stored = self.secrets.pick([name.removesuffix("?") for name in tool.secrets]) if self.secrets else {}
        fields = []
        for raw in tool.secrets:
            name = raw.removesuffix("?")
            field = {"name": name, "optional": raw.endswith("?"), "set": name in stored}
            if name.endswith(PUBLIC_SUFFIXES + ("_VERIFY_SSL",)) and name in stored:
                field["value"] = stored[name]
            fields.append(field)
        required = [field for field in fields if not field["optional"]]
        return {
            "fields": fields, "configured": all(field["set"] for field in required),
            # Пакеты из requires, которых нет в песочнице: строка коннектора предлагает их установить.
            "missing": self.env.missing(list(tool.requires)) if self.env else list(tool.requires),
            # Полное описание: в списке — первая фраза, в форме настройки — что коннектор умеет целиком.
            "full_description": tool.description,
            "about": tool.reason,
            "hint": tool.hint,
            # Поля «Попробовать»: по параметру; попробовать можно только инструмент без подтверждения.
            "parameters": [
                {"name": name, "hint": spec.get("description", ""), "number": spec.get("type") == "integer",
                 "optional": name not in (tool.parameters.get("required") or [])}
                for name, spec in (tool.parameters.get("properties") or {}).items() if isinstance(spec, dict)
            ],
            "tryable": not tool.requires_approval,
            # Кнопка «Проверить подключение» — если скрипт понимает action check.
            "checkable": 'action == "check"' in tool.script.read_text(encoding="utf-8"),
        }

    def archive_lines(self) -> list[str]:
        """Архив и готовые заготовки одной строкой на элемент: агент может предложить вернуть или включить нужное."""
        names = {"tool": "инструмент", "agent": "агент"}
        lines = [f"- {names[item['kind']]} {item['id']} — {summary_line(item['description'] or item['name'])} (готовый)"
                 for item in self.library()]
        for kind in KINDS:
            seen = set()
            for _archive_id, item_id, name in reversed(self._archive(kind)):
                if item_id in seen or self.exists(kind, item_id):
                    continue
                seen.add(item_id)
                lines.append(f"- {names[kind]} {item_id} — {summary_line(name)}")
        return lines

    def _archive(self, kind: str) -> list[tuple[str, str, str]]:
        if kind == "tool":
            return [
                (archive_id, item_id, item_id) for archive_id, item_id in scripts.archived_script_tools(self.paths.tools)
            ]
        if kind == "agent":
            return [(archive_id, agent.id, agent.name) for archive_id, agent in agent_files.archived_agents(self.paths.agents)]
        return []


def uses(workflow: Workflow) -> list[str]:
    """Инструменты, которые зовут шаги процесса: «инструмент: id» и инструменты шага-агента (кроме наборов
    вроде «браузер» и «файлы»)."""
    if workflow.steps is None:
        return []
    found: list[str] = []
    for step in workflow.steps.steps:
        names = [step.text] if step.kind == "tool" else list(step.tools) if step.kind == "agent" else []
        for name in names:
            if name.casefold() not in TOOL_SETS and name not in found:
                found.append(name)
    return found
