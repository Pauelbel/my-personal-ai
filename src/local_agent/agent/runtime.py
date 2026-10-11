"""Runtime ведёт ход агента по графу LangGraph: собирает контекст, запускает граф и отдаёт события в UI.

Ход может остановиться на подтверждении (interrupt). Тогда поток заканчивается событием interrupt,
а то, что ждёт решения, хранится в сессии и в чекпоинте графа: решение можно принять и после
перезагрузки страницы или сервера. Новое сообщение пользователя в такой сессии — ответ на карточку.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from langgraph.types import Command

from local_agent.agent.context import build_context, build_system_prompt, estimate_message_tokens, estimate_tokens
from local_agent.agent.errors import (
    AgentRuntimeError,
    NothingPendingError,
    SessionBusyError,
    SessionNotFoundError,
)
from local_agent.agent.main_agent import MAIN_AGENT_ID, main_agent
from local_agent.agent.models import Agent
from local_agent.agent.project_instructions import load_project_instructions
from local_agent.agent.registry import AgentRegistry, SkillRegistry
from local_agent.agent.summary import ConversationSummarizer
from local_agent.capabilities.toolbox import DEFAULT_ACTIVE_TOOLS, ActivateTool, Toolbox
from local_agent.graph.agent_graph import CATALOG_MARKER, GraphDeps, build_agent_graph, from_chat, log
from local_agent.llm.base import context_length
from local_agent.llm.registry import LLMRegistry
from local_agent.memory.conversation import ConversationService
from local_agent.memory.models import Message
from local_agent.memory.service import MemoryService, MemoryServiceError
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService
from local_agent.tools.base import Tool, summary_line
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.skills import UseSkillTool

logger = logging.getLogger(__name__)

# activate занимает раунд, поэтому основному агенту лимит выше, чем раньше.
MAX_TOOL_ROUNDS = 10

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


# Базовые инструменты, которые добавляют другие части приложения: память, вики, предложения, делегирование.
# Получают сессию, её агента и набор хода; возвращают инструменты, подключённые всегда.
CoreTools = Callable[[Session, Agent, Toolbox], list[Tool]]
# Вызывается, когда ход сессии закончился ответом: так результат саб-агента возвращается основному.
FinishedHook = Callable[[Session, Message], Awaitable[None]]


class AgentRuntime:
    def __init__(
        self,
        sessions: SessionService,
        conversation: ConversationService,
        agents: AgentRegistry,
        providers: LLMRegistry,
        memory: MemoryService,
        tools: ToolRegistry,
        skills: SkillRegistry,
        summarizer: ConversationSummarizer,
        limits: RuntimeLimits,
        checkpointer=None,
    ) -> None:
        self._sessions = sessions
        self._conversation = conversation
        self._agents = agents
        self._providers = providers
        self._memory = memory
        self._tools = tools
        self._skills = skills
        self._summarizer = summarizer
        self._limits = limits
        self._checkpointer = checkpointer
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._background: dict[str, asyncio.Task] = {}
        self.core_tools: list[CoreTools] = []
        self.finished_hooks: list[FinishedHook] = []
        # Архив возможностей для реестра основного агента; задаёт приложение.
        self.archive_lines: Callable[[], list[str]] | None = None
        self._graph = build_agent_graph(
            GraphDeps(sessions, conversation, providers, self.toolbox, self.catalog_text), checkpointer,
        )

    # ---------- агенты и инструменты ----------

    def agent_for(self, session: Session) -> Agent:
        """Основная сессия всегда за основным агентом; дочерняя — за саб-агентом, которому поручили задачу."""
        if not session.parent_id:
            return main_agent(session.provider)
        agent = self._agents.get(session.agent_id)
        if agent is None:
            raise AgentRuntimeError(f"Саб-агент «{session.agent_id}» не найден: возможно, он в архиве")
        return agent

    def toolbox(self, session: Session) -> Toolbox:
        agent = self.agent_for(session)
        sub_agent = agent.id != MAIN_AGENT_ID
        project = self._sessions.project_of(session)
        registry_tools = self._tools.all() if session.workspace else []
        if project is not None and project.mode == "read":
            # Проект только для чтения: всё, что меняет файлы или систему, агенту даже не показываем.
            registry_tools = [tool for tool in registry_tools if not tool.requires_approval]
        if sub_agent:
            registry_tools = [tool for tool in registry_tools if tool.id in agent.tools]
        active = project.active_tools if project is not None and project.active_tools is not None else DEFAULT_ACTIVE_TOOLS
        box = Toolbox(
            core=[], catalog=registry_tools, active=list(active),
            on_active_change=lambda active: self._sessions.set_active_tools(session.id, active),
            all_active=sub_agent,
        )
        if not sub_agent:
            box.core.append(ActivateTool(box))
        skill_tool = UseSkillTool(
            self._skills, {tool.id for tool in self._tools.all()}, {tool.id for tool in registry_tools},
            agent.skills if sub_agent else None,
        )
        if skill_tool.skills():
            box.core.append(skill_tool)
        for build in self.core_tools:
            for tool in build(session, agent, box):
                # Редкие инструменты основного агента (поиск в памяти, архив) лежат в реестре одной строкой.
                (box.catalog if getattr(tool, "catalog", False) else box.core).append(tool)
        box.__post_init__()
        return box

    def catalog_text(self, session: Session, box: Toolbox) -> str:
        """Компактный реестр: имя и одна строка описания, полные схемы подключаются по запросу."""
        agent = self.agent_for(session)
        sub_agent = agent.id != MAIN_AGENT_ID
        skills = [
            skill for skill in self._skills.all()
            if not sub_agent or agent.skills is None or skill.id in agent.skills
        ]
        parts = ["Реестр возможностей (имя — что делает)."]
        project = self._sessions.project_of(session)
        if project is not None and project.mode == "read":
            parts.append(
                "Проект открыт только для чтения: инструменты, меняющие файлы или систему, недоступны. "
                "Если без них не обойтись, скажи пользователю, что нужно переключить проект в полный режим."
            )
        tools = box.catalog_lines()
        if tools:
            parts.append(
                ("Твои инструменты:" if sub_agent else "Инструменты (вызывай подключённые, остальные сначала подключи через activate):")
                + "\n" + "\n".join(tools)
            )
        else:
            parts.append("Инструментов для работы с файлами нет.")
        parts.append(
            "Скиллы — инструкции под тип задач, загружаются через use_skill:\n"
            + "\n".join(f"- {skill.id} — {summary_line(skill.description)}" for skill in skills)
            if skills else "Скиллов пока нет."
        )
        if not sub_agent:
            agents = self._agents.all()
            parts.append(
                "Саб-агенты — помощники с постоянной ролью, задачу поручают через delegate:\n"
                + "\n".join(f"- {item.id} — {summary_line(item.description or item.name)}" for item in agents)
                if agents else "Саб-агентов пока нет."
            )
        if not sub_agent and self.archive_lines is not None and (archived := self.archive_lines()):
            parts.append("В архиве (вернуть можно через archive с action restore, с утверждением пользователя):\n" + "\n".join(archived))
        return "\n\n".join(parts)

    # ---------- ход ----------

    def is_busy(self, session_id: str) -> bool:
        lock = self._session_locks.get(session_id)
        return lock is not None and lock.locked()

    async def run_turn(self, session_id: str, content: str, request_id: str) -> Message:
        """Ход без стриминга: подтверждать некому, поэтому все вызовы с подтверждением отклоняются."""
        events = self.stream_turn(session_id, content, request_id)
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
            events = self.resume(session_id, {"action": "reject"}, request_id)

    async def stream_turn(
        self, session_id: str, content: str, request_id: str, sender: str | None = None, *, retry: bool = False,
    ) -> AsyncIterator[dict]:
        """sender — кто написал реплику, если не пользователь (например, основной агент саб-агенту);
        retry — повтор после сбоя: та же реплика без ответа не сохраняется второй раз."""
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
            async for event in self._start(session, content, request_id, sender, retry):
                yield event

    async def resume(self, session_id: str, decision: object, request_id: str) -> AsyncIterator[dict]:
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
        self, session: Session, content: str, request_id: str, sender: str | None, retry: bool = False,
    ) -> AsyncIterator[dict]:
        agent = self.agent_for(session)
        provider_id = session.provider or agent.llm_provider
        provider = self._providers.get(provider_id)
        if provider is None:
            raise AgentRuntimeError("Провайдер LLM не найден")
        model = session.model or agent.model
        if not model:
            raise AgentRuntimeError("Выберите модель для этой сессии")

        last = await asyncio.to_thread(self._conversation.recent, session.id, 1)
        if retry and last and last[-1].role == "user" and last[-1].content == content:
            user_message = last[-1]
        else:
            user_message = await asyncio.to_thread(self._conversation.add_user_message, session.id, content, sender)
        if sender is None and not session.parent_id:
            self._sessions.name_from_first_message(session.id, content, self._conversation.count(session.id))
        yield {"type": "user_message", "message": user_message}

        box = self.toolbox(session)
        definitions = box.definitions()
        loaded_window = await context_length(provider, model)
        window = loaded_window or self._limits.default_context_tokens
        budget = max(window - self._limits.response_reserve_tokens, 0)
        history = await asyncio.to_thread(self._conversation.recent, session.id, self._limits.max_context_messages * 10)
        # Саб-агент работает на основного агента, а не на пользователя: личная память в его сессию не попадает.
        memory_context = "" if session.parent_id else await asyncio.to_thread(self._memory.prompt_context, content)
        project_context = await asyncio.to_thread(
            load_project_instructions, session.workspace, max_bytes=budget // 4,
        )
        # Бюджет окна считаем по настоящему реестру, а в состояние кладём метку: граф подставит свежий.
        catalog = self.catalog_text(session, box)
        context = build_context(
            build_system_prompt(
                agent.system_prompt, memory_context, session.summary, today=date.today(),
                capabilities=CATALOG_MARKER, project_context=project_context,
            ),
            history,
            max_messages=self._limits.max_context_messages,
            max_tokens=budget - estimate_tokens(catalog),
            tools=definitions,
        )
        used = sum(estimate_message_tokens(message) for message in context.messages) + estimate_tokens(catalog)
        yield log(
            f"Контекст: {context.included_count} из {len(history)} сообщений истории, "
            f"≈{used} из {window} токенов окна{'' if loaded_window else ' (по умолчанию)'}"
        )
        extras = [name for name, present in (
            ("инструкции проекта", project_context), ("память", memory_context), ("резюме", session.summary)
        ) if present]
        yield log(f"В системный промпт добавлено: {', '.join(extras)}" if extras else "Дополнительный контекст не подключён")

        thread = f"{session.id}:{uuid4().hex}"
        # Ход идёт: если сервер упадёт посреди графа, следующий ход начнётся с чистого листа.
        self._sessions.set_pending(session.id, thread, None)
        state = {
            "session_id": session.id, "request_id": request_id, "provider_id": provider_id, "model": model,
            "window": window, "rounds": 0, "max_rounds": agent.max_tool_rounds or MAX_TOOL_ROUNDS,
            "messages": [from_chat(message) for message in context.messages],
            "decisions": {}, "answer": None,
            "included": context.included_count, "history_count": self._conversation.count(session.id),
        }
        async for event in self._drive(session, thread, state):
            yield event

    async def _drive(self, session: Session, thread: str, payload: object) -> AsyncIterator[dict]:
        """Прогоняет граф до ответа или до interrupt и переводит его события в события UI."""
        config = {"configurable": {"thread_id": thread}, "recursion_limit": 3 * MAX_TOOL_ROUNDS + 50}
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
        for hook in self.finished_hooks:
            self._spawn(f"finished:{session.id}:{answer.id}", hook(current, answer))

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
        if self._conversation.count(session.id) > included_count:
            self._spawn(f"summary:{session.id}", self._summarizer.update(session.id, included_count, model))
        threshold = self._limits.memory_auto_update_messages
        if not session.parent_id and threshold and self._memory.pending_user_messages(session.id) >= threshold:
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
