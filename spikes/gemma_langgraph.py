"""Spike: LangGraph + локальная Gemma через OpenAI-compatible API.

Проверяет на живой модели:
  A1. Стриминг: текстовые и reasoning-дельты через наш OpenAICompatibleProvider.
  A2. Tool calling: модель сама выбирает инструменты, результаты возвращаются ей.
  A3. Structured output: response_format json_schema, ответ проходит pydantic-валидацию.
  A4. LangGraph-граф на нашем провайдере: стриминг через get_stream_writer, interrupt перед
      инструментом с подтверждением, «перезапуск сервера» (новый граф и новое соединение с SQLite)
      и resume через Command.
  B.  Для сравнения ChatOpenAI(base_url=…): bind_tools, astream (видны ли рассуждения),
      with_structured_output(method="json_schema").

Запуск:
  uv run --extra spike python spikes/gemma_langgraph.py                          # LM Studio, google/gemma-4-12b
  uv run --extra spike python spikes/gemma_langgraph.py --ollama                 # Ollama, gemma4:12b
  uv run --extra spike python spikes/gemma_langgraph.py --base-url URL --model ID --repeat 3
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field, ValidationError

from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta, ToolCall
from local_agent.llm.openai_compatible import OpenAICompatibleProvider

SYSTEM = (
    "Ты локальный ассистент. Отвечай по-русски. Если для задачи нужен инструмент из списка — вызови его, "
    "не выдумывай содержимое файлов."
)
FILES = {"notes.txt": "Встреча в пятницу в 15:00. Обсудить релиз 2.4 и тест-план регресса. Отвечает Павел."}

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Прочитать текстовый файл рабочей папки",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Путь к файлу"}},
                       "required": ["path"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Записать текстовый файл в рабочую папку (нужно подтверждение)",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "content": {"type": "string", "description": "Полное содержимое файла"},
        }, "required": ["path", "content"], "additionalProperties": False},
    }},
]
REQUIRES_APPROVAL = {"write_file"}
TASK = "Прочитай notes.txt и запиши в summary.txt краткое содержание одной строкой."


class ToolProposal(BaseModel):
    """Так основной агент будет предлагать новый инструмент: всё, что пользователь увидит в карточке."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    description: str = Field(min_length=10, max_length=200)
    reason: str = Field(min_length=10)
    parameters: dict
    requires_approval: bool
    code: str = Field(min_length=20)


PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,40}$"},
        "description": {"type": "string"},
        "reason": {"type": "string"},
        "parameters": {"type": "object"},
        "requires_approval": {"type": "boolean"},
        "code": {"type": "string"},
    },
    "required": ["name", "description", "reason", "parameters", "requires_approval", "code"],
}
PROPOSAL_PROMPT = (
    "Пользователь просит написать тест-кейсы по документации, но инструмента поиска файлов по маске нет. "
    "Предложи такой инструмент: имя, описание в одну строку, зачем он нужен, JSON Schema параметров, "
    "нужно ли подтверждение, и код Python-скрипта: аргументы приходят JSON-объектом в stdin, результат — в stdout. "
    "Ответь только JSON."
)

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'OK ' if ok else 'FAIL'} {name}: {detail}", flush=True)


def fake_tool(call: ToolCall) -> str:
    args = json.loads(call.arguments or "{}")
    if call.name == "read_file":
        return FILES.get(str(args.get("path", "")).lstrip("./"), "Файл не найден")
    if call.name == "write_file":
        return f"Записано {len(str(args.get('content', '')))} символов в {args.get('path')}"
    return "Неизвестный инструмент"


async def collect(provider, model, messages, tools=None, options=None):
    text, reasoning, result = [], [], None
    started = time.perf_counter()
    first = None
    async for item in provider.chat_stream(model, messages, tools, options):
        if first is None and not isinstance(item, ChatResult):
            first = time.perf_counter() - started
        if isinstance(item, ReasoningDelta):
            reasoning.append(item.text)
        elif isinstance(item, str):
            text.append(item)
        else:
            result = item
    return "".join(text), "".join(reasoning), result, first, time.perf_counter() - started


async def a1_streaming(provider, model, options):
    messages = [ChatMessage("system", SYSTEM), ChatMessage("user", "Сколько будет 17 * 23? Подумай и ответь числом.")]
    text, reasoning, result, first, total = await collect(provider, model, messages, options=options)
    report("A1 стриминг", bool(text), f"текст {len(text)} симв., reasoning {len(reasoning)} симв., "
           f"первый токен {first or 0:.1f} с, всего {total:.1f} с, вход/выход {result.input_tokens}/{result.output_tokens}; "
           f"ответ: {' '.join(text.split())[:80]!r}")


