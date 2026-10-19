"""Реестр пользовательских специализированных агентов."""

from agents.models import Agent


class AgentRegistry:
    def __init__(self, agents: list[Agent] = ()) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: object) -> Agent | None:
        return self._agents.get(agent_id) if isinstance(agent_id, str) else None

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def replace(self, agent: Agent) -> None:
        self._agents[agent.id] = agent

    def remove(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
