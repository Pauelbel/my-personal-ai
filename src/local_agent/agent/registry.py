"""Реестр хранит доступных агентов, пока с одним агентом по умолчанию."""

from local_agent.agent.models import Agent


class AgentRegistry:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)