async def a2_tool_calling(provider, model, options):
    messages = [ChatMessage("system", SYSTEM), ChatMessage("user", TASK)]
    calls_made = []
    for _ in range(5):
        text, _, result, _, _ = await collect(provider, model, messages, TOOLS, options)
        if not result.tool_calls:
            break
        messages.append(ChatMessage("assistant", text or None, result.tool_calls))
        for call in result.tool_calls:
            json.loads(call.arguments)  # аргументы должны быть валидным JSON
            calls_made.append(call.name)
            messages.append(ChatMessage("tool", fake_tool(call), tool_call_id=call.id))
    ok = calls_made[:1] == ["read_file"] and "write_file" in calls_made and bool(result.content)
    report("A2 tool calling", ok, f"вызовы {calls_made}; финал: {' '.join((result.content or '').split())[:80]!r}")


async def a3_structured(provider, model):
    started = time.perf_counter()
    result = await provider.chat(
        model, [ChatMessage("system", SYSTEM), ChatMessage("user", PROPOSAL_PROMPT)],
        response_format={"type": "json_schema", "json_schema": {"name": "tool_proposal", "schema": PROPOSAL_SCHEMA}},
    )
    try:
        proposal = ToolProposal.model_validate_json(result.content or "")
        report("A3 structured output", True, f"{proposal.name}: {proposal.description!r}, "
               f"approval={proposal.requires_approval}, код {len(proposal.code)} симв., {time.perf_counter() - started:.1f} с")
    except ValidationError as exc:
        report("A3 structured output", False, f"{exc.error_count()} ошибок: {(result.content or '')[:200]!r}")


# ---------- A4: LangGraph на нашем провайдере ----------

def keep_last(_old, new):
    return new


class TurnState(TypedDict):
    messages: list[dict]
    # Решения пользователя по вызовам текущего раунда: call_id → approve/reject/текст.
    decisions: Annotated[dict, keep_last]


def to_chat(item: dict) -> ChatMessage:
    return ChatMessage(item["role"], item.get("content"),
                       tuple(ToolCall(**call) for call in item.get("tool_calls", [])), item.get("tool_call_id"))


def build_graph(provider, model, options, checkpointer):
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    async def call_model(state: TurnState):
        write = get_stream_writer()
        text, result = [], None
        async for item in provider.chat_stream(model, [to_chat(m) for m in state["messages"]], TOOLS, options):
            if isinstance(item, ReasoningDelta):
                write({"type": "reasoning", "text": item.text})
            elif isinstance(item, str):
                text.append(item)
                write({"type": "delta", "text": item})
            else:
                result = item
        message = {"role": "assistant", "content": "".join(text) or result.content,
                   "tool_calls": [call.__dict__ for call in result.tool_calls]}
        return {"messages": [*state["messages"], message], "decisions": {}}

    def route(state: TurnState):
        return "approve" if state["messages"][-1].get("tool_calls") else END

    def approve(state: TurnState):
        # Отдельный узел: при resume он выполняется заново, поэтому здесь нет побочных эффектов,
        # только interrupt по каждому вызову, которому нужно подтверждение.
        decisions = {}
        for call in state["messages"][-1]["tool_calls"]:
            if call["name"] in REQUIRES_APPROVAL:
                decisions[call["id"]] = interrupt({"kind": "tool_call", "call": call})
        return {"decisions": decisions}

    async def run_tools(state: TurnState):
        messages = list(state["messages"])
        for call in messages[-1]["tool_calls"]:
            decision = state["decisions"].get(call["id"], "approve")
            content = fake_tool(ToolCall(**call)) if decision == "approve" else "Пользователь отклонил этот вызов"
            messages.append({"role": "tool", "content": content, "tool_call_id": call["id"]})
        return {"messages": messages}

    graph = StateGraph(TurnState)
    graph.add_node("call_model", call_model)
    graph.add_node("approve", approve)
    graph.add_node("run_tools", run_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", route, ["approve", END])
    graph.add_edge("approve", "run_tools")
    graph.add_edge("run_tools", "call_model")
    return graph.compile(checkpointer=checkpointer)


async def drive(graph, payload, config):
    """Прогоняет граф до конца или до interrupt; возвращает (interrupts, число дельт, финальное состояние)."""
    deltas = reasoning = 0
    interrupts = []
    async for mode, chunk in graph.astream(payload, config, stream_mode=["custom", "updates"]):
        if mode == "custom":
            deltas += chunk["type"] == "delta"
            reasoning += chunk["type"] == "reasoning"
        elif "__interrupt__" in chunk:
            interrupts.extend(chunk["__interrupt__"])
    state = await graph.aget_state(config)
    return interrupts, deltas, reasoning, state


async def a4_langgraph(provider, model, options):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    database = Path(tempfile.mkdtemp()) / "checkpoints.sqlite"
    config = {"configurable": {"thread_id": "spike-session"}}
    start = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}], "decisions": {}}
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_graph(provider, model, options, saver)
        interrupts, deltas, reasoning, state = await drive(graph, start, config)
    if not interrupts:
        report("A4 LangGraph interrupt", False, f"interrupt не случился, следующий узел {state.next}")
        return
    pending = interrupts[0].value
    report("A4 LangGraph interrupt", pending["call"]["name"] == "write_file",
           f"ждёт подтверждения {pending['call']['name']} {pending['call']['arguments'][:80]}; "
           f"дельт {deltas}, reasoning {reasoning}")
    # «Перезапуск сервера»: новое соединение с базой и новый граф, состояние берётся из чекпоинта.
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_graph(provider, model, options, saver)
        interrupts, deltas, reasoning, state = await drive(graph, Command(resume="approve"), config)
    messages = state.values["messages"]
    tools = [m for m in messages if m["role"] == "tool"]
    final = messages[-1]
    ok = not interrupts and final["role"] == "assistant" and not final.get("tool_calls") and any(
        m["content"].startswith("Записано") for m in tools)
    report("A4 LangGraph resume после перезапуска", ok,
           f"результаты инструментов {[m['content'][:30] for m in tools]}; финал: {' '.join((final.get('content') or '').split())[:80]!r}")


