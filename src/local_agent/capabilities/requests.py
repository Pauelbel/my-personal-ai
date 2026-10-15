"""Обращения саб-агента к пользователю: вопрос, без ответа на который дальше нельзя, и просьба подключить инструменты.

Оба — карточки в чате. На вопрос пользователь отвечает текстом, и ответ возвращается агенту результатом
инструмента. Просьбу об инструментах он утверждает — инструменты записываются в файл агента и появляются
у него сразу, в этом же ходе: набор инструментов собирается заново на каждом шаге.
"""

from dataclasses import replace
from pathlib import Path

from local_agent.agent.models import Agent
from local_agent.tools.base import ToolResult
from local_agent.tools.registry import ToolRegistry


class AskUserTool:
    id = "ask_user"
    name = "Спросить пользователя"
    requires_approval = True
    # Карточка — вопрос: ответ пользователя приходит инструменту текстом, а не как отказ от вызова.
    takes_reply = True
    description = (
        "Спросить пользователя, когда без него дальше нельзя: не хватает данных, доступа, решения или что-то "
        "не получается. Пользователь увидит вопрос в чате и ответит — дождись ответа и продолжай. "
        "Не спрашивай то, что можешь узнать сам инструментами"
    )
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "Вопрос целиком: что нужно и зачем"}},
        "required": ["question"],
        "additionalProperties": False,
    }

    def __init__(self, agent_name: str) -> None:
        self._agent_name = agent_name

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        return {"kind": "question", "title": f"{self._agent_name} спрашивает", "question": arguments.get("question", "")}

    def reply(self, decision: dict, arguments: dict) -> ToolResult:
        text = decision["text"] if decision["action"] == "reply" else ""
        if text:
            return ToolResult(f"Ответ пользователя: {text}")
        return ToolResult("Пользователь не стал отвечать: решай сам и напиши в итоге, что ты предположил.")

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        return ToolResult("Вопрос задаётся только через карточку пользователю", is_error=True)


class RequestToolsTool:
    id = "request_tools"
    name = "Попросить инструменты"
    requires_approval = True

    def __init__(self, capabilities, tools: ToolRegistry, agent: Agent) -> None:
        self._capabilities = capabilities
        self._agent = agent
        self._available = sorted(tool.id for tool in tools.all() if tool.id not in agent.tools)

    @property
    def description(self) -> str:
        return (
            "Попросить пользователя подключить тебе инструменты, которые есть в системе, но не у тебя, — когда без них "
            "твою задачу не сделать. После одобрения они появятся у тебя сразу. Есть: " + ", ".join(self._available)
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "tools": {"type": "array", "items": {"type": "string", "enum": self._available}},
                "reason": {"type": "string", "description": "Зачем они нужны в этой задаче"},
            },
            "required": ["tools", "reason"],
            "additionalProperties": False,
        }

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        tools = _requested(arguments)
        return {
            "kind": "tools_request", "title": f"{self._agent.name} просит инструменты: {', '.join(tools)}",
            "tools": tools, "reason": arguments.get("reason", ""),
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        tools = [tool for tool in _requested(arguments) if tool in self._available]
        if not tools:
            return ToolResult("Таких инструментов нет или они уже у тебя", is_error=True)
        current = self._capabilities.agents.get(self._agent.id) or self._agent
        try:
            self._capabilities.save_agent(replace(current, tools=tuple(dict.fromkeys([*current.tools, *tools]))))
        except (ValueError, OSError) as exc:
            return ToolResult(f"Не удалось подключить: {exc}", is_error=True)
        return ToolResult(f"Подключено: {', '.join(tools)}. Пользуйся ими сразу.")


def _requested(arguments: dict[str, object]) -> list[str]:
    tools = arguments.get("tools")
    return [item for item in tools if isinstance(item, str)] if isinstance(tools, list) else []
