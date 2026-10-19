"""Реестр выбирает реализацию LLM по имени провайдера из сессии."""

from llm.base import LLMProvider


class LLMRegistry:
    def __init__(self, providers: dict[str, LLMProvider]) -> None:
        self._providers = providers

    def get(self, provider_id: str) -> LLMProvider | None:
        return self._providers.get(provider_id)

    def set(self, provider_id: str, provider: LLMProvider) -> LLMProvider | None:
        """Добавить или заменить провайдера; прежний возвращается — его нужно закрыть."""
        previous = self._providers.get(provider_id)
        self._providers[provider_id] = provider
        return previous

    def remove(self, provider_id: str) -> LLMProvider | None:
        return self._providers.pop(provider_id, None)

    def items(self) -> list[tuple[str, str]]:
        """Пары (id, отображаемое имя) для выбора провайдера в UI."""
        return [
            (provider_id, getattr(provider, "name", provider_id))
            for provider_id, provider in self._providers.items()
        ]

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
