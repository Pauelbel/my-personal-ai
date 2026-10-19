"""Контекст запроса к модели: системный промпт, затем история сессии в формате OpenAI API.

Ответ модели с вызовами и результаты этих вызовов идут вместе: у каждого вызова в контексте есть результат
(ход могли прервать посреди раунда — тогда результат «вызов был прерван»).
"""

import math

MESSAGE_OVERHEAD_TOKENS = 4
INTERRUPTED_CALL = "Вызов инструмента был прерван"


def estimate_tokens(text: str | None) -> int:
    """Грубая оценка: около 4 байт UTF-8 на токен, так же считает UI."""
    return math.ceil(len((text or "").encode("utf-8")) / 4)


def message_tokens(message: dict) -> int:
    calls = sum(estimate_tokens(call["name"] + call["arguments"]) for call in message.get("tool_calls") or ())
    return MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.get("content")) + calls


def history_messages(history: list[dict]) -> list[dict]:
    """Сохранённые сообщения → сообщения для модели. Служебные поля (meta, stats, reasoning) модели не нужны."""
    results = {item["tool_call_id"]: item for item in history if item["role"] == "tool"}
    messages: list[dict] = []
    for item in history:
        role = item["role"]
        if role == "tool":
            # Результаты встают сразу за своим вызовом, ниже.
            continue
        if role == "assistant" and item.get("tool_calls"):
            messages.append({"role": "assistant", "content": item.get("content") or None, "tool_calls": item["tool_calls"]})
            for call in item["tool_calls"]:
                result = results.get(call["id"])
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": result["content"] if result else INTERRUPTED_CALL})
            continue
        if role == "user" and messages and messages[-1]["role"] == "user":
            # После неудачного хода в истории остаётся реплика без ответа: склеиваем, чтобы роли чередовались.
            messages[-1] = {"role": "user", "content": f"{messages[-1]['content']}\n\n{item['content']}"}
            continue
        messages.append({"role": role, "content": item.get("content") or ""})
    return messages


def build(system: str, history: list[dict]) -> list[dict]:
    return [{"role": "system", "content": system}, *history_messages(history)]


def total_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    return sum(message_tokens(item) for item in messages) + (estimate_tokens(str(tools)) if tools else 0)
