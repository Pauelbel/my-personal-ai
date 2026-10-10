"""Инструмент переписки узлов команды: сообщение можно отправить только разрешённому получателю."""

from collections.abc import Mapping
from pathlib import Path

from local_agent.tools.base import ToolResult


class SendMessageTool:
    id = "send_message"
    name = "Сообщение команде"
    requires_approval = False

    def __init__(self, recipients: Mapping[str, str]) -> None:
        # id получателя → чем он занимается; стрелки графа проверяет именно этот список.
        self._recipients = dict(recipients)
        # Отправленное за ход забирает координатор, когда ход закончится.
        self.sent: list[tuple[str, str]] = []

    @property
    def recipients(self) -> dict[str, str]:
        return dict(self._recipients)

    @property
    def description(self) -> str:
        return (
            "Написать другому участнику команды. Разрешённые получатели:\n"
            + "\n".join(f"- {node_id}: {about}" for node_id, about in self._recipients.items())
            + "\nСообщение уйдёт, когда ты закончишь ход, а ответ придёт следующим сообщением. "
            "Если ты не вызываешь send_message, твой финальный ответ сам вернётся тому, кто дал тебе задачу."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "enum": list(self._recipients), "description": "id получателя"},
                "content": {"type": "string", "description": "Текст сообщения: задача, вопрос или результат"},
            },
            "required": ["to", "content"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        to, content = arguments.get("to"), arguments.get("content")
        if not isinstance(to, str) or to not in self._recipients:
            allowed = ", ".join(self._recipients)
            return ToolResult(f"Этому получателю писать нельзя. Разрешены: {allowed}", is_error=True)
        if not isinstance(content, str) or not content.strip():
            return ToolResult("Сообщение не может быть пустым", is_error=True)
        self.sent.append((to, content.strip()))
        return ToolResult(f"Сообщение для {to} поставлено в очередь")
