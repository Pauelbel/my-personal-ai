"""План задачи в сессии: как он выглядит в промпте, какие пункты ещё не сделаны и что считать итогом этапа.

Маленькая модель на длинной задаче забывает её части: исходное «что сдать» тонет в результатах инструментов.
План хранится в сессии и подставляется в системный промпт перед каждым запросом, а ответ без вызова
инструмента при несделанных пунктах не заканчивает ход (core/graph.py). Записывает план инструмент tools/plan.py.
Когда этап отмечают сделанным, здесь же выбирается его итог — текст, который чат покажет результатом этапа;
выбор сохраняется с результатом вызова (meta), и интерфейс ничего не угадывает.
"""

import re

from local_agent.memory.models import Message


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


# Итог этапа должен быть содержательным: короткое «Готово, перехожу дальше» — не результат.
RESULT_MIN_CHARS = 200
STRUCTURE = re.compile(r"^\s*(?:#{1,4}\s|[-*]\s|\d+[.)]\s|\|)|```", re.M)


def substantial(text: str) -> bool:
    text = text.strip()
    return len(text) >= RESULT_MIN_CHARS or len(STRUCTURE.findall(text)) >= 3


def stage_messages(history: list[Message]) -> list[Message]:
    """Реплики модели за этап, который сейчас отмечают сделанным: от прошлого вызова plan до текущего ответа
    (последней реплики модели). Реплика пользователя, не ответ на карточку, — граница хода: дальше не смотрим."""
    current = next((index for index in range(len(history) - 1, -1, -1) if history[index].role == "assistant"), None)
    if current is None:
        return []
    picked = [history[current]]
    for index in range(current - 1, -1, -1):
        message = history[index]
        if message.role == "assistant" and any(call.name == "plan" for call in message.tool_calls):
            break
        reply = message.role == "user" and index > 0 and history[index - 1].role == "tool"
        if message.role == "user" and not reply:
            break
        if message.role == "assistant":
            picked.append(message)
    return picked[::-1]


def stage_result(messages: list[Message]) -> Message | None:
    """Итог этапа: текст того ответа, где этап отмечен сделанным, если он содержательный; иначе — самый объёмный
    содержательный текст за этап (модель могла написать чек-лист раньше, а отметить этап коротким ответом)."""
    if not messages:
        return None
    current = messages[-1]
    if current.content.strip() and substantial(current.content):
        return current
    candidates = [message for message in messages[:-1] if message.content.strip() and substantial(message.content)]
    return max(candidates, key=lambda message: len(message.content.strip()), default=None)
