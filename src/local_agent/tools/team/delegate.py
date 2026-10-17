"""Инструменты поручений: основной агент поручает задачу саб-агенту (delegate), саб-агент — другому агенту (ask_agent).

Очередь, фоновые ходы и доставка результата — core/delegation.py; здесь только то, что видит модель.
"""

from pathlib import Path

from local_agent.agents.registry import AgentRegistry
from local_agent.core.delegation import DelegationManager
from local_agent.core.tool import ToolResult
from local_agent.sessions.models import Session


class DelegateTool:
    id = "delegate"
    name = "Поручить саб-агенту"
    requires_approval = False

    def __init__(self, manager: DelegationManager, agents: AgentRegistry, parent: Session) -> None:
        self._manager = manager
        self._agents = agents
        self._parent = parent

    @property
    def description(self) -> str:
        return (
            "Поручить задачу саб-агенту. Он работает в фоне, ты продолжаешь разговор; результат придёт "
            "отдельным сообщением. Передай задачу целиком: что сделать, пути, условия, формат результата"
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": [agent.id for agent in self._agents.all()]},
                "task": {"type": "string", "description": "Задача целиком"},
            },
            "required": ["agent", "task"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        agent, task = arguments.get("agent"), arguments.get("task")
        if not isinstance(agent, str) or not isinstance(task, str) or not task.strip():
            return ToolResult("Нужны agent и task", is_error=True)
        try:
            child = self._manager.delegate(self._parent, agent, task.strip())
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(
            f"Задача поручена {agent} (задача {child.id[:8]}). Скажи пользователю, что работа идёт в фоне "
            "и результат придёт сообщением; не жди его и не выдумывай результат."
        )


class AskAgentTool:
    """Саб-агент обращается к другому агенту команды — с вопросом или поручением — и ждёт ответа."""

    id = "ask_agent"
    name = "Обратиться к агенту"
    requires_approval = False

    def __init__(self, manager: DelegationManager, agents: AgentRegistry, asker: Session, targets: tuple[str, ...]) -> None:
        self._manager = manager
        self._agents = agents
        self._asker = asker
        self._targets = targets

    @property
    def description(self) -> str:
        known = [agent for agent in (self._agents.get(item) for item in self._targets) if agent]
        return (
            "Обратиться к другому агенту команды: поручить ему часть задачи или задать вопрос — и дождаться ответа. "
            "Передай всё целиком: что нужно, пути, контекст, в каком виде вернуть. "
            "К кому можно обратиться: " + "; ".join(f"{agent.id} — {agent.description or agent.name}" for agent in known)
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": list(self._targets)},
                "question": {"type": "string", "description": "Поручение или вопрос целиком"},
            },
            "required": ["agent", "question"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        agent, question = arguments.get("agent"), arguments.get("question")
        if agent not in self._targets or not isinstance(question, str) or not question.strip():
            return ToolResult(f"Нужны agent (один из: {', '.join(self._targets)}) и question", is_error=True)
        try:
            text = await self._manager.ask(self._asker, agent, question.strip())
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Ответ {agent}:\n\n{text}")
