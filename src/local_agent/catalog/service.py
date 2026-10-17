"""Каталог возможностей: инструменты, скиллы и агенты — создание, правка, архив, готовые заготовки и список для UI.

Всё рабочее — файлы на диске (data/tools, data/skills, data/agents), которые можно править руками;
реестры в памяти обновляются сразу, без перезапуска. Готовые заготовки лежат в коде рядом со своим видом
(skills/library, connectors/library) и встроенные дополнения (tools/testgen): «включить» копирует заготовку
в data/ или включает дополнение — дальше это обычная возможность.
"""


import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from local_agent.agents import files as agent_files
from local_agent.agents.main import MAIN_AGENT_ID
from local_agent.agents.models import Agent
from local_agent.agents.registry import AgentRegistry
from local_agent.catalog.extras import ExtrasStore
from local_agent.connectors import scripts
from local_agent.connectors.pyenv import PythonEnv, requirement_name
from local_agent.connectors.secrets import PUBLIC_SUFFIXES, SecretStore
from local_agent.core.registry import ToolRegistry
from local_agent.core.tool import Tool, summary_line
from local_agent.skills import files as skill_files
from local_agent.skills.registry import SkillRegistry

KINDS = ("tool", "skill", "agent")


@dataclass(frozen=True)
class CapabilityPaths:
    tools: Path
    skills: Path
    agents: Path


