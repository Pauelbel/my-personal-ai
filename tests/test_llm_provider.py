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


async def _drain(stream):
    async for _ in stream:
        pass
