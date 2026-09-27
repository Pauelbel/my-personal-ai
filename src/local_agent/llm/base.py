"""Контракт LLM позволяет менять провайдера без изменения выполнения агента."""

from typing import Protocol

from local_agent.llm.models import ChatMessage, ChatResult


class LLMProviderError(Exception):
    """Провайдер не смог вернуть корректный ответ модели."""


class LLMProviderUnavailable(LLMProviderError):
    """Сервер провайдера недоступен или превысил время ожидания."""


class LLMProvider(Protocol):
    async def chat(
        self, model: str, messages: list[ChatMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult: ...

    async def list_models(self) -> list[str]: ...

    async def close(self) -> None: ...
