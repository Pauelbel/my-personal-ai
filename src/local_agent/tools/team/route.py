"""Кто делает задачу: выбор пользователя и передача чужой задачи основному агенту.

Основной агент, у которого есть подходящий саб-агент, не решает молча, а спрашивает карточкой «Кто сделает»:
поручить агенту или сделать самому. Выбор запоминается в проекте маршрутом «тип задачи → исполнитель»,
и задачи того же типа дальше идут этим путём без вопроса. Саб-агент, которому пользователь написал задачу
не по профилю, не берётся за неё, а передаёт основному агенту — распределяет работу он.
"""

from pathlib import Path

from local_agent.agents.main import MAIN_AGENT_ID
from local_agent.agents.registry import AgentRegistry
from local_agent.core.tool import ToolResult
from local_agent.projects.service import ProjectService, route_kind
from local_agent.sessions.models import Session


class ChooseRouteTool:
    id = "choose_route"
    name = "Кто сделает"
    requires_approval = True
    # Карточка-вопрос: выбор пользователя приходит инструменту результатом.
    takes_reply = True
    description = (
        "Спросить пользователя, кто сделает задачу: ты сам или саб-агент. Вызывай, когда для задачи есть подходящий "
        "саб-агент, а маршрута для такого типа задач в проекте нет и пользователь сам не сказал, кто делает. "
        "Выбор запомнится для этого типа задач"
    )

    def __init__(self, projects: ProjectService, agents: AgentRegistry, session: Session) -> None:
        self._projects = projects
        self._agents = agents
        self._session = session

    def _known(self) -> list[str]:
        project = self._projects.get(self._session.project_id) if self._session.project_id else None
        return sorted(project.routes) if project is not None else []

    @property
    def parameters(self) -> dict[str, object]:
        known = self._known()
        # Одна и та же работа под разными названиями — лишний вопрос пользователю: сначала сверься с запомненными.
        hint = (
            f" Если задача того же типа, что один из уже запомненных ({', '.join(known)}), назови его дословно"
            if known else ""
        )
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Тип задачи в 1–3 словах, например «автотесты», «тест-кейсы»." + hint},
                "agent": {
                    "type": "string", "enum": [agent.id for agent in self._agents.all()],
                    "description": "Какого саб-агента предложить",
                },
                "task": {"type": "string", "description": "Задача одной строкой"},
            },
            "required": ["kind", "agent", "task"],
            "additionalProperties": False,
        }

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        suggested = self._agents.get(str(arguments.get("agent")))
        return {
            "kind": "route", "title": f"Кто сделает: {arguments.get('task', '')}",
            "task_kind": route_kind(str(arguments.get("kind") or "")),
            "suggested": suggested.id if suggested else None,
            "agents": [
                {"id": agent.id, "name": agent.name, "description": agent.description} for agent in self._agents.all()
            ],
        }

    def reply(self, decision: dict, arguments: dict) -> ToolResult:
        choice, text = decision.get("choice"), decision["text"] if decision["action"] == "reply" else ""
        kind = route_kind(str(arguments.get("kind") or ""))
        agent = self._agents.get(choice) if isinstance(choice, str) else None
        if choice == MAIN_AGENT_ID:
            self._remember(kind, MAIN_AGENT_ID)
            return ToolResult(f"Пользователь решил: делаешь сам. {self._remembered(kind, 'ты сам')}Делай задачу сейчас.")
        if agent is not None:
            self._remember(kind, agent.id)
            return ToolResult(
                f"Пользователь решил: поручить {agent.id} ({agent.name}). {self._remembered(kind, agent.name)}"
                "Сейчас вызови delegate с этим агентом и задачей целиком, сам её не делай."
            )
        if text:
            return ToolResult(f"Ответ пользователя: {text}. Сделай так, как он сказал.")
        return ToolResult("Пользователь не выбрал: реши сам по профилю агентов и скажи, кто делает.")

    def _remember(self, kind: str, agent_id: str) -> None:
        if kind and self._session.project_id:
            self._projects.set_route(self._session.project_id, kind, agent_id)

    @staticmethod
    def _remembered(kind: str, who: str) -> str:
        return f"Запомнено: «{kind}» → {who}, такие задачи дальше без вопроса. " if kind else ""

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        return ToolResult("Выбор делается только через карточку пользователю", is_error=True)


class HandoffTool:
    id = "handoff"
    name = "Передать основному агенту"
    requires_approval = False
    description = (
        "Передать основному агенту задачу, которую пользователь написал тебе, но она не по твоему профилю: "
        "он распределит её сам. После передачи коротко скажи пользователю, что задачу ведёт основной агент, "
        "и закончи ход"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Задача пользователя дословно, со всеми деталями"},
            "reason": {"type": "string", "description": "Почему она не по твоему профилю"},
        },
        "required": ["task", "reason"],
        "additionalProperties": False,
    }

    def __init__(self, manager, session: Session) -> None:
        self._manager = manager
        self._session = session

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        task, reason = arguments.get("task"), arguments.get("reason")
        if not isinstance(task, str) or not task.strip():
            return ToolResult("Нужна task — задача пользователя целиком", is_error=True)
        try:
            self._manager.handoff(self._session, task.strip(), reason if isinstance(reason, str) else "")
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(
            "Передано основному агенту, он распределит задачу. Скажи пользователю одной фразой, что задачу ведёт "
            "основной агент, и закончи ход — сам её не делай."
        )
