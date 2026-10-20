"""Клиент модели: рассуждения отключаются запросом; сервер, не принявший параметр, получает запрос без него."""

import asyncio
import json

import httpx

from meepo.llm.client import LLMClient, Reply


def sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks).encode("utf-8") + b"data: [DONE]\n\n"


ANSWER = sse({"choices": [{"delta": {"content": "Готово: файл test_web_ui/google_test_cases.md"}}]})


def ask(client: LLMClient) -> Reply:
    async def run():
        return [item async for item in client.stream("m", [{"role": "user", "content": "привет"}])][-1]
    return asyncio.run(run())


def test_reasoning_disabled_in_request():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=ANSWER)

    reply = ask(LLMClient("http://model/v1", transport=httpx.MockTransport(handler)))
    assert reply.content.startswith("Готово") and bodies[0]["reasoning_effort"] == "none"


def test_server_rejecting_parameter_gets_request_without_it():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "unknown field reasoning_effort"}})
        return httpx.Response(200, content=ANSWER)

    client = LLMClient("http://model/v1", transport=httpx.MockTransport(handler))
    assert ask(client).content.startswith("Готово")
    assert ask(client).content.startswith("Готово")
    # Первый запрос отвергнут и повторён без параметра; следующий сразу идёт без него.
    assert ["reasoning_effort" in body for body in bodies] == [True, False, False]
