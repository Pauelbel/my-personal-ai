"""Runtime выполняет ход агента: собирает контекст, стримит ответ LLM, вызывает инструменты и сохраняет историю."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass

from local_agent.agent.context import build_context, build_system_prompt, estimate_message_tokens
from local_agent.agent.registry import AgentRegistry
from local_agent.agent.summary import ConversationSummarizer
from local_agent.agent.tool_executor import ToolExecutor
from local_agent.llm.base import context_length, stream_chat
from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta
from local_agent.llm.registry import LLMRegistry
from local_agent.memory.conversation import ConversationService
from local_agent.memory.models import Message
from local_agent.memory.service import MemoryService, MemoryServiceError
from local_agent.sessions.service import SessionService
from local_agent.tools.base import ToolResult
from local_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

LOG_ARGUMENTS_CHARS = 160
MAX_TOOL_ROUNDS = 5
MAX_CALLS_PER_ROUND = 4
APPROVAL_TIMEOUT_SECONDS = 300
INTERRUPTED_SUFFIX = "\n\n_(ответ прерван)_"


class AgentRuntimeError(Exception):
    """Сессию нельзя обработать из-за неподдерживаемой конфигурации."""


class SessionNotFoundError(AgentRuntimeError):
    """Запрошенная сессия не существует."""


class SessionBusyError(AgentRuntimeError):
    """В сессии уже выполняется другой ход."""


@dataclass(frozen=True)
class RuntimeLimits:
    max_context_messages: int
    default_context_tokens: int
    response_reserve_tokens: int
    memory_auto_update_messages: int


class AgentRuntime:
    def __init__(
        self,
        sessions: SessionService,
        conversation: ConversationService,
        agents: AgentRegistry,
        providers: LLMRegistry,
        memory: MemoryService,
        tools: ToolRegistry,
        summarizer: ConversationSummarizer,
        limits: RuntimeLimits,
    ) -> None:
        self._sessions = sessions
        self._conversation = conversation
        self._agents = agents
        self._providers = providers
        self._memory = memory
        self._tools = tools
        self._summarizer = summarizer
        self._limits = limits
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._approvals: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._background: dict[str, asyncio.Task] = {}

    def is_busy(self, session_id: str) -> bool:
        lock = self._session_locks.get(session_id)
        return lock is not None and lock.locked()

    def resolve_approval(self, session_id: str, call_id: str, approved: bool) -> bool:
        future = self._approvals.get((session_id, call_id))
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True

    async def run_turn(self, session_id: str, content: str, request_id: str) -> Message:
        """Ход без стриминга: подтверждать некому, поэтому изменяющие инструменты отклоняются."""
        answer: Message | None = None
        async for event in self.stream_turn(session_id, content, request_id):
            if event["type"] == "approval_required":
                self.resolve_approval(session_id, event["call"]["id"], False)
            elif event["type"] == "done":
                answer = event["message"]
        assert answer is not None
        return answer

    async def stream_turn(
        self, session_id: str, content: str, request_id: str
    ) -> AsyncIterator[dict]:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionBusyError("В этой сессии ещё выполняется предыдущий ход")
        async with lock:
            async for event in self._turn(session_id, content, request_id):
                yield event

    async def close(self) -> None:
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _turn(self, session_id: str, content: str, request_id: str) -> AsyncIterator[dict]:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError("Сессия не найдена")
        agent = self._agents.get(session.agent_id)
        if agent is None:
            raise AgentRuntimeError("Агент не найден")
        provider_id = session.provider or agent.llm_provider
        provider = self._providers.get(provider_id)
        if provider is None:
            raise AgentRuntimeError("Провайдер LLM не найден")
        model = session.model or agent.model
        if not model:
            raise AgentRuntimeError("Выберите модель для этой сессии")

        user_message = await asyncio.to_thread(self._conversation.add_user_message, session_id, content)
        self._sessions.name_from_first_message(
            session_id, content, self._conversation.count(session_id)
        )
        yield {"type": "user_message", "message": user_message}

        executor = ToolExecutor.for_session(self._tools, agent, session, request_id)
        definitions = executor.definitions()
        loaded_window = await context_length(provider, model)
        window = loaded_window or self._limits.default_context_tokens
        history = await asyncio.to_thread(
            self._conversation.recent, session_id, self._limits.max_context_messages * 10
        )
        memory_context = self._memory.context()
        context = build_context(
            build_system_prompt(agent.system_prompt, memory_context, session.summary),
            history,
            max_messages=self._limits.max_context_messages,
            max_tokens=max(window - self._limits.response_reserve_tokens, 0),
            tools=definitions,
        )
        messages = context.messages
        used = sum(estimate_message_tokens(message) for message in messages)
        yield _log(
            f"Контекст: {context.included_count} из {len(history)} сообщений истории, "
            f"≈{used} из {window} токенов окна{'' if loaded_window else ' (по умолчанию)'}"
        )
        extras = [name for name, present in (("память", memory_context), ("резюме", session.summary)) if present]
        yield _log(f"В системный промпт добавлено: {', '.join(extras)}" if extras else "Память и резюме не подключены")
        yield _log(
            "Инструменты: " + ", ".join(item["function"]["name"] for item in definitions)
            if definitions else "Инструменты недоступны: выберите рабочую папку и включите их"
        )
        added = 0
        partial: list[str] = []
        result: ChatResult | None = None
        started = time.perf_counter()
        try:
            rounds = 0
            while True:
                offered = definitions if rounds < MAX_TOOL_ROUNDS else None
                result = None
                yield _log(
                    f"Запрос к {provider_id} · {model}" + (f" · раунд {rounds + 1}" if rounds else "")
                    + ("" if offered or not definitions else " · без инструментов (лимит раундов)")
                )
                requested = time.perf_counter()
                waiting = True
                async for item in stream_chat(provider, model, messages, offered):
                    if waiting and not isinstance(item, ChatResult):
                        waiting = False
                        yield _log(f"Первый токен через {time.perf_counter() - requested:.1f} с")
                    if isinstance(item, str):
                        partial.append(item)
                        yield {"type": "delta", "text": item}
                    elif isinstance(item, ReasoningDelta):
                        yield {"type": "reasoning", "text": item.text}
                    else:
                        result = item
                if result is None:
                    raise AgentRuntimeError("Модель не вернула ответ")
                yield _log(_round_summary(result, time.perf_counter() - requested))
                if not result.tool_calls:
                    break
                if rounds == MAX_TOOL_ROUNDS:
                    raise AgentRuntimeError("Модель превысила лимит раундов вызова инструментов")
                rounds += 1

                text = "".join(partial)
                partial = []
                assistant = await asyncio.to_thread(
                    self._conversation.add_assistant_message, session_id, text, result.tool_calls
                )
                added += 1
                yield {"type": "tool_calls", "message": assistant}
                messages.append(ChatMessage(role="assistant", content=text or None, tool_calls=result.tool_calls))

                for index, call in enumerate(result.tool_calls):
                    if index >= MAX_CALLS_PER_ROUND:
                        output = ToolResult("Лимит вызовов инструментов за один раунд", is_error=True)
                    else:
                        tool = executor.get(call.name)
                        yield _log(f"Вызов {call.name} {_shorten(call.arguments, LOG_ARGUMENTS_CHARS)}")
                        approved = True
                        if tool is not None and tool.requires_approval:
                            future = asyncio.get_running_loop().create_future()
                            self._approvals[(session_id, call.id)] = future
                            try:
                                yield _log("Жду подтверждения пользователя")
                                yield {
                                    "type": "approval_required",
                                    "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
                                    "tool_name": tool.name,
                                }
                                approved = await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
                            except TimeoutError:
                                approved = False
                            finally:
                                self._approvals.pop((session_id, call.id), None)
                            yield _log("Вызов разрешён" if approved else "Вызов отклонён")
                        tool_started = time.perf_counter()
                        output = (
                            await executor.execute(call) if approved
                            else ToolResult("Пользователь отклонил этот вызов", is_error=True)
                        )
                        yield _log(
                            f"{call.name}: "
                            + (f"ошибка — {_shorten(output.content, LOG_ARGUMENTS_CHARS)}" if output.is_error
                               else f"готово, {len(output.content)} символов")
                            + f" за {time.perf_counter() - tool_started:.1f} с"
                        )
                    tool_message = await asyncio.to_thread(
                        self._conversation.add_tool_message,
                        session_id,
                        call_id=call.id,
                        name=call.name,
                        content=output.content,
                        is_error=output.is_error,
                    )
                    added += 1
                    yield {"type": "tool_result", "message": tool_message}
                    messages.append(ChatMessage(role="tool", content=output.content, tool_call_id=call.id))
        except (asyncio.CancelledError, GeneratorExit):
            # Пользователь нажал «Стоп» или закрыл вкладку: сохраняем то, что модель успела написать.
            if text := "".join(partial).strip():
                self._conversation.add_assistant_message(session_id, text + INTERRUPTED_SUFFIX)
            raise
        except Exception:
            logger.exception(
                "Запрос к LLM завершился ошибкой request_id=%s session_id=%s agent_id=%s provider=%s model=%s latency_ms=%.1f",
                request_id, session_id, agent.id, provider_id, model,
                (time.perf_counter() - started) * 1000,
            )
            raise

        if not result.content:
            raise AgentRuntimeError("Модель не вернула ответ")
        answer = await asyncio.to_thread(self._conversation.add_assistant_message, session_id, result.content)
        added += 1
        count = result.input_tokens
        self._sessions.set_context_tokens(
            session_id, count if isinstance(count, int) and count >= 0 else None
        )
        logger.info(
            "Запрос к LLM выполнен request_id=%s session_id=%s agent_id=%s provider=%s model=%s input_tokens=%s output_tokens=%s latency_ms=%.1f",
            request_id, session_id, agent.id, provider_id, model,
            result.input_tokens, result.output_tokens,
            (time.perf_counter() - started) * 1000,
        )
        self._after_turn(session_id, model, context.included_count + added)
        yield {"type": "done", "message": answer}

    def _after_turn(self, session_id: str, model: str, included_count: int) -> None:
        if self._conversation.count(session_id) > included_count:
            self._spawn(f"summary:{session_id}", self._summarizer.update(session_id, included_count, model))
        threshold = self._limits.memory_auto_update_messages
        if threshold and self._memory.pending_user_messages(session_id) >= threshold:
            self._spawn(f"memory:{session_id}", self._update_memory(session_id))

    async def _update_memory(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            result = await self._memory.update_session(session)
            logger.info(
                "Автоматическое обновление памяти session_id=%s processed=%s applied=%s",
                session_id, result.processed_messages, result.applied_operations,
            )
        except MemoryServiceError:
            logger.warning("Автоматическое обновление памяти не удалось session_id=%s", session_id, exc_info=True)

    def _spawn(self, key: str, coroutine: Coroutine) -> None:
        """Фоновая работа после ответа; одна задача каждого вида на сессию."""
        running = self._background.get(key)
        if running is not None and not running.done():
            coroutine.close()
            return
        task = asyncio.create_task(coroutine, name=key)
        self._background[key] = task
        task.add_done_callback(lambda done: self._on_background_done(key, done))

    def _on_background_done(self, key: str, task: asyncio.Task) -> None:
        if self._background.get(key) is task:
            del self._background[key]
        if not task.cancelled() and task.exception() is not None:
            logger.error("Фоновая задача завершилась ошибкой key=%s", key, exc_info=task.exception())


def _log(text: str) -> dict:
    """Строка лога хода: UI показывает её под статусом «Модель думает»."""
    return {"type": "log", "text": text}


def _round_summary(result: ChatResult, seconds: float) -> str:
    tokens = ""
    if result.input_tokens is not None or result.output_tokens is not None:
        tokens = f", токенов: вход {result.input_tokens or '?'}, выход {result.output_tokens or '?'}"
    calls = f"; просит инструменты: {', '.join(call.name for call in result.tool_calls)}" if result.tool_calls else ""
    return f"Модель ответила за {seconds:.1f} с{tokens}{calls}"


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