class CapabilityService:
    def __init__(
        self, tools: ToolRegistry, skills: SkillRegistry, agents: AgentRegistry, paths: CapabilityPaths,
        extras: dict[str, tuple[str, Callable[[], Tool]]] | None = None, env: PythonEnv | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self.tools = tools
        self.skills = skills
        self.agents = agents
        self.paths = paths
        # Готовые дополнения: id → (описание, фабрика); включённые помнит extras.json.
        self.extras = extras or {}
        self._extras_store = ExtrasStore(paths.tools / "extras.json")
        # Общее окружение песочницы: в нём пакеты, которые пользователь разрешил поставить скриптам.
        self.env = env
        self.secrets = secrets

    def load(self) -> None:
        """Инструменты-скрипты и включённые дополнения подхватываются при старте."""
        for tool in scripts.load_script_tools(self.paths.tools):
            self.tools.replace(self._bind(tool))
        for tool_id in self._extras_store.enabled() & set(self.extras):
            self.tools.replace(self.extras[tool_id][1]())

    # ---------- проверки ----------

    def exists(self, kind: str, item_id: str) -> bool:
        if kind == "tool":
            return self.tools.get(item_id) is not None
        if kind == "skill":
            return self.skills.get(item_id) is not None
        return self.agents.get(item_id) is not None

    def is_script_tool(self, tool_id: str) -> bool:
        return isinstance(self.tools.get(tool_id), scripts.ScriptTool)

    def archived(self, kind: str, item_id: str) -> str | None:
        """Самая свежая запись архива для id, если есть."""
        entries = [archive_id for archive_id, archived_id, _ in self._archive(kind) if archived_id == item_id]
        return entries[-1] if entries else None

    # ---------- изменения ----------

    def is_extra(self, tool_id: str) -> bool:
        return tool_id in self.extras

    def save_tool(self, tool_id: str, **fields) -> scripts.ScriptTool:
        tool = self._bind(scripts.write_script_tool(self.paths.tools, tool_id, **fields))
        self.tools.replace(tool)
        return tool

    def _bind(self, tool: scripts.ScriptTool) -> scripts.ScriptTool:
        tool.env = self.env
        tool.secret_store = self.secrets
        return tool

    def save_skill(self, skill_id: str, name: str, description: str, instructions: str):
        skill = skill_files.write_skill(self.paths.skills, skill_id, name, description, instructions)
        self.skills.replace(skill)
        return skill

    def save_agent(self, agent: Agent) -> Agent:
        if agent.id == MAIN_AGENT_ID:
            raise ValueError("Имя main занято основным агентом")
        create = self.agents.get(agent.id) is None
        saved = agent_files.write_agent(
            self.paths.agents, agent,
            known_tools={tool.id for tool in self.tools.all()},
            known_skills={skill.id for skill in self.skills.all()},
            create=create,
        )
        self.agents.replace(saved)
        return saved

    def link(self, source: str, target: str, connected: bool) -> Agent:
        """Связь на холсте: source может спрашивать target посреди своей задачи. Хранится в файле source."""
        agent = self.agents.get(source)
        if agent is None or self.agents.get(target) is None:
            raise ValueError("Связать можно только двух саб-агентов")
        if source == target:
            raise ValueError("Агент не может спрашивать сам себя")
        others = [item for item in agent.agents if item != target]
        return self.save_agent(replace(agent, agents=tuple(others + [target] if connected else others)))

    # ---------- готовые заготовки ----------

    def library(self) -> list[dict]:
        """Готовые, но не включённые: скиллы из skills/library, коннекторы из connectors/library, дополнения."""
        items = [
            {"kind": "skill", "id": skill.id, "name": skill.name, "description": skill.description}
            for skill in skill_files.library_skills() if self.skills.get(skill.id) is None
        ]
        items += [
            {"kind": "tool", "id": tool.id, "name": tool.name, "description": summary_line(tool.description, 200)}
            for tool in scripts.library_tools() if self.tools.get(tool.id) is None
        ]
        items += [
            {"kind": "tool", "id": tool_id, "name": tool_id, "description": description}
            for tool_id, (description, _) in self.extras.items() if self.tools.get(tool_id) is None
        ]
        return items

    def in_library(self, kind: str, item_id: str) -> bool:
        return any(item["kind"] == kind and item["id"] == item_id for item in self.library())

    def enable(self, kind: str, item_id: str) -> str:
        """Включает готовую заготовку: скилл и коннектор копируются в data/ и дальше правятся как свои."""
        if not self.in_library(kind, item_id):
            raise FileNotFoundError("Такой готовой заготовки нет или она уже включена")
        if kind == "skill":
            target = self.paths.skills / f"{item_id}.md"
            if target.exists():
                raise FileExistsError("Скилл с таким id уже есть")
            self.paths.skills.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(skill_files.LIBRARY / f"{item_id}.md", target)
            self.skills.replace(skill_files.parse_skill(target))
        elif item_id in self.extras:
            self._extras_store.set(item_id, True)
            self.tools.replace(self.extras[item_id][1]())
        else:
            target = self.paths.tools / item_id
            if target.exists():
                raise FileExistsError("Инструмент с таким id уже есть")
            shutil.copytree(scripts.LIBRARY / item_id, target, ignore=shutil.ignore_patterns("__pycache__"))
            self.tools.replace(self._bind(scripts.read_script_tool(target)))
        return item_id

    # ---------- архив ----------

    def archive(self, kind: str, item_id: str) -> None:
        if kind == "tool":
            # Дополнение не удаляется, а выключается: оно снова появится среди готовых.
            if self.is_extra(item_id) and self.tools.get(item_id) is not None:
                self._extras_store.set(item_id, False)
                self.tools.unregister(item_id)
                return
            if not self.is_script_tool(item_id):
                raise ValueError("В архив можно убрать только инструмент, созданный агентом, или дополнение")
            scripts.archive_script_tool(self.paths.tools, item_id)
            self.tools.unregister(item_id)
        elif kind == "skill":
            if self.skills.get(item_id) is None:
                raise FileNotFoundError("Скилла нет")
            skill_files.archive_skill(self.paths.skills, item_id)
            self.skills.remove(item_id)
        else:
            if self.agents.get(item_id) is None:
                raise FileNotFoundError("Саб-агента нет")
            agent_files.archive_agent(self.paths.agents, item_id)
            self.agents.remove(item_id)

    def restore(self, kind: str, archive_id: str) -> str:
        if kind == "tool":
            tool = self._bind(scripts.restore_script_tool(self.paths.tools, archive_id))
            self.tools.replace(tool)
            return tool.id
        if kind == "skill":
            skill = skill_files.restore_skill(self.paths.skills, archive_id)
            self.skills.replace(skill)
            return skill.id
        agent = agent_files.restore_agent(self.paths.agents, archive_id)
        self.agents.replace(agent)
        return agent.id

    def save_source(self, kind: str, item_id: str, files: dict[str, str]) -> None:
        """Правка из окна просмотра: файлы сначала проверяются копией, на диск попадают, только если они рабочие."""
        if kind == "tool":
            self._save_tool_source(item_id, files)
            return
        if kind not in {"skill", "agent"} or not self.exists(kind, item_id):
            raise FileNotFoundError("Возможность не найдена или встроена в приложение")
        root = self.paths.skills if kind == "skill" else self.paths.agents
        name = f"{item_id}.md"
        if set(files) != {name}:
            raise ValueError(f"Ожидается один файл {name}")
        target = root / name
        with tempfile.TemporaryDirectory(dir=root, prefix=".edit-") as scratch:
            draft = Path(scratch) / name
            draft.write_text(files[name], encoding="utf-8", newline="\n")
            try:
                if kind == "skill":
                    parsed = skill_files.parse_skill(draft, item_id)
                else:
                    parsed = agent_files.parse_agent(draft)
                    agent_files.check_agent(
                        parsed, {tool.id for tool in self.tools.all()}, {skill.id for skill in self.skills.all()},
                    )
            except ValueError as exc:
                raise ValueError(str(exc).replace(str(draft), str(target))) from None
            draft.replace(target)
        (self.skills if kind == "skill" else self.agents).replace(parsed)

    def _save_tool_source(self, tool_id: str, files: dict[str, str]) -> None:
        if not self.is_script_tool(tool_id):
            raise FileNotFoundError("Править можно только инструмент, созданный агентом")
        allowed = {"tool.md", "parameters.json", "script.py"}
        if not files or not set(files) <= allowed:
            raise ValueError(f"Файлы инструмента: {', '.join(sorted(allowed))}")
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

    def purge(self, kind: str, archive_id: str) -> None:
        """Окончательно удаляет запись архива: вернуть её будет нельзя."""
        if not any(entry == archive_id for entry, _, _ in self._archive(kind)):
            raise FileNotFoundError("Записи архива нет")
        root = {"tool": self.paths.tools, "skill": self.paths.skills, "agent": self.paths.agents}[kind]
        path = root / ".deleted" / archive_id
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

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
        skills = [
            {"id": skill.id, "name": skill.name, "description": skill.description,
             "path": str(self.paths.skills / f"{skill.id}.md")}
            for skill in self.skills.all()
        ]
        agents = [
            {"id": agent.id, "name": agent.name, "description": agent.description, "tools": list(agent.tools),
             "skills": list(agent.skills or ()), "path": str(self.paths.agents / f"{agent.id}.md")}
            for agent in self.agents.all()
        ]
        archive = [
            {"kind": kind, "archive_id": archive_id, "id": item_id, "name": name}
            for kind in KINDS for archive_id, item_id, name in self._archive(kind)
        ]
        packages = self.env.packages() if self.env else {}
        users = {
            name: [tool.id for tool in self.tools.all() if isinstance(tool, scripts.ScriptTool)
                   and name in {requirement_name(spec) for spec in tool.requires}]
            for name in packages
        }
        python = {
            "path": str(self.env.path) if self.env else None,
            "packages": [{"name": name, "version": version, "tools": users[name]} for name, version in packages.items()],
        }
        return {
            "tools": tools, "skills": skills, "agents": agents, "archive": archive, "library": self.library(),
            "python": python,
        }

    def _origin(self, tool: Tool) -> str:
        if isinstance(tool, scripts.ScriptTool):
            # Скрипт, которому нужны доступы (адрес, логин, токен), — коннектор: его настраивают формой.
            return "connector" if tool.secrets else "agent"
        return "mcp" if getattr(tool, "server", None) else "extra" if self.is_extra(tool.id) else "builtin"

    def _connector(self, tool: scripts.ScriptTool) -> dict:
        """Поля формы «настроить»: какие доступы нужны и заданы ли. Адрес и логин не тайна — их значения видны."""
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
            # Полное описание: в списке — первая фраза, в форме настройки — что коннектор умеет целиком.
            "full_description": tool.description,
            "about": tool.reason,
            # Кнопка «Проверить подключение» — если скрипт понимает action check.
            "checkable": 'action == "check"' in tool.script.read_text(encoding="utf-8"),
        }

    def archive_lines(self) -> list[str]:
        """Архив и готовые заготовки одной строкой на элемент: агент может предложить вернуть или включить нужное."""
        names = {"tool": "инструмент", "skill": "скилл", "agent": "саб-агент"}
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
        if kind == "skill":
            return [(archive_id, skill.id, skill.name) for archive_id, skill in skill_files.archived_skills(self.paths.skills)]
        return [(archive_id, agent.id, agent.name) for archive_id, agent in agent_files.archived_agents(self.paths.agents)]
