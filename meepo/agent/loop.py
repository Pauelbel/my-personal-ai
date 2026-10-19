"""Цикл хода основного агента: модель → вызовы инструментов (с карточками) → модель … → ответ.

Раунд — один запрос к модели и выполнение её вызовов (не больше MAX_CALLS_PER_ROUND). На лимите раундов —
карточка «продолжить или остановить»; «остановить» — последний запрос без инструментов: итог.
События хода уходят в `emit`: delta, reasoning, tool_call, tool_result, approval, log, done.
«Стоп» отменяет задачу хода: написанный текст сохраняется с пометкой «(ответ прерван)».
"""

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from meepo.agent import context
from meepo.agent.approvals import Approvals
from meepo.agent.prompt import system_prompt
from meepo.agent.tools.base import Tool, ToolContext, ToolResult, check_arguments, describe_parameters
from meepo.llm.client import Reply

logger = logging.getLogger(__name__)

MAX_CALLS_PER_ROUND = 4
LOG_CHARS = 160
INTERRUPTED_SUFFIX = "\n\n_(ответ прерван)_"
STOPPED_TEXT = "_(ход остановлен)_"
STOP_NOTE = (
    "Пользователь остановил работу на лимите шагов. Инструменты больше не вызывай. Коротко напиши итог: "
    "что уже сделано (и где лежит результат) и что осталось сделать."
)


class TurnError(Exception):
    """Ход нельзя начать: нет проекта, не выбрана модель."""


@dataclass
class TurnState:
    session_id: str
    request_id: str
    project: dict
    model: str
    window: int
    messages: list[dict]
    max_rounds: int
    emit: Callable[[dict], None]
    rounds: int = 0
    stopped: bool = False
    # Текст ответа, который модель пишет прямо сейчас: его сохраняет «Стоп».
    partial: str = ""
    spent: float = 0.0
    output_tokens: int = 0
    input_tokens: int | None = None
    log: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        """Строка лога хода: видна под ответом и сохраняется с ним."""
        self.log.append(text)
        self.emit({"type": "log", "text": text})


