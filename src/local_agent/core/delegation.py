"""Делегирование: основной агент поручает задачу саб-агенту и продолжает разговор с пользователем.

У саб-агента в сессии один разговор — дочерняя сессия (parent_id): в неё идут и поручения основного агента,
и реплики пользователя, поэтому агент помнит, что делал раньше в этой сессии. Поручения выполняются по очереди
фоновой задачей: тот же граф, но со своим агентом, его инструментами и без личной памяти пользователя. Подтверждения саб-агента становятся карточками
в боковой панели. Готовый результат приходит в сессию основного агента репликой от имени саб-агента —
основной агент учтёт его в следующем ходе.
"""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from local_agent.agent.errors import AgentRuntimeError, SessionBusyError
from local_agent.agent.main_agent import MAIN_AGENT_ID, MAIN_AGENT_NAME
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
# Как часто очередное поручение проверяет, освободился ли разговор агента.
QUEUE_WAIT_SECONDS = 0.5
# Итог хода передаёт фоновый хук сразу после ответа; если за это время его нет — ход остановили.
FINISH_GRACE_SECONDS = 3.0
TITLE_CHARS = 60
# Сколько саб-агент ждёт ответа на вопрос другому саб-агенту, включая подтверждения пользователя.
ASK_TIMEOUT_SECONDS = 30 * 60
# Так подписаны поручения основного агента в разговорах саб-агентов.
DELEGATOR = "Основной агент"
# Самая длинная цепочка обращений «агент ждёт агента»: дальше маленькая модель теряет нить, а задачи — лавиной.
MAX_CHAIN = 3


