"""Провайдер по сценарию: отвечает заготовленными шагами и запоминает, что видела модель."""

import json

from local_agent.llm.models import ChatResult, ToolCall


def call(tool: str, /, **arguments) -> tuple[str, dict]:
    return (tool, arguments)


class ScriptedProvider:
    """steps — по шагу на запрос: строка — ответ текстом, (имя, аргументы) — вызов инструмента."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.requests: list[dict] = []

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        self.requests.append({"model": model, "messages": list(messages), "tools": tools})
        step = self.steps.pop(0) if self.steps else "Готово"
        if isinstance(step, str):
            return ChatResult(content=step, input_tokens=100, output_tokens=5)
        name, arguments = step
        index = len(self.requests)
        return ChatResult(
            content=None,
            tool_calls=(ToolCall(f"call-{index}", name, json.dumps(arguments, ensure_ascii=False)),),
        )

    async def list_models(self):
        return ["test"]

    async def close(self):
        pass

    def tool_names(self, request: int = -1) -> list[str]:
        return [item["function"]["name"] for item in self.requests[request]["tools"] or []]

    def system_prompt(self, request: int = -1) -> str:
        return self.requests[request]["messages"][0].content


def events_of(response) -> list[dict]:
    """События SSE-ответа TestClient."""
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
