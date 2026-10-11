"""Делегирование: основной агент поручает задачу саб-агенту и продолжает разговор с пользователем.

Задача идёт в дочерней сессии (parent_id) фоновой задачей: тот же граф, но со своим агентом,
его инструментами и без личной памяти пользователя. Подтверждения саб-агента становятся карточками
в боковой панели. Готовый результат приходит в сессию основного агента репликой от имени саб-агента —
основной агент учтёт его в следующем ходе.
"""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from local_agent.agent.errors import AgentRuntimeError
from local_agent.agent.registry import AgentRegistry
from local_agent.llm.base import LLMProviderError
from local_agent.memory.conversation import ConversationService
from local_agent.memory.models import Message
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService
from local_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_PARALLEL = 2
PARENT_WAIT_SECONDS = 1.0
TITLE_CHARS = 60
# Сколько саб-агент ждёт ответа на вопрос другому саб-агенту, включая подтверждения пользователя.
ASK_TIMEOUT_SECONDS = 30 * 60


class DelegationManager:
    def __init__(
        self, runtime, sessions: SessionService, conversation: ConversationService, agents: AgentRegistry,
        *, max_parallel: int = DEFAULT_PARALLEL, provider: str = "", model: str = "",
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._conversation = conversation
        self._agents = agents
        # Локальная модель всё равно обрабатывает запросы по очереди: больше двух задач только мешают друг другу.
        self._slots = asyncio.Semaphore(max_parallel)
        self._tasks: dict[str, asyncio.Task] = {}
        self._provider = provider
        self._model = model
        # Дочерние сессии, чей результат ждёт основной агент. Ответы на реплики самого пользователя
        # в разговоре с саб-агентом в основной чат не уходят.
        self._awaiting: set[str] = set()
        # Вопросы одного саб-агента другому (ask_agent): ответ ждёт инструмент, а не основной чат.
        self._questions: dict[str, asyncio.Future] = {}
        runtime.finished_hooks.append(self.on_finished)

    def delegate(self, parent: Session, agent_id: str, task: str) -> Session:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Саб-агента {agent_id} нет")
        title = _title(task)
        provider, model = (self._provider, self._model) if self._model else (parent.provider, parent.model)
        child = self._sessions.create(
            title=title, agent_id=agent.id, model=model, provider=provider or parent.provider,
            project_id=parent.project_id, parent_id=parent.id, hidden=True,
        )
        self._awaiting.add(child.id)
        self._sessions.set_task(child.id, agent_id=agent.id, agent_name=agent.name, title=title, status="queued")
        self._tasks[child.id] = asyncio.create_task(self._run(child.id, task), name=f"delegate:{child.id}")
        self._tasks[child.id].add_done_callback(lambda _: self._tasks.pop(child.id, None))
        return child

    def chat(self, parent: Session, agent_id: str) -> Session:
        """Разговор пользователя с саб-агентом: последняя его сессия у этой основной или новая."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Саб-агента {agent_id} нет")
        children = [
            item for item in self._sessions.list() if item.parent_id == parent.id and item.agent_id == agent_id
        ]
        if children:
            return max(children, key=lambda item: item.updated_at)
        provider, model = (self._provider, self._model) if self._model else (parent.provider, parent.model)
        return self._sessions.create(
            title=f"Разговор с агентом «{agent.name}»", agent_id=agent.id, model=model, provider=provider or parent.provider,
            project_id=parent.project_id, parent_id=parent.id, hidden=True,
        )

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, child_id: str, task: str) -> None:
        async with self._slots:
            self._sessions.set_task(child_id, status="running")
            try:
                async for event in self._runtime.stream_turn(child_id, task, f"delegate-{uuid4().hex[:8]}", sender="Основной агент"):
                    if event["type"] == "interrupt":
                        self._sessions.set_task(child_id, status="waiting")
            except (AgentRuntimeError, LLMProviderError) as exc:
                await self._fail(child_id, str(exc))
            except asyncio.CancelledError:
                self._sessions.set_task(child_id, status="failed", error="Остановлено")
                raise
            except Exception as exc:  # noqa: BLE001 — сбой фоновой задачи не должен пропасть молча
                logger.exception("Задача саб-агента упала session_id=%s", child_id)
                await self._fail(child_id, f"внутренняя ошибка: {type(exc).__name__}")

    async def ask(self, asker: Session, agent_id: str, question: str) -> str:
        """Вопрос саб-агента другому саб-агенту посреди задачи: разговор идёт в дочерней сессии спрашивающего,
        ответ возвращается ему же результатом инструмента. Подтверждения адресата — обычные карточки."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Саб-агента {agent_id} нет")
        if agent_id in self._chain(asker):
            raise ValueError(f"Саб-агент {agent_id} уже участвует в этой цепочке вопросов: так получился бы цикл")
        sender = self._agents.get(asker.agent_id)
        child = self._sessions.create(
            title=_title(question), agent_id=agent.id, model=asker.model, provider=asker.provider,
            project_id=asker.project_id, parent_id=asker.id, hidden=True,
        )
        self._sessions.set_task(child.id, agent_id=agent.id, agent_name=agent.name, title=_title(question), status="running")
        answer = asyncio.get_running_loop().create_future()
        self._questions[child.id] = answer
        try:
            async for event in self._runtime.stream_turn(
                child.id, question, f"ask-{uuid4().hex[:8]}", sender=sender.name if sender else asker.agent_id,
            ):
                if event["type"] == "interrupt":
                    self._sessions.set_task(child.id, status="waiting")
            # Ответ приходит через on_finished: сразу или после решения пользователя по карточке.
            text = await asyncio.wait_for(answer, ASK_TIMEOUT_SECONDS)
        except (AgentRuntimeError, LLMProviderError) as exc:
            self._sessions.set_task(child.id, status="failed", error=str(exc))
            raise ValueError(f"{agent.name} не ответил: {exc}") from exc
        except TimeoutError as exc:
            self._sessions.set_task(child.id, status="failed", error="Не дождались ответа")
            raise ValueError(f"{agent.name} не ответил за {ASK_TIMEOUT_SECONDS // 60} минут") from exc
        finally:
            self._questions.pop(child.id, None)
        self._sessions.set_task(child.id, status="done", error=None)
        return text

    def _chain(self, session: Session) -> set[str]:
        """Агенты по цепочке сессий вверх от спрашивающего: им вопрос задавать нельзя."""
        agents = set()
        current: Session | None = session
        while current is not None:
            agents.add(current.agent_id)
            current = self._sessions.get(current.parent_id) if current.parent_id else None
        return agents

    async def on_finished(self, session: Session, answer: Message) -> None:
        """Ход дочерней сессии закончился ответом: передаём результат основному агенту."""
        if (question := self._questions.get(session.id)) is not None:
            if not question.done():
                question.set_result(answer.content)
            return
        if not session.parent_id or not session.task or session.id not in self._awaiting:
            return
        self._awaiting.discard(session.id)
        self._sessions.set_task(session.id, status="done", error=None)
        task = session.task
        await self._report(session.parent_id, task.get("agent_name") or session.agent_id,
                           f"Результат задачи «{task.get('title', '')}» от саб-агента {task.get('agent_name')}:\n\n{answer.content}")

    async def _fail(self, child_id: str, error: str) -> None:
        self._awaiting.discard(child_id)
        child = self._sessions.get(child_id)
        self._sessions.set_task(child_id, status="failed", error=error)
        if child and child.parent_id and child.task:
            await self._report(child.parent_id, child.task.get("agent_name") or child.agent_id,
                               f"Задача «{child.task.get('title', '')}» не выполнена: {error}")

    async def _report(self, parent_id: str, sender: str, content: str) -> None:
        # Посреди хода основного агента реплика встала бы между вызовом инструмента и его результатом.
        while self._runtime.is_busy(parent_id):
            await asyncio.sleep(PARENT_WAIT_SECONDS)
        if self._sessions.get(parent_id) is not None:
            await asyncio.to_thread(self._conversation.add_user_message, parent_id, content, sender)


