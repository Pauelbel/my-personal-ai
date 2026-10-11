"""Возможности агента: инструменты, скиллы и саб-агенты — создание, архив, восстановление и список для UI.

Всё созданное — файлы на диске (data/tools, data/skills, data/agents), которые можно править руками;
реестры в памяти обновляются сразу, без перезапуска.
"""


from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from local_agent.agent import loader
from local_agent.agent.main_agent import MAIN_AGENT_ID
from local_agent.agent.models import Agent
from local_agent.agent.registry import AgentRegistry, SkillRegistry
from local_agent.capabilities import scripts
from local_agent.capabilities.extras import ExtrasStore
from local_agent.tools.base import Tool, summary_line
from local_agent.tools.registry import ToolRegistry

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
        extras: dict[str, tuple[str, Callable[[], Tool]]] | None = None,
    ) -> None:
        self.tools = tools
        self.skills = skills
        self.agents = agents
        self.paths = paths
        # Готовые дополнения: id → (описание, фабрика); включённые помнит extras.json.
        self.extras = extras or {}
        self._extras_store = ExtrasStore(paths.tools / "extras.json")

    def load(self) -> None:
        """Инструменты, созданные агентом, подхватываются с диска при старте."""
        for tool in scripts.load_script_tools(self.paths.tools):
            self.tools.replace(tool)
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
        tool = scripts.write_script_tool(self.paths.tools, tool_id, **fields)
        self.tools.replace(tool)
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
            tool = scripts.restore_script_tool(self.paths.tools, archive_id)
            self.tools.replace(tool)
            return tool.id
        if kind == "skill":
            skill = loader.restore_skill(self.paths.skills, archive_id)
            self.skills.replace(skill)
            return skill.id
        agent = loader.restore_agent(self.paths.agents, archive_id)
        self.agents.replace(agent)
        return agent.id

    # ---------- обзор для UI ----------

    def overview(self) -> dict:
        tools = [
            {
                "id": tool.id, "name": tool.name, "description": summary_line(tool.description, 200),
                "origin": "agent" if isinstance(tool, scripts.ScriptTool)
                else "mcp" if getattr(tool, "server", None) else "extra" if self.is_extra(tool.id) else "builtin",
                "requires_approval": tool.requires_approval,
                "path": str(tool.folder) if isinstance(tool, scripts.ScriptTool) else None,
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
        return {"tools": tools, "skills": skills, "agents": agents, "archive": archive}

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