class Agent:
    def __init__(self, *, sessions, projects, approvals: Approvals, llm: Callable, settings: Callable,
                 tools: list[Tool], default_model: str = "") -> None:
        self.sessions = sessions
        self.projects = projects
        self.approvals = approvals
        self.llm = llm
        self.settings = settings
        self.tools = {tool.name: tool for tool in tools}
        self.default_model = default_model

    async def run(self, session_id: str, text: str, request_id: str, emit: Callable[[dict], None]) -> None:
        session = self.sessions.get(session_id)
        project = self.projects.get(session["project_id"]) if session else None
        if project is None:
            raise TurnError("Сессия не привязана к проекту: выберите проект")
        settings = self.settings()
        model = settings.model or self.default_model
        if not model:
            raise TurnError("Модель не выбрана: укажите DEFAULT_MODEL в .env или выберите модель в настройках")
        self.sessions.add_message(session_id, "user", text)
        window = await self.llm().context_length(model) or settings.default_context_tokens
        history = self.sessions.messages(session_id)
        state = TurnState(session_id, request_id, project, model, window, context.build("", history),
                          settings.max_tool_rounds, emit)
        ctx = ToolContext(Path(project["path"]), session_id, self.sessions, request_id, emit)
        try:
            await self._loop(state, ctx)
        except asyncio.CancelledError:
            self._save_stopped(state)
            raise

    async def _loop(self, state: TurnState, ctx: ToolContext) -> None:
        while True:
            if state.rounds >= state.max_rounds and not state.stopped:
                await self._limit(state)
            tools = None if state.stopped else [tool.schema() for tool in self.tools.values()]
            reply = await self._ask(state, tools)
            if reply.tool_calls and tools:
                await self._run_calls(state, ctx, reply)
                state.rounds += 1
                continue
            text = reply.content
            if reply.tool_calls:
                # Работу остановили на лимите: инструменты не предлагались, а модель всё равно их зовёт.
                note = f"Работа остановлена после {state.rounds} шагов. Напишите «продолжай» — продолжу с этого места."
                text = f"{text}\n\n{note}" if text else note
            self._finish(state, text, reply)
            return

    async def _ask(self, state: TurnState, tools: list[dict] | None) -> Reply:
        session = self.sessions.get(state.session_id)
        # Промпт пересобирается каждый раунд: план мог измениться посреди хода.
        state.messages[0] = {"role": "system", "content": system_prompt(state.project, session["plan"])}
        messages = state.messages + ([{"role": "user", "content": STOP_NOTE}] if state.stopped else [])
        state.note(f"Запрос к модели {state.model} · раунд {state.rounds + 1}" + ("" if tools else " · без инструментов"))
        started = time.perf_counter()
        state.partial = ""
        reply = None
        async for item in self.llm().stream(state.model, messages, tools):
            if isinstance(item, Reply):
                reply = item
            elif item[0] == "delta":
                state.partial += item[1]
                state.emit({"type": "delta", "text": item[1]})
            elif item[1]:
                state.emit({"type": "reasoning", "text": item[1]})
        elapsed = time.perf_counter() - started
        state.spent += elapsed
        state.output_tokens += reply.output_tokens or 0
        state.input_tokens = reply.input_tokens
        tokens = f", токенов: вход {reply.input_tokens or '?'}, выход {reply.output_tokens or '?'}"
        calls = f"; вызовы: {', '.join(call['name'] for call in reply.tool_calls)}" if reply.tool_calls else ""
        state.note(f"Модель ответила за {elapsed:.1f} с{tokens}{calls}")
        self.sessions.update(state.session_id, context_tokens=reply.input_tokens, context_window=state.window)
        return reply

    async def _run_calls(self, state: TurnState, ctx: ToolContext, reply: Reply) -> None:
        calls = reply.tool_calls
        self.sessions.add_message(state.session_id, "assistant", reply.content, tool_calls=calls, reasoning=reply.reasoning)
        state.messages.append({"role": "assistant", "content": reply.content or None, "tool_calls": calls})
        state.partial = ""
        replies = []
        for index, call in enumerate(calls):
            state.emit({"type": "tool_call", "call": call})
            if index >= MAX_CALLS_PER_ROUND:
                result = ToolResult(f"Не больше {MAX_CALLS_PER_ROUND} вызовов за раунд — повтори этот вызов отдельно", True)
            else:
                result, answer = await self._call(state, ctx, call)
                if answer:
                    replies.append(answer)
            self.sessions.add_message(state.session_id, "tool", result.content, tool_call_id=call["id"],
                                      tool_name=call["name"], is_error=result.is_error, meta=result.meta)
            state.messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                   "content": result.content})
            state.emit({"type": "tool_result", "call_id": call["id"], "name": call["name"],
                        "content": result.content, "is_error": result.is_error, "meta": result.meta})
        for text in replies:
            # Ответ на карточку — обычная реплика пользователя: она идёт после результатов вызовов.
            self.sessions.add_message(state.session_id, "user", text, card_reply=True)
            state.messages.append({"role": "user", "content": text})

    async def _call(self, state: TurnState, ctx: ToolContext, call: dict) -> tuple[ToolResult, str]:
        """Результат вызова и текст пользователя, если он ответил на карточку словами."""
        tool = self.tools.get(call["name"])
        if tool is None:
            return ToolResult(f"Инструмента «{call['name']}» нет. Доступны: {', '.join(self.tools)}", True), ""
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return ToolResult("Аргументы должны быть JSON-объектом", True), ""
        # Неполный вызов пользователю не показываем: модель сначала исправит его сама.
        if problem := check_arguments(tool.parameters, arguments):
            return ToolResult(f"{problem}. Параметры {tool.name}: {describe_parameters(tool.parameters)}. "
                              "Исправь и вызови снова.", True), ""
        session = self.sessions.get(state.session_id)
        if tool.group and tool.group not in session["trusted"]:
            try:
                preview = tool.preview(arguments, ctx) if tool.preview else {}
            except (ValueError, OSError, UnicodeError) as exc:
                return ToolResult(f"{exc}. Исправь и вызови снова.", True), ""
            decision = await self.approvals.ask(state.session_id, {
                "kind": "tool", "tool": tool.name, "group": tool.group, "arguments": arguments, **preview,
            }, state.emit)
            state.note(f"Решение по {tool.name}: {decision['action']}" + (" · доверие на сессию" if decision["trust"] else ""))
            if decision["trust"]:
                self.sessions.trust(state.session_id, tool.group)
            if decision["action"] == "reject":
                return ToolResult("Пользователь отклонил этот вызов", True), ""
            if decision["action"] == "reply":
                return ToolResult(f"Пользователь не разрешил вызов и ответил: «{decision['text']}». "
                                  "Учти ответ: доработай действие или спроси, как поступить.", True), decision["text"]
        return await self._execute(state, ctx, tool, arguments), ""

    async def _execute(self, state: TurnState, ctx: ToolContext, tool: Tool, arguments: dict) -> ToolResult:
        state.note(f"Вызов {tool.name} {_shorten(json.dumps(arguments, ensure_ascii=False))}")
        started = time.perf_counter()
        try:
            if inspect.iscoroutinefunction(tool.run):
                result = await tool.run(arguments, ctx)
            else:
                result = await asyncio.to_thread(tool.run, arguments, ctx)
        except (ValueError, OSError, UnicodeError) as exc:
            result = ToolResult(str(exc), True)
        except Exception as exc:  # noqa: BLE001 — сбой одного инструмента не роняет ход: модель получает ошибку
            logger.exception("Инструмент %s упал", tool.name)
            result = ToolResult(f"Внутренняя ошибка инструмента: {type(exc).__name__}: {exc}", True)
        outcome = f"ошибка — {_shorten(result.content)}" if result.is_error else f"готово, {len(result.content)} символов"
        state.note(f"{tool.name}: {outcome} за {time.perf_counter() - started:.1f} с")
        return result

    async def _limit(self, state: TurnState) -> None:
        step = self.settings().max_tool_rounds
        decision = await self.approvals.ask(state.session_id, {
            "kind": "limit", "title": f"Сделано шагов: {state.rounds} — продолжить работу?",
            "text": f"Это лимит хода. «Да» даст ещё {step} шагов; «Нет» — агент напишет итог: что сделано и что осталось.",
        }, state.emit)
        if decision["action"] == "approve":
            state.max_rounds += step
        else:
            state.stopped = True

    def _finish(self, state: TurnState, text: str, reply: Reply) -> None:
        stats = {"seconds": round(state.spent, 1), "rounds": state.rounds + 1,
                 "input_tokens": state.input_tokens, "output_tokens": state.output_tokens or None}
        message = self.sessions.add_message(state.session_id, "assistant", text, reasoning=reply.reasoning,
                                            stats=stats, log=state.log)
        state.emit({"type": "done", "message": message})

    def _save_stopped(self, state: TurnState) -> None:
        text = state.partial.strip()
        self.sessions.add_message(state.session_id, "assistant", text + INTERRUPTED_SUFFIX if text else STOPPED_TEXT,
                                  log=state.log)


def _shorten(text: str, limit: int = LOG_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