# ---------- B: ChatOpenAI ----------

async def b_chat_openai(base_url, model, options):
    try:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
        from langchain_openai import ChatOpenAI
    except ImportError:
        report("B ChatOpenAI", False, "нет langchain-openai: запустите с --extra spike")
        return
    llm = ChatOpenAI(base_url=base_url, api_key="local", model=model, timeout=300,
                     **({"model_kwargs": options} if options else {}))
    bound = llm.bind_tools(TOOLS)
    messages = [SystemMessage(SYSTEM), HumanMessage(TASK)]
    reasoning_seen = False
    calls_made = []
    final = None
    for _ in range(5):
        merged = None
        async for chunk in bound.astream(messages):
            merged = chunk if merged is None else merged + chunk
            kwargs = chunk.additional_kwargs or {}
            blocks = chunk.content_blocks if hasattr(chunk, "content_blocks") else []
            if kwargs.get("reasoning_content") or kwargs.get("reasoning") or any(
                    b.get("type") == "reasoning" for b in blocks):
                reasoning_seen = True
        messages.append(merged)
        if not merged.tool_calls:
            final = merged
            break
        for call in merged.tool_calls:
            calls_made.append(call["name"])
            content = fake_tool(ToolCall(call["id"], call["name"], json.dumps(call["args"], ensure_ascii=False)))
            messages.append(ToolMessage(content, tool_call_id=call["id"]))
    report("B ChatOpenAI tool calling", calls_made[:1] == ["read_file"] and "write_file" in calls_made,
           f"вызовы {calls_made}; reasoning в стриме виден: {reasoning_seen}; "
           f"финал: {' '.join(str(final.content if final else '').split())[:60]!r}")
    try:
        structured = llm.with_structured_output(ToolProposal, method="json_schema")
        proposal = await structured.ainvoke([SystemMessage(SYSTEM), HumanMessage(PROPOSAL_PROMPT)])
        report("B ChatOpenAI structured output", isinstance(proposal, ToolProposal), f"{proposal.name}")
    except Exception as exc:  # noqa: BLE001 — в spike важно увидеть любую ошибку обвязки
        report("B ChatOpenAI structured output", False, f"{type(exc).__name__}: {str(exc)[:200]}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ollama", action="store_true", help="Ollama на :11434 с gemma4:12b")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--repeat", type=int, default=1, help="Сколько раз прогнать сценарии")
    parser.add_argument("--think", choices=["default", "on", "off"], default="default",
                        help="reasoning_effort: on — high, off — none (если сервер понимает)")
    parser.add_argument("--skip-b", action="store_true", help="Не проверять ChatOpenAI")
    args = parser.parse_args()
    base_url = args.base_url or ("http://127.0.0.1:11434/v1" if args.ollama else "http://127.0.0.1:1234/v1")
    model = args.model or ("gemma4:12b" if args.ollama else "google/gemma-4-12b")
    options = {"on": {"reasoning_effort": "high"}, "off": {"reasoning_effort": "none"}}.get(args.think)
    provider = OpenAICompatibleProvider(base_url, 300, name="spike")
    try:
        models = await provider.list_models()
        if model not in models:
            print(f"Модель {model!r} не найдена на {base_url}. Есть: {models}")
            return 2
        print(f"Сервер {base_url}, модель {model}, окно {await provider.context_length(model)}, reasoning: {args.think}")
        for run in range(args.repeat):
            print(f"\nПрогон {run + 1}/{args.repeat}")
            for step in (a1_streaming(provider, model, options), a2_tool_calling(provider, model, options),
                         a3_structured(provider, model), a4_langgraph(provider, model, options)):
                try:
                    await step
                except Exception as exc:  # noqa: BLE001
                    report(step.__name__, False, f"{type(exc).__name__}: {str(exc)[:200]}")
            if not args.skip_b:
                await b_chat_openai(base_url, model, options)
    finally:
        await provider.close()
    failed = [name for name, ok, _ in results if not ok]
    print(f"\nИтого: {len(results) - len(failed)} из {len(results)} проверок прошли" + (f"; упали: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
