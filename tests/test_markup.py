"""Разметка, которую сервер не разобрал: рассуждения в тексте ответа и вызовы Gemma текстом."""

import asyncio
import json

import httpx

from local_agent.llm.markup import ThinkingRouter, clean_reasoning, recover_tool_calls
from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta
from local_agent.llm.openai_compatible import OpenAICompatibleProvider

TOOLS = [{"type": "function", "function": {"name": "browser_browser_type", "parameters": {"type": "object"}}}]


def route(chunks):
    router = ThinkingRouter()
    pieces = [piece for chunk in chunks for piece in router.feed(chunk)] + router.flush()
    thinking = "".join(text for flag, text in pieces if flag)
    answer = "".join(text for flag, text in pieces if not flag)
    return thinking, answer


def test_thinking_is_split_from_answer_even_when_marker_is_cut():
    # Метки разрезаны между кусками потока: «<|chan» + «nel>thought».
    assert route(["<|chan", "nel>thought\nДумаю", " о задаче<chan", "nel|>Ответ: ", "42"]) == ("\nДумаю о задаче", "Ответ: 42")
    assert route(["<think>план</think>", "Готово"]) == ("план", "Готово")
    assert route(["Обычный ответ без меток, a < b"]) == ("", "Обычный ответ без меток, a < b")
    assert clean_reasoning("<|channel>thought\nплан<channel|>") == "\nплан"


def test_gemma_call_left_in_text_is_recovered():
    text = 'call:browser_browser_type{target:<|"|>e197<|"|>,text:<|"|>John, "Doe"<|"|>,slowly:false}<tool_call|><|tool_response>'
    calls, rest = recover_tool_calls(f"Заполняю форму. {text}", {"browser_browser_type"})
    assert [call.name for call in calls] == ["browser_browser_type"] and rest == "Заполняю форму."
    assert json.loads(calls[0].arguments) == {"target": "e197", "text": 'John, "Doe"', "slowly": False}
    # Незнакомый инструмент не выдумываем.
    assert recover_tool_calls(text, {"read_file"}) == ((), text)


def sse(*deltas):
    lines = [f"data: {json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False)}" for delta in deltas]
    return httpx.Response(200, text="\n\n".join([*lines, "data: [DONE]"]) + "\n\n",
                          headers={"Content-Type": "text/event-stream"})


def stream(*deltas, tools=TOOLS):
    async def run():
        provider = OpenAICompatibleProvider("http://llm.test/v1", 5, transport=httpx.MockTransport(lambda request: sse(*deltas)))
        try:
            return [item async for item in provider.chat_stream("m", [ChatMessage("user", "x")], tools)]
        finally:
            await provider.close()

    return asyncio.run(run())


def test_call_written_in_reasoning_becomes_a_real_tool_call():
    # Как в диалоге SauceDemo: LM Studio оставил вызов в reasoning_content, а в ответе — «Затем нажму Continue».
    items = stream(
        {"reasoning_content": 'call:browser_browser_type{target:<|"|>e197<|"|>,text:<|"|>John<|"|>}<tool_call|><|tool_response>'},
        {"content": "Заполняю форму."},
    )
    result = items[-1]
    assert isinstance(result, ChatResult) and [call.name for call in result.tool_calls] == ["browser_browser_type"]
    assert json.loads(result.tool_calls[0].arguments) == {"target": "e197", "text": "John"}
    assert result.content == "Заполняю форму."


def test_thinking_inside_content_goes_to_reasoning_not_chat():
    items = stream({"content": "<|channel>thought\nНадо отв"}, {"content": "етить<channel|>Привет!"}, tools=None)
    shown = "".join(item for item in items if isinstance(item, str))
    reasoning = "".join(item.text for item in items if isinstance(item, ReasoningDelta))
    assert shown == "Привет!" and "Надо ответить" in reasoning and "<|channel>" not in reasoning
    assert items[-1].content == "Привет!"
