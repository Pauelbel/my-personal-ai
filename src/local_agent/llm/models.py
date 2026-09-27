"""Небольшие модели данных описывают запрос и результат LLM-вызова."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ReasoningDelta:
    """Кусок рассуждений, которые thinking-модель пишет до ответа."""

    text: str


@dataclass(frozen=True)
class ChatResult:
    content: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: tuple[ToolCall, ...] = ()
