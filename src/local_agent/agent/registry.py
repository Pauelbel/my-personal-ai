"""Реестры хранят агентов и навыки, загруженные из их папок."""

from local_agent.agent.models import Agent, Skill


class AgentRegistry:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def replace(self, agent: Agent) -> None:
        """Агент перечитан из файла: следующий ход возьмёт уже новую версию."""
        self._agents[agent.id] = agent

    def remove(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)


class SkillRegistry:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {skill.id: skill for skill in skills}

    def get(self, skill_id: object) -> Skill | None:
        return self._skills.get(skill_id) if isinstance(skill_id, str) else None

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def replace(self, skill: Skill) -> None:
        self._skills[skill.id] = skill

    def remove(self, skill_id: str) -> None:
        self._skills.pop(skill_id, None)
