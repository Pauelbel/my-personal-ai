"""Реестр агентов, загруженных из data/agents."""

from local_agent.agents.models import Agent


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
