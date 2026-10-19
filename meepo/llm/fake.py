"""Фейковая модель для тестов: отдаёт заранее заданные ответы по очереди, без сервера.

Ответ в сценарии:
- строка — текстовый ответ;
- словарь {"content": "...", "calls": [("write_file", {"path": "a.md", "content": "x"}), ...]} — вызовы инструментов;
- исключение — будет брошено вместо ответа (LLMEmpty, LLMRepetition, …).
Каждый запрос запоминается в `requests`: сообщения и были ли предложены инструменты.
"""

import json
from collections.abc import AsyncIterator

from meepo.llm.client import Reply


class FakeLLM:
    def __init__(self, script: list, window: int | None = 8192) -> None:
        self.script = list(script)
        self.window = window
        self.requests: list[dict] = []
        self.base_url = "http://fake/v1"
        self.api_key = ""

    async def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator:
        self.requests.append({"model": model, "messages": [dict(item) for item in messages], "tools": tools})
        if not self.script:
            raise AssertionError("Фейковой модели не хватило ответов сценария")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            item = {"content": item}
        content = item.get("content", "")
        if content:
            yield "delta", content
        calls = [
            {"id": f"call-{len(self.requests)}-{index}", "name": name,
             "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}
            for index, (name, arguments) in enumerate(item.get("calls", []))
        ]
        yield Reply(content, calls, "", input_tokens=100, output_tokens=10)

    async def list_models(self) -> list[str]:
        return ["fake"]

    async def context_length(self, model: str) -> int | None:
        return self.window

    async def close(self) -> None:
        pass
