"""Инструмент plan: агент записывает пункты задачи и отмечает сделанные (как это использует ядро — core/plan.py)."""

from pathlib import Path

from local_agent.core.plan import open_steps, plan_text, stage_messages, stage_result
from local_agent.core.tool import CURRENT_TURN, ToolResult
from local_agent.memory.conversation import ConversationService
from local_agent.sessions.service import SessionService

MAX_STEPS = 12
MAX_STEP_CHARS = 200
# Сколько последних сообщений сессии просматривать в поисках итога этапа.
HISTORY_LOOKUP = 400


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

    def __init__(self, sessions: SessionService, session_id: str, conversation: ConversationService | None = None) -> None:
        self._sessions = sessions
        self._session_id = session_id
        # По истории выбирается итог этапа, который чат покажет результатом (см. core/plan.py).
        self._conversation = conversation

    def validate(self, arguments: dict[str, object]) -> str | None:
        """Маленькая модель отмечает пункт его текстом («Тест-кейсы») или строкой «2» — переводим в номер пункта.
        Не узнали — номер остаётся как есть, и execute объяснит, что нужно."""
        step = arguments.get("step")
        if arguments.get("action") != "done" or not isinstance(step, str):
            return None
        text = " ".join(step.split()).casefold().rstrip(".")
        if text.isdigit():
            arguments["step"] = int(text)
            return None
        session = self._sessions.get(self._session_id)
        items = [item["text"].casefold() for item in session.plan] if session else []
        found = [number for number, item in enumerate(items, start=1) if item == text] or [
            number for number, item in enumerate(items, start=1) if text and (text in item or item in text)
        ]
        if len(found) == 1:
            arguments["step"] = found[0]
        return None

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
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
            return ToolResult(f"{plan_text(plan)}\n{tail}", meta=self._stage_meta(step, plan[step - 1]["text"]))
        return ToolResult("action — set или done", is_error=True)

    def _stage_meta(self, step: int, title: str) -> dict:
        """Какой текст чат покажет итогом этапа: решается здесь один раз и сохраняется с результатом вызова."""
        meta = {"stage": step, "title": title, "result": None}
        if self._conversation is not None:
            result = stage_result(stage_messages(self._conversation.recent(self._session_id, HISTORY_LOOKUP)))
            meta["result"] = result.id if result else None
        return meta
