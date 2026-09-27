"""Реестр выбирает реализацию LLM по имени провайдера из сессии."""

from local_agent.llm.base import LLMProvider


class LLMRegistry:
    def __init__(self, providers: dict[str, LLMProvider]) -> None:
        self._providers = providers

    def get(self, provider_id: str) -> LLMProvider | None:
        return self._providers.get(provider_id)