class DelegationManager:
    def __init__(
        self, runtime, sessions: SessionService, conversation: ConversationService, agents: AgentRegistry,
        *, max_parallel: int = DEFAULT_PARALLEL, provider: str = "", model: str = "", turns=None,
    ) -> None:
        self._runtime = runtime
        # Через хаб ходов результат поручения запускает ход основного агента — его видно в чате, как обычный.
        self._turns = turns
        self._sessions = sessions
        self._conversation = conversation
        self._agents = agents
        # Локальная модель всё равно обрабатывает запросы по очереди: больше двух задач только мешают друг другу.
        self._slots = asyncio.Semaphore(max_parallel)
        self._tasks: dict[str, asyncio.Task] = {}
        # Поручения одному агенту в одной сессии идут по очереди: у разговора одна очередь.
        self._queues: dict[str, asyncio.Lock] = {}
        self._provider = provider
        self._model = model
        # Дочерние сессии, чей результат ждёт основной агент. Ответы на реплики самого пользователя
        # в разговоре с саб-агентом в основной чат не уходят.
        self._awaiting: set[str] = set()
        # Вопросы одного саб-агента другому (ask_agent): ответ ждёт инструмент, а не основной чат.
        self._questions: dict[str, asyncio.Future] = {}
        # Кто ждёт ответа разговора: разговор адресата → разговор спросившего. По этим ссылкам видна цепочка.
        self._asked_by: dict[str, str] = {}
        runtime.finished_hooks.append(self.on_finished)

    def delegate(self, parent: Session, agent_id: str, task: str) -> Session:
        """Поручение уходит в разговор агента в этой сессии; агент, которого там не было, добавляется в неё."""
        child = self.chat(parent, agent_id)
        agent = self._agents.get(agent_id)
        title = _title(task)
        if not self.busy(child.id):
            self._sessions.set_task(
                child.id, agent_id=agent.id, agent_name=agent.name, title=title, status="queued", error=None,
                from_name=MAIN_AGENT_NAME,
            )
        key = f"{child.id}:{uuid4().hex[:8]}"
        self._tasks[key] = asyncio.create_task(self._run(child.id, task), name=f"delegate:{key}")
        self._tasks[key].add_done_callback(lambda _: self._tasks.pop(key, None))
        return child

    def member(self, parent: Session, agent_id: str) -> Session | None:
        """Разговор агента в сессии; если их несколько (так было до одного разговора на агента) — последний."""
        children = [
            item for item in self._sessions.list()
            if item.parent_id == parent.id and item.agent_id == agent_id and item.kind == "chat"
        ]
        return max(children, key=lambda item: item.updated_at) if children else None

    def chat(self, parent: Session, agent_id: str) -> Session:
        """Разговор с саб-агентом в сессии: существующий или новый — так агент добавляется в сессию."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Саб-агента {agent_id} нет")
        if (child := self.member(parent, agent_id)) is not None:
            return child
        provider, model = (self._provider, self._model) if self._model else (parent.provider, parent.model)
        return self._sessions.create(
            title=f"Разговор с агентом «{agent.name}»", agent_id=agent.id, model=model, provider=provider or parent.provider,
            project_id=parent.project_id, parent_id=parent.id, hidden=True,
        )

    def remove(self, parent: Session, agent_id: str) -> None:
        """Убрать агента из сессии: его разговоры в ней удаляются."""
        children = [item for item in self._sessions.list() if item.parent_id == parent.id and item.agent_id == agent_id]
        if any(self.busy(item.id) for item in children):
            raise SessionBusyError("Агент сейчас работает в этой сессии: дождитесь ответа или остановите его")
        for child in children:
            self._sessions.delete(child.id)

    def personal(self, agent_id: str, provider: str, model: str) -> Session:
        """Личный чат с агентом вне проектов: один на агента, создаётся при первом открытии.
        У основного агента это общий чат — для вопросов, которым не нужен проект."""
        agent = self._agents.get(agent_id)
        if agent is None and agent_id != MAIN_AGENT_ID:
            raise ValueError(f"Саб-агента {agent_id} нет")
        for item in self._sessions.list():
            if item.kind == "agent" and item.agent_id == agent_id:
                return item
        if agent is None:
            return self._sessions.create(
                title=MAIN_AGENT_NAME, agent_id=MAIN_AGENT_ID, model=model, provider=provider,
                project_id=None, hidden=True, kind="agent",
            )
        return self._sessions.create(
            title=agent.name, agent_id=agent.id, model=self._model or model, provider=self._provider or provider,
            project_id=None, hidden=True, kind="agent",
        )

    def busy(self, session_id: str) -> bool:
        """Идёт ход, ждёт решения по карточке или стоят поручения: очищать и убирать такой разговор нельзя."""
        session = self._sessions.get(session_id)
        queue = self._queues.get(session_id)
        return (
            self._runtime.is_busy(session_id) or session_id in self._awaiting
            or (queue is not None and queue.locked()) or bool(session and session.pending)
        )

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, child_id: str, task: str) -> None:
        async with self._queues.setdefault(child_id, asyncio.Lock()):
            # Разговор может быть занят пользователем или ждать решения по карточке: поручение подождёт.
            while self._runtime.is_busy(child_id) or self._pending(child_id):
                await asyncio.sleep(QUEUE_WAIT_SECONDS)
            if self._sessions.get(child_id) is None:
                return
            async with self._slots:
                await self._turn(child_id, task)
            # Ход остановился на карточке: следующее поручение ждёт, пока по ней решат и ход закончится.
            while child_id in self._awaiting and (self._runtime.is_busy(child_id) or self._pending(child_id)):
                await asyncio.sleep(QUEUE_WAIT_SECONDS)
            # Ход закончился, а итога нет и после паузы на хук — продолжение остановили, ждать нечего.
            for _ in range(int(FINISH_GRACE_SECONDS / QUEUE_WAIT_SECONDS)):
                if child_id not in self._awaiting:
                    return
                await asyncio.sleep(QUEUE_WAIT_SECONDS)
            if child_id in self._awaiting:
                self._awaiting.discard(child_id)
                self._sessions.set_task(child_id, status="failed", error="Остановлено")

    async def _turn(self, child_id: str, task: str) -> None:
        self._awaiting.add(child_id)
        self._sessions.set_task(child_id, title=_title(task), status="running", error=None, from_name=MAIN_AGENT_NAME)
        try:
            while True:
                try:
                    async for event in self._runtime.stream_turn(child_id, task, f"delegate-{uuid4().hex[:8]}", sender=DELEGATOR):
                        if event["type"] == "interrupt":
                            self._sessions.set_task(child_id, status="waiting")
                    return
                except SessionBusyError:
                    # Пользователь успел написать агенту между проверкой и началом хода.
                    await asyncio.sleep(QUEUE_WAIT_SECONDS)
        except (AgentRuntimeError, LLMProviderError) as exc:
            await self._fail(child_id, str(exc))
        except asyncio.CancelledError:
            self._awaiting.discard(child_id)
            self._sessions.set_task(child_id, status="failed", error="Остановлено")
            raise
        except Exception as exc:  # noqa: BLE001 — сбой фоновой задачи не должен пропасть молча
            logger.exception("Задача саб-агента упала session_id=%s", child_id)
            await self._fail(child_id, f"внутренняя ошибка: {type(exc).__name__}")

    def _pending(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        return bool(session and session.pending)

    async def ask(self, asker: Session, agent_id: str, question: str) -> str:
        """Агент обращается к другому агенту и ждёт ответа: вопрос или поручение уходит в разговор адресата в этой
        же сессии (адресат, которого там не было, добавляется), ответ возвращается спросившему результатом
        инструмента. Подтверждения адресата — обычные карточки пользователю."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агента {agent_id} нет")
        if agent_id == asker.agent_id:
            raise ValueError("Обратиться к самому себе нельзя")
        chain = self._chain(asker)
        if agent_id in chain:
            raise ValueError(f"{agent.name} уже ждёт ответа в этой цепочке обращений: так получился бы цикл")
        if len(chain) >= MAX_CHAIN:
            raise ValueError(
                f"Цепочка обращений уже из {len(chain)} агентов ({' → '.join(chain)}): дальше не передавай — "
                "сделай сам или напиши, что нужно, в своём ответе"
            )
        owner = self._owner(asker)
        if owner is None:
            raise ValueError("Обращаться к другим агентам можно только в сессии проекта")
        sender = self._agents.get(asker.agent_id)
        sender_name = sender.name if sender else asker.agent_id
        target = self.chat(owner, agent_id)
        async with self._queues.setdefault(target.id, asyncio.Lock()):
            # Адресат может быть занят своей задачей или ждать решения по карточке: вопрос подождёт.
            while self._runtime.is_busy(target.id) or self._pending(target.id):
                await asyncio.sleep(QUEUE_WAIT_SECONDS)
            answer = asyncio.get_running_loop().create_future()
            self._questions[target.id] = answer
            self._asked_by[target.id] = asker.id
            self._sessions.set_task(
                target.id, agent_id=agent.id, agent_name=agent.name, title=_title(question), status="running",
                error=None, from_name=sender_name,
            )
            try:
                while True:
                    try:
                        async for event in self._runtime.stream_turn(
                            target.id, question, f"ask-{uuid4().hex[:8]}", sender=sender_name,
                        ):
                            if event["type"] == "interrupt":
                                self._sessions.set_task(target.id, status="waiting")
                        break
                    except SessionBusyError:
                        await asyncio.sleep(QUEUE_WAIT_SECONDS)
                # Ответ приходит через on_finished: сразу или после решения пользователя по карточке.
                text = await asyncio.wait_for(answer, ASK_TIMEOUT_SECONDS)
            except (AgentRuntimeError, LLMProviderError) as exc:
                self._sessions.set_task(target.id, status="failed", error=str(exc))
                raise ValueError(f"{agent.name} не ответил: {exc}") from exc
            except TimeoutError as exc:
                self._sessions.set_task(target.id, status="failed", error="Не дождались ответа")
                raise ValueError(f"{agent.name} не ответил за {ASK_TIMEOUT_SECONDS // 60} минут") from exc
            finally:
                self._questions.pop(target.id, None)
                self._asked_by.pop(target.id, None)
        self._sessions.set_task(target.id, status="done", error=None)
        return text

    def handoff(self, session: Session, task: str, reason: str) -> Session:
        """Пользователь написал саб-агенту задачу не по профилю: она уходит основному агенту — в основную сессию
        проекта или, из личного чата, в общий чат основного, — и он сразу берётся её распределять."""
        if session.id in self._awaiting or session.id in self._questions:
            raise ValueError(
                "Эту задачу тебе дал агент, а не пользователь: не передавай её — ответь, что она не по твоему "
                "профилю и почему"
            )
        agent = self._agents.get(session.agent_id)
        name = agent.name if agent else session.agent_id
        target = self._owner(session) or self.personal(MAIN_AGENT_ID, session.provider, session.model)
        why = f" ({reason.strip()})" if reason.strip() else ""
        content = f"Пользователь написал агенту «{name}» задачу не по его профилю{why}. Распредели её:\n\n{task}"
        key = f"handoff:{target.id}:{uuid4().hex[:8]}"
        self._tasks[key] = asyncio.create_task(self._report(target.id, name, content), name=key)
        self._tasks[key].add_done_callback(lambda _: self._tasks.pop(key, None))
        return target

    def _chain(self, session: Session) -> list[str]:
        """Агенты, которые сейчас ждут друг друга, от первого спросившего до этого: им вопрос задавать нельзя."""
        agents: list[str] = []
        current: Session | None = session
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if current.agent_id != MAIN_AGENT_ID:
                agents.append(current.agent_id)
            asker = self._asked_by.get(current.id)
            current = self._sessions.get(asker) if asker else None
        return list(reversed(agents))

    def _owner(self, session: Session) -> Session | None:
        """Основная сессия проекта, которой принадлежит разговор агента."""
        current: Session | None = session
        while current is not None and current.parent_id:
            current = self._sessions.get(current.parent_id)
        return current if current is not None and current.kind == "chat" and current.project_id else None

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
        """Итог поручения уходит основному агенту, и он сразу продолжает работу: проверяет результат,
        делает следующий шаг (например, передаёт его другому агенту) или докладывает пользователю."""
        # Посреди хода основного агента реплика встала бы между вызовом инструмента и его результатом.
        while self._runtime.is_busy(parent_id) or (self._turns is not None and self._turns.running(parent_id)):
            await asyncio.sleep(PARENT_WAIT_SECONDS)
        parent = self._sessions.get(parent_id)
        if parent is None:
            return
        # Основной ждёт решения по карточке: новая реплика стала бы ответом на неё — итог просто ложится в чат.
        if self._turns is None or parent.pending:
            await asyncio.to_thread(self._conversation.add_user_message, parent_id, content, sender)
            return
        try:
            self._turns.start(parent_id, self._runtime.stream_turn(parent_id, content, f"report-{uuid4().hex[:8]}", sender=sender))
        except SessionBusyError:
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
    """Саб-агент обращается к другому агенту команды — с вопросом или поручением — и ждёт ответа."""

    id = "ask_agent"
    name = "Обратиться к агенту"
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
            "Обратиться к другому агенту команды: поручить ему часть задачи или задать вопрос — и дождаться ответа. "
            "Передай всё целиком: что нужно, пути, контекст, в каком виде вернуть. "
            "К кому можно обратиться: " + "; ".join(f"{agent.id} — {agent.description or agent.name}" for agent in known)
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": list(self._targets)},
                "question": {"type": "string", "description": "Поручение или вопрос целиком"},
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
