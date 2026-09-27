"""Реестр выбирает реализацию LLM по имени провайдера из сессии."""

from local_agent.llm.base import LLMProvider


class LLMRegistry:
    def __init__(self, providers: dict[str, LLMProvider]) -> None:
        self._providers = providers

    def get(self, provider_id: str) -> LLMProvider | None:
        return self._providers.get(provider_id)

    def items(self) -> list[tuple[str, str]]:
        """Пары (id, отображаемое имя) для выбора провайдера в UI."""
        return [
            (provider_id, getattr(provider, "name", provider_id))
            for provider_id, provider in self._providers.items()
        ]

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
