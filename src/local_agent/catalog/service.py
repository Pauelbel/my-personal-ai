"""Возможности агента: инструменты, скиллы и саб-агенты — создание, архив, восстановление и список для UI.

Всё созданное — файлы на диске (data/tools, data/skills, data/agents), которые можно править руками;
реестры в памяти обновляются сразу, без перезапуска.
"""


import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from local_agent.agent import loader
from local_agent.agent.main_agent import MAIN_AGENT_ID
from local_agent.agent.models import Agent
from local_agent.agent.registry import AgentRegistry, SkillRegistry
from local_agent.capabilities import scripts
from local_agent.capabilities.extras import ExtrasStore
from local_agent.capabilities.pyenv import PythonEnv, requirement_name
from local_agent.capabilities.secrets import PUBLIC_SUFFIXES, SecretStore
from local_agent.tools.base import Tool, summary_line
from local_agent.tools.registry import ToolRegistry

# Готовые инструменты-скрипты (коннекторы): при первом запуске кладутся в data/tools, дальше они ваши —
# их можно править, архивировать и удалять; удалённый не возвращается.
BUNDLED_TOOLS = Path(__file__).resolve().parent.parent / "extras" / "tools"
SEEDED_FILE = ".bundled"

KINDS = ("tool", "skill", "agent")
# Запись архива для готового дополнения, которое ещё не включали.
EXTRA_PREFIX = "extra:"


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
        """Инструменты, созданные агентом, подхватываются с диска при старте."""
        self._seed_bundled()
        for tool in scripts.load_script_tools(self.paths.tools):
            self.tools.replace(self._bind(tool))
        for tool_id in self._extras_store.enabled() & set(self.extras):
            self.tools.replace(self.extras[tool_id][1]())

    def _seed_bundled(self) -> None:
        """Кладёт готовый инструмент в data/tools один раз: список уже положенных — в data/tools/.bundled."""
        root = self.paths.tools
        root.mkdir(parents=True, exist_ok=True)
        marker = root / SEEDED_FILE
        seeded = set(marker.read_text(encoding="utf-8").split()) if marker.exists() else set()
        added = []
        for folder in sorted(path for path in BUNDLED_TOOLS.iterdir() if path.is_dir()) if BUNDLED_TOOLS.is_dir() else []:
            if folder.name in seeded:
                continue
            if not (root / folder.name).exists():
                shutil.copytree(folder, root / folder.name, ignore=shutil.ignore_patterns("__pycache__"))
            added.append(folder.name)
        if added:
            marker.write_text("\n".join(sorted(seeded | set(added))) + "\n", encoding="utf-8")

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
        skill = loader.write_skill(self.paths.skills, skill_id, name, description, instructions)
        self.skills.replace(skill)
        return skill

    def save_agent(self, agent: Agent) -> Agent:
        if agent.id == MAIN_AGENT_ID:
            raise ValueError("Имя main занято основным агентом")
        create = self.agents.get(agent.id) is None
        saved = loader.write_agent(
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

    def archive(self, kind: str, item_id: str) -> None:
        if kind == "tool":
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
            loader.archive_skill(self.paths.skills, item_id)
            self.skills.remove(item_id)
        else:
            if self.agents.get(item_id) is None:
                raise FileNotFoundError("Саб-агента нет")
            loader.archive_agent(self.paths.agents, item_id)
            self.agents.remove(item_id)

    def restore(self, kind: str, archive_id: str) -> str:
        if kind == "tool" and archive_id.startswith(EXTRA_PREFIX):
            tool_id = archive_id.removeprefix(EXTRA_PREFIX)
            if tool_id not in self.extras:
                raise FileNotFoundError("Такого дополнения нет")
            self._extras_store.set(tool_id, True)
            self.tools.replace(self.extras[tool_id][1]())
            return tool_id
        if kind == "tool":
            tool = self._bind(scripts.restore_script_tool(self.paths.tools, archive_id))
            self.tools.replace(tool)
            return tool.id
        if kind == "skill":
            skill = loader.restore_skill(self.paths.skills, archive_id)
            self.skills.replace(skill)
            return skill.id
        agent = loader.restore_agent(self.paths.agents, archive_id)
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
                    parsed = loader._parse_skill(draft, item_id)
                else:
                    parsed = loader._parse_agent(draft)
                    loader._check_agent(
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
        if kind == "tool" and archive_id.startswith(EXTRA_PREFIX):
            raise ValueError("Дополнение встроено в приложение: его можно только оставить выключенным")
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
            "tools": tools, "skills": skills, "agents": agents, "archive": archive, "python": python,
            "secrets": self.secret_overview(),
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

    def secret_overview(self) -> list[dict]:
        """Имена секретов без значений: какие заданы и каким инструментам нужны (в том числе ещё не заданные)."""
        stored = set(self.secrets.names()) if self.secrets else set()
        users: dict[str, list[str]] = {name: [] for name in stored}
        for tool in self.tools.all():
            for name in getattr(tool, "secrets", ()):
                users.setdefault(name.removesuffix("?"), []).append(tool.id)
        return [{"name": name, "set": name in stored, "tools": users[name]} for name in sorted(users)]

    def archive_lines(self) -> list[str]:
        """Архив одной строкой на элемент: агент может предложить вернуть нужное."""
        names = {"tool": "инструмент", "skill": "скилл", "agent": "саб-агент"}
        lines = []
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
            extras = [
                (EXTRA_PREFIX + tool_id, tool_id, description) for tool_id, (description, _) in self.extras.items()
                if self.tools.get(tool_id) is None
            ]
            return extras + [
                (archive_id, item_id, item_id) for archive_id, item_id in scripts.archived_script_tools(self.paths.tools)
            ]
        if kind == "skill":
            return [(archive_id, skill.id, skill.name) for archive_id, skill in loader.archived_skills(self.paths.skills)]
        return [(archive_id, agent.id, agent.name) for archive_id, agent in loader.archived_agents(self.paths.agents)]
