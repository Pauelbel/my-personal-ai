"""План задачи: агент записывает пункты и отмечает сделанные, а движок не даёт закончить ход с несделанными.

Маленькая модель на длинной задаче забывает её части: исходное «что сдать» тонет в результатах инструментов.
План хранится в сессии и подставляется в системный промпт перед каждым запросом, а ответ без вызова
инструмента при несделанных пунктах не заканчивает ход: модель просят продолжить (см. agent_graph).
"""

from pathlib import Path

from local_agent.sessions.service import SessionService
from local_agent.tools.base import ToolResult

MAX_STEPS = 12
MAX_STEP_CHARS = 200


def plan_text(plan: list[dict]) -> str:
    lines = []
    for index, step in enumerate(plan, 1):
        mark = "[x]" if step.get("done") else "[ ]"
        note = f" — {step['note']}" if step.get("note") else ""
        lines.append(f"{index}. {mark} {step['text']}{note}")
    return "\n".join(lines)


def open_steps(plan: list[dict]) -> list[tuple[int, str]]:
    return [(index, step["text"]) for index, step in enumerate(plan, 1) if not step.get("done")]


def plan_prompt(plan: list[dict]) -> str:
    """Блок системного промпта: план перед глазами модели на каждом раунде."""
    if not open_steps(plan):
        return ""
    return (
        "План текущей задачи (после каждого пункта отмечай его через plan с action done; итог пиши, когда отмечены все). "
        "Если пользователь дал новую задачу, а этот план уже не нужен, составь новый через plan с action set:\n"
        + plan_text(plan)
    )


class PlanTool:
    id = "plan"
    name = "План задачи"
    requires_approval = False
    description = (
        "План задачи из нескольких частей: action set — записать пункты по порядку (в начале работы); "
        "action done — отметить пункт сделанным, в note — коротко результат (где лежит файл) или почему пропущен. "
        "Пока есть неотмеченные пункты, ход не закончится"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "done"]},
            "steps": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_STEPS,
                      "description": "Для set: пункты по порядку, коротко, например «Тест-кейсы», «Баг-репорты», «Автотесты»"},
            "step": {"type": "integer", "minimum": 1, "description": "Для done: номер пункта"},
            "note": {"type": "string", "description": "Для done: результат пункта или почему пропущен"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, sessions: SessionService, session_id: str) -> None:
        self._sessions = sessions
        self._session_id = session_id

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        # Импорт здесь: граф импортирует этот модуль, а номер хода берётся из графа.
        from local_agent.graph.agent_graph import CURRENT_TURN

        session = self._sessions.get(self._session_id)
        if session is None:
            return ToolResult("Сессия не найдена", is_error=True)
        request_id = (CURRENT_TURN.get() or {}).get("request_id")
        action = arguments.get("action")
        if action == "set":
            steps = arguments.get("steps")
            if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
                return ToolResult("Для set нужен список пунктов steps", is_error=True)
            if len(steps) > MAX_STEPS:
                return ToolResult(f"Не больше {MAX_STEPS} пунктов: объедини мелкие", is_error=True)
            plan = [{"text": " ".join(step.split())[:MAX_STEP_CHARS], "done": False} for step in steps]
            self._sessions.set_plan(self._session_id, plan, request_id)
            return ToolResult(f"План записан:\n{plan_text(plan)}\nНачни с пункта 1, после него отметь plan done.")
        if action == "done":
            plan = [dict(step) for step in session.plan]
            step = arguments.get("step")
            if not plan:
                return ToolResult("Плана нет: сначала запиши его через action set", is_error=True)
            if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= len(plan):
                return ToolResult(f"step — номер пункта от 1 до {len(plan)}", is_error=True)
            plan[step - 1]["done"] = True
            note = arguments.get("note")
            if isinstance(note, str) and note.strip():
                plan[step - 1]["note"] = " ".join(note.split())[:MAX_STEP_CHARS]
            self._sessions.set_plan(self._session_id, plan, request_id)
            remaining = open_steps(plan)
            tail = (f"Следующий — пункт {remaining[0][0]}: {remaining[0][1]}. Делай его сразу вызовом инструмента."
                    if remaining else "Все пункты сделаны: напиши пользователю итог — что сделано и где лежит.")
            return ToolResult(f"{plan_text(plan)}\n{tail}")
        return ToolResult("action — set или done", is_error=True)