class DelegateTool:
    id = "delegate"
    name = "Поручить саб-агенту"
    requires_approval = False

    def __init__(self, manager: DelegationManager, agents: AgentRegistry, parent: Session) -> None:
        self._manager = manager
        self._agents = agents
        self._parent = parent

    @property
    def description(self) -> str:
        return (
            "Поручить задачу саб-агенту. Он работает в фоне, ты продолжаешь разговор; результат придёт "
            "отдельным сообщением. Передай задачу целиком: что сделать, пути, условия, формат результата"
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": [agent.id for agent in self._agents.all()]},
                "task": {"type": "string", "description": "Задача целиком"},
            },
            "required": ["agent", "task"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        agent, task = arguments.get("agent"), arguments.get("task")
        if not isinstance(agent, str) or not isinstance(task, str) or not task.strip():
            return ToolResult("Нужны agent и task", is_error=True)
        try:
            child = self._manager.delegate(self._parent, agent, task.strip())
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(
            f"Задача поручена {agent} (задача {child.id[:8]}). Скажи пользователю, что работа идёт в фоне "
            "и результат придёт сообщением; не жди его и не выдумывай результат."
        )


class AskAgentTool:
    """Саб-агент спрашивает другого саб-агента, с которым связан на холсте, и ждёт ответа."""

    id = "ask_agent"
    name = "Спросить саб-агента"
    requires_approval = False

    def __init__(self, manager: DelegationManager, agents: AgentRegistry, asker: Session, targets: tuple[str, ...]) -> None:
        self._manager = manager
        self._agents = agents
        self._asker = asker
        self._targets = targets

    @property
    def description(self) -> str:
        known = [agent for agent in (self._agents.get(item) for item in self._targets) if agent]
        return (
            "Спросить другого саб-агента и дождаться его ответа. Передай вопрос целиком: что нужно, пути, контекст. "
            "Кого можно спросить: " + "; ".join(f"{agent.id} — {agent.description or agent.name}" for agent in known)
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": list(self._targets)},
                "question": {"type": "string", "description": "Вопрос или поручение целиком"},
            },
            "required": ["agent", "question"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        agent, question = arguments.get("agent"), arguments.get("question")
        if agent not in self._targets or not isinstance(question, str) or not question.strip():
            return ToolResult(f"Нужны agent (один из: {', '.join(self._targets)}) и question", is_error=True)
        try:
            text = await self._manager.ask(self._asker, agent, question.strip())
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Ответ {agent}:\n\n{text}")


def _title(text: str) -> str:
    title = " ".join(text.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"
