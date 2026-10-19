"""Runtime ведёт ход агента по графу LangGraph: собирает контекст, запускает граф и отдаёт события в UI.

Ход может остановиться на подтверждении (interrupt). Тогда поток заканчивается событием interrupt,
а то, что ждёт решения, хранится в сессии и в чекпоинте графа: решение можно принять и после
перезагрузки страницы или сервера. Новое сообщение пользователя в такой сессии — ответ на карточку.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from langgraph.types import Command

from agents.main import main_agent
from agents.models import Agent
from agents.wiki import wiki_agent
from core.context import (
    browser_state,
    build_context,
    build_system_prompt,
    estimate_message_tokens,
    estimate_tokens,
    stash_paste,
)
from core.delegation import DelegationStore
from core.errors import AgentRuntimeError, NothingPendingError, SessionBusyError, SessionNotFoundError
from core.graph import CATALOG_MARKER, STOPPED_TEXT, GraphDeps, build_agent_graph, from_chat, log
from core.project_instructions import load_project_instructions
from core.registry import ToolRegistry
from core.summary import ConversationSummarizer
from core.tool import Tool
from core.toolbox import DEFAULT_ACTIVE_TOOLS, ActivateTool, Toolbox
from llm.base import context_length
from llm.registry import LLMRegistry
from memory.conversation import ConversationService
from memory.models import Message
from memory.service import MemoryService, MemoryServiceError
from sessions.models import Session
from sessions.service import SessionService

logger = logging.getLogger(__name__)

__all__ = [
    "AgentRuntime", "AgentRuntimeError", "NothingPendingError", "RuntimeLimits",
    "SessionBusyError", "SessionNotFoundError",
]


@dataclass(frozen=True)
class RuntimeLimits:
    max_context_messages: int
    default_context_tokens: int
    response_reserve_tokens: int
    memory_auto_update_messages: int
    # Сколько последних реплик пользователя (с ответами на них) помнит чат вики; всё раньше забывается.
    wiki_context_messages: int = 3
    # Раундов вызова инструментов за ход; дальше — карточка «продолжить или остановить».
    max_tool_rounds: int = 40


# Базовые инструменты, которые добавляют другие части приложения: память, вики, предложения, шаги процессов агентов.
# Получают сессию, её агента и набор хода; возвращают инструменты, подключённые всегда.
CoreTools = Callable[[Session, Agent, Toolbox], list[Tool]]


class AgentRuntime:
    def __init__(
        self,
        sessions: SessionService,
        conversation: ConversationService,
        providers: LLMRegistry,
        memory: MemoryService,
        tools: ToolRegistry,
        summarizer: ConversationSummarizer,
        limits: RuntimeLimits,
        checkpointer=None,
        delegations: DelegationStore | None = None,
    ) -> None:
        self._sessions = sessions
        self._conversation = conversation
        self._providers = providers
        self._memory = memory
        self._tools = tools
        self._summarizer = summarizer
        self._limits = limits
        self._checkpointer = checkpointer
        self._delegations = delegations
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._background: dict[str, asyncio.Task] = {}
        self.core_tools: list[CoreTools] = []
        # Дополнительные инструкции основного агента: читаются на каждом ходе, правка действует сразу.
        self.main_instructions: Callable[[], str] = lambda: ""
        # Архив возможностей для реестра основного агента; задаёт приложение.
        self.archive_lines: Callable[[], list[str]] | None = None
        self.agent_lines: Callable[[], list[str]] | None = None
        # Имена секретов (без значений): основной агент указывает их в secrets, когда предлагает инструмент.
        self.secret_names: Callable[[], list[str]] | None = None
        # Файл картинки из реплики по её имени (data/attachments); задаёт приложение.
        self.attachment_path: Callable[[str], Path | None] = lambda name: None
        self._graph = build_agent_graph(
            GraphDeps(sessions, conversation, providers, self.toolbox, self.catalog_text, delegations), checkpointer,
        )

    # ---------- агенты и инструменты ----------

    def agent_for(self, session: Session) -> Agent:
        """Сессии проектов — за основным агентом, чат вики — за агентом вики."""
        if session.kind == "wiki":
            return wiki_agent(session.provider)
        return main_agent(session.provider, self.main_instructions())

    def toolbox(self, session: Session) -> Toolbox:
        agent = self.agent_for(session)
        project = self._sessions.project_of(session)
        registry_tools = self._tools.all() if session.workspace else []
        if project is not None and project.mode == "read":
            # Проект только для чтения: всё, что меняет файлы или систему, агенту даже не показываем.
            registry_tools = [tool for tool in registry_tools if not tool.requires_approval]
        active = project.active_tools if project is not None and project.active_tools is not None else DEFAULT_ACTIVE_TOOLS
        box = Toolbox(
            core=[], catalog=registry_tools, active=list(active),
            on_active_change=lambda active: self._sessions.set_active_tools(session.id, active),
        )
        for build in self.core_tools:
            for tool in build(session, agent, box):
                # Редкие инструменты основного агента (поиск в памяти, архив) лежат в реестре одной строкой.
                (box.catalog if getattr(tool, "catalog", False) else box.core).append(tool)
        if box.catalog:
            # У чата вики реестра нет: все его инструменты подключены сразу, activate ему не нужен.
            box.core.insert(0, ActivateTool(box))
        box.__post_init__()
        return box

    def catalog_text(self, session: Session, box: Toolbox) -> str:
        """Компактный реестр: имя и одна строка описания, полные схемы подключаются по запросу."""
        if session.kind == "wiki":
            return "Все инструменты вики подключены сразу. Файлов проекта и памяти о пользователе здесь нет."
        parts = ["Реестр возможностей (имя — что делает)."]
        if self.agent_lines is not None and (agents := self.agent_lines()):
            parts.append(
                "Специализированные агенты — отдельные исполнители со своим контекстом. Подходящую законченную "
                "задачу передавай через delegate_agent (сначала подключи его через activate):\n" + "\n".join(agents)
            )
        project = self._sessions.project_of(session)
        if project is not None and project.mode == "read":
            parts.append(
                "Проект открыт только для чтения: инструменты, меняющие файлы или систему, недоступны. "
                "Если без них не обойтись, скажи пользователю, что нужно переключить проект в полный режим."
            )
        tools = box.catalog_lines()
        if tools:
            parts.append(
                "Инструменты (вызывай подключённые, остальные сначала подключи через activate):\n" + "\n".join(tools)
            )
        else:
            parts.append("Инструментов для работы с файлами нет.")
        if self.secret_names is not None and (names := self.secret_names()):
            parts.append(
                "Доступы, которые пользователь уже задал (значения скрыты; скрипт инструмента получает их из os.environ, если перечислить "
                f"в secrets при propose): {', '.join(names)}"
            )
        if self.archive_lines is not None and (archived := self.archive_lines()):
            parts.append("В архиве (вернуть можно через archive с action restore, с утверждением пользователя):\n" + "\n".join(archived))
        return "\n\n".join(parts)

    # ---------- ход ----------

    def is_busy(self, session_id: str) -> bool:
        lock = self._session_locks.get(session_id)
        return lock is not None and lock.locked()

    def mark_stopped(self, session_id: str) -> None:
        """«Стоп» посреди хода: если итога нет (модель ещё думала или шли инструменты), ход в чате подписывается
        «ход остановлен» — иначе под репликой пользователя пусто, и непонятно, что случилось."""
        if self._delegations:
            self._delegations.stop(session_id)
        last = self._conversation.recent(session_id, 1)
        if last and last[-1].role == "assistant" and not last[-1].tool_calls:
            return
        self._conversation.add_assistant_message(session_id, STOPPED_TEXT)

    async def run_turn(
        self, session_id: str, content: str, request_id: str, attachments: tuple[str, ...] = (),
    ) -> Message:
        """Ход без стриминга: подтверждать некому, поэтому все вызовы с подтверждением отклоняются."""
        events = self.stream_turn(session_id, content, request_id, attachments=attachments)
        while True:
            answer: Message | None = None
            pending = False
            async for event in events:
                if event["type"] == "done":
                    answer = event["message"]
                elif event["type"] == "interrupt":
                    pending = True
            if answer is not None:
                return answer
            if not pending:
                raise AgentRuntimeError("Модель не вернула ответ")
            events = self.resume(session_id, {"action": "reject"})

    async def stream_turn(
        self, session_id: str, content: str, request_id: str, *, retry: bool = False,
        attachments: tuple[str, ...] = (),
    ) -> AsyncIterator[dict]:
        """retry — повтор после сбоя: та же реплика без ответа не сохраняется второй раз;
        attachments — имена картинок, которые пользователь вставил в реплику."""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionBusyError("В этой сессии ещё выполняется предыдущий ход")
        async with lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError("Сессия не найдена")
            if session.pending_thread and session.pending:
                # Ход ждёт решения по карточке: новое сообщение — ответ на неё.
                async for event in self._drive(session, session.pending_thread, Command(resume={
                    "action": "reply", "text": content,
                })):
                    yield event
                return
            if session.pending_thread:
                # Прошлый ход оборвался посреди графа (сбой или остановка сервера): начинаем с чистого листа.
                await self._forget(session.id, session.pending_thread)
            async for event in self._start(session, content, request_id, retry, tuple(attachments)):
                yield event

    async def resume(self, session_id: str, decision: object) -> AsyncIterator[dict]:
        """Продолжает ход, остановленный на подтверждении: approve, reject или reply с текстом."""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionBusyError("В этой сессии ещё выполняется предыдущий ход")
        async with lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError("Сессия не найдена")
            if not session.pending_thread or not session.pending:
                raise NothingPendingError("Ход уже не ждёт решения")
            async for event in self._drive(session, session.pending_thread, Command(resume=decision)):
                yield event

    async def close(self) -> None:
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _start(
        self, session: Session, content: str, request_id: str, retry: bool = False, attachments: tuple[str, ...] = (),
    ) -> AsyncIterator[dict]:
        agent = self.agent_for(session)
        provider_id = session.provider or agent.llm_provider
        provider = self._providers.get(provider_id)
        if provider is None:
            raise AgentRuntimeError("Провайдер LLM не найден")
        model = session.model or agent.model
        if not model:
            raise AgentRuntimeError("Выберите модель для этой сессии")
        loaded_window = await context_length(provider, model)
        window = loaded_window or self._limits.default_context_tokens
        budget = max(window - self._limits.response_reserve_tokens, 0)
        stashed = None
        if session.kind == "chat" and session.workspace:
            # Огромная вставка — в файл проекта: в разговоре и в окне модели остаются её края и путь.
            stashed = await asyncio.to_thread(stash_paste, Path(session.workspace), content, budget)
            if stashed is not None:
                content = stashed[0]

        last = await asyncio.to_thread(self._conversation.recent, session.id, 1)
        if retry and last and last[-1].role == "user" and last[-1].content == content:
            user_message = last[-1]
        else:
            user_message = await asyncio.to_thread(
                self._conversation.add_user_message, session.id, content, attachments,
            )
        wiki = session.kind == "wiki"
        if session.kind == "chat":
            title = content or "Картинка"
            self._sessions.name_from_first_message(session.id, title, self._conversation.count(session.id))
        yield {"type": "user_message", "message": user_message}
        if stashed is not None:
            yield log(f"Большой текст сохранён в файл {stashed[1]} — модель получит его начало, конец и путь к файлу")

        box = self.toolbox(session)
        definitions = box.definitions()
        history = await asyncio.to_thread(self._conversation.recent, session.id, self._limits.max_context_messages * 10)
        if wiki:
            # Чат вики — череда независимых просьб: модель видит только хвост разговора, без резюме,
            # иначе старые тексты и ответы путают её на новой задаче.
            history = last_user_turns(history, self._limits.wiki_context_messages)
        # Вики — не про пользователя: личная память в её чат не попадает.
        project_key = session.project_id or session.workspace or ""
        memory_context = await asyncio.to_thread(
            self._memory.prompt_context, content, project_key,
        ) if _with_user(session) else ""
        summary = "" if wiki else session.summary
        project_context = await asyncio.to_thread(
            load_project_instructions, session.workspace, max_bytes=budget // 4,
        )
        # Бюджет окна считаем по настоящему реестру, а в состояние кладём метку: граф подставит свежий.
        catalog = self.catalog_text(session, box)
        context = build_context(
            build_system_prompt(
                agent.system_prompt, memory_context, summary, today=date.today(),
                capabilities=CATALOG_MARKER, project_context=project_context, browser=browser_state(history),
            ),
            history,
            max_messages=self._limits.max_context_messages,
            max_tokens=budget - estimate_tokens(catalog),
            tools=definitions,
            images=self.attachment_path,
        )
        used = sum(estimate_message_tokens(message) for message in context.messages) + estimate_tokens(catalog)
        yield log(
            f"Контекст: {context.included_count} из {len(history)} сообщений истории, "
            f"≈{used} из {window} токенов окна{'' if loaded_window else ' (по умолчанию)'}"
        )
        extras = [name for name, present in (
            ("инструкции проекта", project_context), ("память", memory_context), ("резюме", summary)
        ) if present]
        yield log(f"В системный промпт добавлено: {', '.join(extras)}" if extras else "Дополнительный контекст не подключён")

        thread = f"{session.id}:{uuid4().hex}"
        # Ход идёт: если сервер упадёт посреди графа, следующий ход начнётся с чистого листа.
        self._sessions.set_pending(session.id, thread, None)
        state = {
            "session_id": session.id, "request_id": request_id, "provider_id": provider_id, "model": model,
            "window": window, "budget": budget, "rounds": 0,
            "max_rounds": self._limits.max_tool_rounds,
            "round_step": self._limits.max_tool_rounds,
            "messages": [from_chat(message) for message in context.messages],
            "decisions": {}, "answer": None,
            "included": context.included_count, "history_count": self._conversation.count(session.id),
            "delegation": self._delegations.active(session.id) if self._delegations else None,
        }
        async for event in self._drive(session, thread, state):
            yield event

    async def _drive(self, session: Session, thread: str, payload: object) -> AsyncIterator[dict]:
        """Прогоняет граф до ответа или до interrupt и переводит его события в события UI."""
        # Узлов графа на раунд — три (модель, подтверждения, инструменты); за один запуск графа — не больше
        # лимита раундов: дальше ход встаёт на карточку «продолжить или остановить».
        config = {"configurable": {"thread_id": thread}, "recursion_limit": 3 * self._limits.max_tool_rounds + 50}
        interrupted = None
        answer: Message | None = None
        try:
            async for mode, chunk in self._graph.astream(payload, config, stream_mode=["custom", "updates"]):
                if mode == "custom":
                    if chunk.get("type") == "done":
                        answer = chunk["message"]
                    yield chunk
                elif isinstance(chunk, dict) and "__interrupt__" in chunk:
                    interrupted = chunk["__interrupt__"][0]
        except BaseException:
            # «Стоп», закрытая вкладка или ошибка: ход прерван целиком, ждать в нём больше нечего.
            self._sessions.set_pending(session.id, None, None)
            self._spawn(f"forget:{thread}", self._delete_thread(thread))
            raise
        if interrupted is not None:
            pending = {**interrupted.value, "interrupt_id": interrupted.id}
            self._sessions.set_pending(session.id, thread, pending)
            yield {"type": "interrupt", "pending": pending}
            return
        values = await self._thread_values(thread)
        await self._forget(session.id, thread)
        if answer is None:
            return
        included = values.get("included", 0) + max(
            self._conversation.count(session.id) - values.get("history_count", 0), 0,
        )
        current = self._sessions.get(session.id) or session
        self._after_turn(current, values.get("model") or current.model, included)

    async def _thread_values(self, thread: str) -> dict:
        if self._checkpointer is None:
            return {}
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": thread}})
        return dict(snapshot.values or {})

    async def _forget(self, session_id: str, thread: str) -> None:
        self._sessions.set_pending(session_id, None, None)
        await self._delete_thread(thread)

    async def _delete_thread(self, thread: str) -> None:
        if self._checkpointer is not None:
            await self._checkpointer.adelete_thread(thread)

    # ---------- после хода ----------

    def _after_turn(self, session: Session, model: str, included_count: int) -> None:
        # Чат вики забывает старое намеренно: резюме выпавших сообщений ему не нужно.
        if session.kind == "chat" and self._conversation.count(session.id) > included_count:
            self._spawn(f"summary:{session.id}", self._summarizer.update(session.id, included_count, model))
        threshold = self._limits.memory_auto_update_messages
        # Память — о пользователе: из чата вики её не пополняем.
        if _with_user(session) and threshold and self._memory.pending_user_messages(session.id) >= threshold:
            self._spawn(f"memory:{session.id}", self._update_memory(session.id))

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


def last_user_turns(history: list[Message], count: int) -> list[Message]:
    """Хвост истории с последних count реплик пользователя: всё, что раньше, модель не видит."""
    starts = [index for index, message in enumerate(history) if message.role == "user"]
    return history[starts[-count]:] if len(starts) > count else history


def _with_user(session: Session) -> bool:
    """Разговор пользователя с основным агентом — сессия проекта: только в ней нужна и пополняется
    личная память. Вики — не про пользователя."""
    return session.kind == "chat"
