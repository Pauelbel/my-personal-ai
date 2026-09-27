"""Контракт LLM позволяет менять провайдера без изменения выполнения агента."""

from collections.abc import AsyncIterator
from typing import Protocol

from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta


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


async def stream_chat(
    provider: LLMProvider,
    model: str,
    messages: list[ChatMessage],
    tools: list[dict[str, object]] | None = None,
) -> AsyncIterator[str | ReasoningDelta | ChatResult]:
    """Отдаёт рассуждения и текст по частям, в конце ChatResult; провайдер без стриминга отвечает одним куском."""
    streaming = getattr(provider, "chat_stream", None)
    if streaming is not None:
        async for item in streaming(model, messages, tools):
            yield item
        return
    result = await provider.chat(model, messages, tools) if tools else await provider.chat(model, messages)
    if result.content:
        yield result.content
    yield result


async def context_length(provider: LLMProvider, model: str) -> int | None:
    """Размер окна загруженной модели, если провайдер умеет его сообщить."""
    lookup = getattr(provider, "context_length", None)
    return await lookup(model) if lookup is not None else None
