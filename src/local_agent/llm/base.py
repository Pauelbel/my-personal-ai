"""Контракт LLM позволяет менять провайдера без изменения выполнения агента."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Protocol

from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta

# Gemma 4 закрывает рассуждения маркером «<channel|>». Бывает, что сервер (Ollama) их не распознаёт и шлёт
# обычным текстом: тогда всё до маркера — рассуждения, а ответ — после него.
THINKING_END = "<channel|>"


class LLMProviderError(Exception):
    """Провайдер не смог вернуть корректный ответ модели."""


class LLMEmptyResponse(LLMProviderError):
    """Модель ответила пустотой: ни текста, ни вызовов (бывает у thinking-моделей, ушедших в рассуждения)."""


class LLMRepetition(LLMProviderError):
    """Модель зациклилась: по кругу пишет одно и то же — в рассуждениях или в ответе."""


class LLMProviderUnavailable(LLMProviderError):
    """Сервер провайдера недоступен или превысил время ожидания."""


class LLMProvider(Protocol):
    async def chat(
        self, model: str, messages: list[ChatMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult: ...

    async def list_models(self) -> list[str]: ...

    async def close(self) -> None: ...


class RepetitionGuard:
    """Замечает, что модель пишет по кругу: хвост текста уже встречался в недавнем выводе несколько раз.

    Маленькие thinking-модели иногда без конца повторяют один и тот же план в рассуждениях; поток при этом
    не молчит, и тайм-аут не срабатывает. Проверка — раз в CHECK_EVERY символов, по последним WINDOW символам.
    """

    WINDOW = 200
    REPEATS = 5
    CHECK_EVERY = 400
    KEEP = 16_000

    def __init__(self) -> None:
        self._text = ""
        self._unchecked = 0

    def looped(self, chunk: str) -> bool:
        self._text = (self._text + chunk)[-self.KEEP:]
        self._unchecked += len(chunk)
        if self._unchecked < self.CHECK_EVERY or len(self._text) < self.WINDOW * self.REPEATS:
            return False
        self._unchecked = 0
        tail = self._text[-self.WINDOW:]
        # Пробелы и переводы строк — шум: зацикленный текст повторяется с точностью до них.
        return len(set(tail.split())) > 3 and self._text.count(tail) >= self.REPEATS


async def stream_chat(
    provider: LLMProvider,
    model: str,
    messages: list[ChatMessage],
    tools: list[dict[str, object]] | None = None,
) -> AsyncIterator[str | ReasoningDelta | ChatResult]:
    """Отдаёт рассуждения и текст по частям, в конце ChatResult; провайдер без стриминга отвечает одним куском."""
    streaming = getattr(provider, "chat_stream", None)
    if streaming is not None:
        guard = RepetitionGuard()
        stream = streaming(model, messages, tools)
        try:
            async for item in stream:
                text = item.text if isinstance(item, ReasoningDelta) else item if isinstance(item, str) else ""
                if text and guard.looped(text):
                    raise LLMRepetition("Модель зациклилась: одно и то же повторяется по кругу")
                yield _without_thinking(item) if isinstance(item, ChatResult) else item
        finally:
            # Оборванный поток закрываем сразу: иначе сервер модели продолжит генерировать впустую.
            await stream.aclose()
        return
    result = await provider.chat(model, messages, tools) if tools else await provider.chat(model, messages)
    if not (result.content or "").strip() and not result.tool_calls:
        raise LLMEmptyResponse("Модель вернула пустой ответ")
    if result.content:
        yield result.content
    yield _without_thinking(result)


def _without_thinking(result: ChatResult) -> ChatResult:
    """Ответ без рассуждений, пришедших текстом; в потоке они уже показаны, в историю не попадают."""
    if not result.content or THINKING_END not in result.content:
        return result
    answer = result.content.rsplit(THINKING_END, 1)[-1].strip()
    if not answer and not result.tool_calls:
        raise LLMEmptyResponse("Модель написала только рассуждения, без ответа")
    return replace(result, content=answer or None)


async def context_length(provider: LLMProvider, model: str) -> int | None:
    """Размер окна загруженной модели, если провайдер умеет его сообщить."""
    lookup = getattr(provider, "context_length", None)
    return await lookup(model) if lookup is not None else None
