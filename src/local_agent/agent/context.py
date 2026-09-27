"""Сборщик контекста ограничивает историю, передаваемую модели за один вызов."""

from local_agent.llm.models import ChatMessage
from local_agent.memory.models import Message


def build_context(
    system_prompt: str, history: list[Message], max_messages: int
) -> list[ChatMessage]:
    recent = history[-max_messages:]
    return [ChatMessage(role="system", content=system_prompt)] + [
        ChatMessage(role=message.role, content=message.content)
        for message in recent
    ]
