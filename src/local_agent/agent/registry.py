"""Реестр хранит агентов, загруженных из папки агентов."""

from local_agent.agent.models import Agent


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
