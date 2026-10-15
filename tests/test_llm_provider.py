"""Проверка адаптера подтверждает формат запросов к OpenAI-compatible API."""

import asyncio
import json

import httpx

from local_agent.llm.base import LLMProviderError
from local_agent.llm.models import ChatMessage, ToolCall
from local_agent.llm.openai_compatible import OpenAICompatibleProvider


def test_openai_compatible_provider() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Привет!"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
            },
        )

    async def exercise() -> None:
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:1234/v1",
            timeout_seconds=5,
            transport=httpx.MockTransport(respond),
        )
        try:
            assert await provider.list_models() == ["test-model"]
            result = await provider.chat(
                "test-model", [ChatMessage(role="user", content="Привет")]
            )
            assert result.content == "Привет!"
            assert result.input_tokens == 8
            assert result.output_tokens == 2
        finally:
            await provider.close()

    asyncio.run(exercise())
    assert [request.url.path for request in requests] == [
        "/v1/models", "/v1/chat/completions"
    ]
    assert requests[1].method == "POST"
    assert b'"model":"test-model"' in requests[1].content


def test_provider_parses_and_sends_tool_calls() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
                }]}}],
            })
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done"}}]})

    async def exercise() -> None:
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:1234/v1", 5, transport=httpx.MockTransport(respond)
        )
        try:
            first = await provider.chat("test-model", [ChatMessage("user", "Read")], tools=[{
                "type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}
            }])
            assert first.content is None
            assert first.tool_calls == (ToolCall("call-1", "read_file", '{"path":"note.txt"}'),)
            second = await provider.chat("test-model", [
                ChatMessage("assistant", None, tool_calls=first.tool_calls),
                ChatMessage("tool", "text", tool_call_id="call-1"),
            ])
            assert second.content == "Done"
        finally:
            await provider.close()

    asyncio.run(exercise())
    first_payload = json.loads(requests[0].content)
    second_payload = json.loads(requests[1].content)
    assert first_payload["tools"][0]["function"]["name"] == "read_file"
    assert second_payload["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert second_payload["messages"][1]["tool_call_id"] == "call-1"


def test_http_error_carries_server_explanation_and_vllm_window() -> None:
    """vLLM отвечает 404 с пояснением, если модели с таким id нет, а окно сообщает в max_model_len."""
    missing = {"object": "error", "message": "The model `llm_studio//x` does not exist.", "code": 404}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/models":
            return httpx.Response(404, text="Not Found")
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "/models/qwen", "max_model_len": 131072}]})
        return httpx.Response(404, json=missing)

    async def exercise():
        provider = OpenAICompatibleProvider(
            "https://llm.example/v1", timeout_seconds=5, transport=httpx.MockTransport(respond), name="Сервер",
        )
        errors = []
        try:
            for call in (
                provider.chat("llm_studio//x", [ChatMessage(role="user", content="Привет")]),
                _drain(provider.chat_stream("llm_studio//x", [ChatMessage(role="user", content="Привет")])),
            ):
                try:
                    await call
                except LLMProviderError as exc:
                    errors.append(str(exc))
            return errors, await provider.context_length("/models/qwen"), await provider.context_length("other")
        finally:
            await provider.close()

    errors, window, unknown = asyncio.run(exercise())

    assert errors == ["Сервер модели «Сервер» вернул ошибку HTTP 404: The model `llm_studio//x` does not exist."] * 2
    assert window == 131072 and unknown is None


def test_ollama_window_comes_from_loaded_models() -> None:
    """Ollama не сообщает окно в /v1/models, только у загруженной модели в /api/ps."""
    loaded = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": loaded})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "gemma4:12b", "object": "model"}]})
        return httpx.Response(404, text="404 page not found")

    async def exercise():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:11434/v1", timeout_seconds=5, transport=httpx.MockTransport(respond),
        )
        try:
            before_load = await provider.context_length("gemma4:12b")
            # Модель загрузилась после первого запроса: окно видно сразу, без ожидания кэша.
            loaded.append({"name": "gemma4:12b", "model": "gemma4:12b", "context_length": 4096})
            return before_load, await provider.context_length("gemma4:12b"), await provider.context_length("other")
        finally:
            await provider.close()

    assert asyncio.run(exercise()) == (None, 4096, None)


async def _drain(stream):
    async for _ in stream:
        pass


def test_looping_stream_is_cut_and_closed() -> None:
    import pytest

    from local_agent.llm.base import LLMRepetition, stream_chat
    from local_agent.llm.models import ChatResult, ReasoningDelta

    plan = (
        "I'll click \"Add to cart\" for Sauce Labs Onesie (e102).\nI'll click on the Cart (e32).\n"
        "I'll type \"Ivan\" in the First Name field.\nI'll click \"Finish\".\nI'll report the result.\n"
    )
    closed = []

    class Looping:
        async def chat_stream(self, model, messages, tools=None):
            try:
                for _ in range(500):
                    yield ReasoningDelta(plan)
                yield ChatResult(content="", tool_calls=())
            finally:
                closed.append(True)

    async def consume(provider):
        return [item async for item in stream_chat(provider, "m", [ChatMessage("user", "x")])]

    with pytest.raises(LLMRepetition):
        asyncio.run(consume(Looping()))
    assert closed == [True]

    class Long:
        async def chat_stream(self, model, messages, tools=None):
            for number in range(300):
                yield f"Шаг {number}: проверить поле {number * 7} и записать результат в отчёт. "
            yield ChatResult(content="ok", tool_calls=())

    # Длинный, но не повторяющийся ответ доходит целиком.
    assert len(asyncio.run(consume(Long()))) == 301


def test_thinking_leaked_as_text_is_cut_from_answer() -> None:
    import pytest

    from local_agent.llm.base import LLMEmptyResponse, stream_chat
    from local_agent.llm.models import ChatResult

    call = ToolCall(id="c1", name="browser_browser_hover", arguments='{"target": "e1"}')

    class Leaking:
        def __init__(self, text, calls=()):
            self.text, self.calls = text, calls

        async def chat_stream(self, model, messages, tools=None):
            yield self.text
            yield ChatResult(content=self.text, tool_calls=self.calls)

    async def result(provider):
        return [item async for item in stream_chat(provider, "m", [ChatMessage("user", "x")])][-1]

    # Gemma 4 через Ollama: рассуждения пришли текстом и закрыты маркером — в ответ идёт только хвост.
    assert asyncio.run(result(Leaking("Навожу курсор на первую книгу…<channel|>Готово."))).content == "Готово."
    hover = asyncio.run(result(Leaking("Навожу курсор по очереди.<channel|>", (call,))))
    assert hover.content is None and hover.tool_calls == (call,)
    assert asyncio.run(result(Leaking("Обычный ответ"))).content == "Обычный ответ"
    # Одни рассуждения без ответа и вызовов — как пустой ответ: граф повторит запрос.
    with pytest.raises(LLMEmptyResponse):
        asyncio.run(result(Leaking("План действий<channel|>  ")))


def test_repetition_guard_catches_monotone_token_loop_but_not_tables():
    from local_agent.llm.base import RepetitionGuard

    # gemma4 иногда без конца пишет служебные токены канала в рассуждения: разных «слов» там одно.
    stuck = RepetitionGuard()
    assert any(stuck.looped("\n<|channel>thought") for _ in range(2000))

    # Таблица: строка-разделитель одна, строки данных разные — это не цикл.
    table = RepetitionGuard()
    rows = ["| Поле | Значение |", "|" + "-" * 300 + "|"] + [f"| поле {index} | значение {index * 7} |" for index in range(400)]
    assert not any(table.looped(row + "\n") for row in rows)
