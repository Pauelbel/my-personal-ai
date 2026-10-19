"""plan: агент записывает пункты многочастной задачи и отмечает сделанные.

Маленькая модель на длинной задаче забывает её части: исходное «что сдать» тонет в результатах инструментов.
План хранится в сессии и подставляется в системный промпт перед каждым запросом (agent/prompt.py);
guard agent/guards/plan.py не даёт закончить ход, пока в плане этого хода есть несделанные пункты.
"""

from meepo.agent.tools.base import Tool, ToolContext, ToolResult

MAX_STEPS = 12
MAX_STEP_CHARS = 200


def plan_text(plan: list[dict]) -> str:
    lines = []
    for index, step in enumerate(plan, 1):
        note = f" — {step['note']}" if step.get("note") else ""
        lines.append(f"{index}. {'[x]' if step.get('done') else '[ ]'} {step['text']}{note}")
    return "\n".join(lines)


def open_steps(plan: list[dict]) -> list[tuple[int, str]]:
    return [(index, step["text"]) for index, step in enumerate(plan, 1) if not step.get("done")]


def _step_number(step: object, plan: list[dict]) -> object:
    """Маленькая модель отмечает пункт его текстом («Тест-кейсы») или строкой «2» — переводим в номер пункта."""
    if not isinstance(step, str):
        return step
    text = " ".join(step.split()).casefold().rstrip(".")
    if text.isdigit():
        return int(text)
    items = [item["text"].casefold() for item in plan]
    found = [number for number, item in enumerate(items, 1) if item == text] or [
        number for number, item in enumerate(items, 1) if text and (text in item or item in text)
    ]
    return found[0] if len(found) == 1 else step


def plan(args: dict, ctx: ToolContext) -> ToolResult:
    session = ctx.sessions.get(ctx.session_id)
    if args["action"] == "set":
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
            raise ValueError("Для set нужен список пунктов steps")
        if len(steps) > MAX_STEPS:
            raise ValueError(f"Не больше {MAX_STEPS} пунктов: объедини мелкие")
        items = [{"text": " ".join(step.split())[:MAX_STEP_CHARS], "done": False} for step in steps]
        ctx.sessions.update(ctx.session_id, plan=items, plan_request=ctx.request_id)
        return ToolResult(f"План записан:\n{plan_text(items)}\nНачни с пункта 1, после него отметь plan done.")
    items = [dict(step) for step in session["plan"]]
    if not items:
        raise ValueError("Плана нет: сначала запиши его через action set")
    step = _step_number(args.get("step"), items)
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= len(items):
        raise ValueError(f"step — номер пункта от 1 до {len(items)}")
    items[step - 1]["done"] = True
    if isinstance(args.get("note"), str) and args["note"].strip():
        items[step - 1]["note"] = " ".join(args["note"].split())[:MAX_STEP_CHARS]
    ctx.sessions.update(ctx.session_id, plan=items, plan_request=ctx.request_id)
    remaining = open_steps(items)
    tail = (f"Следующий — пункт {remaining[0][0]}: {remaining[0][1]}. Делай его сразу вызовом инструмента."
            if remaining else "Все пункты сделаны: напиши пользователю итог — что сделано и где лежит.")
    return ToolResult(f"{plan_text(items)}\n{tail}")


TOOLS = [
    Tool("plan", "План задачи из нескольких частей: action set — записать пункты по порядку (в начале работы); "
         "action done — отметить пункт сделанным, в note — коротко результат (где лежит файл) или почему пропущен. "
         "Пока есть неотмеченные пункты, ход не закончится.",
         {"type": "object", "properties": {
             "action": {"type": "string", "enum": ["set", "done"]},
             "steps": {"type": "array", "items": {"type": "string"},
                       "description": "Для set: пункты по порядку, коротко, например «Тест-кейсы», «Автотесты»"},
             # Без типа: модель присылает и 2, и «2», и текст пункта — _step_number разберётся.
             "step": {"description": "Для done: номер пункта (число)"},
             "note": {"type": "string", "description": "Для done: результат пункта или почему пропущен"},
         }, "required": ["action"], "additionalProperties": False},
         plan),
]
