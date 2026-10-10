"""Команда в сессии: координатор без своей модели, только очередь сообщений между агентами холста.

Сообщение пользователя получает агент, выбранный в чате: по умолчанию агент сессии (карточка «вход»),
или любой другой агент холста, если пользователь пишет ему напрямую. Агенты пишут соседям по стрелкам
через send_message; сообщения встают в очередь. Если агент за ход никому не написал, его ответ возвращается
тому, кто дал ему задачу, а ответ агента сессии пользователю остаётся в чате сессии — это итог хода.
Каждый агент холста ведёт свою скрытую сессию внутри сессии и помнит прошлые поручения.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from local_agent.agent.models import Agent
from local_agent.agent.registry import AgentRegistry
from local_agent.agent.runtime import AgentRuntime, AgentRuntimeError, SessionBusyError
from local_agent.memory.conversation import ConversationService
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService
from local_agent.team.models import ENTRY_NODE_ID, Canvas, CanvasNode
from local_agent.team.validator import canvas_errors
from local_agent.tools.messaging import SendMessageTool


@dataclass(frozen=True)
class QueuedMessage:
    # None — пользователь.
    sender: str | None
    to: str
    content: str
    # Ответ на задачу, которую получатель сам кому-то поручил, а не новая задача для него.
    is_reply: bool = False


class TeamCoordinator:
    def __init__(
        self,
        runtime: AgentRuntime,
        sessions: SessionService,
        conversation: ConversationService,
        agents: AgentRegistry,
        max_steps: int,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._conversation = conversation
        self._agents = agents
        self._max_steps = max_steps
        # Сессии, где сейчас работает команда: между ходами агентов сессия свободна, но писать в неё рано.
        self._busy: set[str] = set()

    def is_busy(self, session_id: str) -> bool:
        return session_id in self._busy

    @staticmethod
    def is_team(session: Session) -> bool:
        """Команда включается, когда агенту сессии есть кому поручать работу; иначе это обычный чат."""
        return bool(session.canvas.recipients(ENTRY_NODE_ID))

    def errors(self, canvas: Canvas) -> list[str]:
        return canvas_errors(canvas, lambda agent_id: self._agents.get(agent_id) is not None)

    async def turn(
        self, session: Session, content: str, request_id: str, start: str = ENTRY_NODE_ID
    ) -> AsyncIterator[dict]:
        """start — агент холста, которому пишет пользователь; session — сессия-хозяин холста."""
        if errors := self.errors(session.canvas):
            raise AgentRuntimeError("На холсте ошибки: " + "; ".join(errors))
        if session.canvas.node(start) is None and start != ENTRY_NODE_ID:
            raise AgentRuntimeError("Этого агента уже нет на холсте сессии")
        # Между проверкой и захватом нет await, поэтому два хода в одной сессии не стартуют одновременно.
        if session.id in self._busy:
            raise SessionBusyError("В этой сессии ещё работает команда")
        self._busy.add(session.id)
        try:
            # aclosing: при закрытии потока вложенные генераторы закрываются сразу, а не сборщиком мусора.
            async with aclosing(self._loop(session, content, request_id, start)) as events:
                async for event in events:
                    yield event
        finally:
            self._busy.discard(session.id)

    async def _loop(self, session: Session, content: str, request_id: str, start: str) -> AsyncIterator[dict]:
        canvas = session.canvas
        queue = deque([QueuedMessage(sender=None, to=start, content=content)])
        # Кому узел вернёт результат: вершина стека — последний, кто дал ему задачу; None — пользователь.
        requesters: dict[str, list[str | None]] = {}
        steps = 0
        while queue:
            if steps >= self._max_steps:
                # Пометку видит тот, кому писал пользователь: в его переписке и остановился ход.
                note = await asyncio.to_thread(
                    self._conversation.add_assistant_message, self.thread(session, start),
                    f"_Команда остановлена: достигнут лимит {self._max_steps} шагов._",
                )
                yield {"type": "notice", "message": note}
                break
            message = queue.popleft()
            steps += 1
            node = self._node(canvas, message.to)
            if not message.is_reply:
                requesters.setdefault(node.id, []).append(message.sender)
            thread = self.thread(session, node.id)
            yield {"type": "node_started", "node_id": node.id, "session_id": thread, "step": steps}

            recipients = {item.id: self._about(session, item) for item in canvas.recipients(node.id)}
            messenger = SendMessageTool(recipients)
            sender = None if message.sender is None else self._name(session, message.sender)
            text = message.content if sender is None else f"[{'ответ' if message.is_reply else 'от'}: {sender}]\n\n{message.content}"
            answer = None
            turn = self._runtime.stream_turn(thread, text, request_id, [messenger] if recipients else [], sender=sender)
            async with aclosing(turn) as events:
                async for event in events:
                    if event["type"] == "done":
                        answer = event["message"]
                    yield {**event, "node_id": node.id, "session_id": thread}

            for to, text in messenger.sent:
                queue.append(QueuedMessage(sender=node.id, to=to, content=text))
            if messenger.sent:
                # Узел поручил работу другим и ждёт ответа: отвечать своему заказчику пока рано.
                continue
            stack = requesters.get(node.id, [])
            target = stack.pop() if stack else None
            # Ответ пользователю уже лежит в чате сессии; ответ без заказчика остаётся в чате узла.
            if target is not None:
                queue.append(QueuedMessage(sender=node.id, to=target, content=answer.content, is_reply=True))
        yield {"type": "team_finished", "steps": steps}

    def thread(self, session: Session, node_id: str) -> str:
        """Сессия, где агент холста ведёт свою переписку. У агента сессии это сама сессия; у остальных —
        скрытая сессия, которая создаётся при первом обращении и живёт, пока жива сессия-хозяин."""
        if node_id == ENTRY_NODE_ID:
            return session.id
        node = session.canvas.node(node_id)
        known = session.node_sessions.get(node.id)
        if known and self._sessions.get(known) is not None:
            return known
        agent = self._agents.get(node.agent_id)
        # Агент холста начинает с модели сессии-хозяйки, если в его файле не закреплена своя; дальше её меняют в его чате.
        provider, model = (agent.llm_provider, agent.model) if agent.model else (session.provider, session.model)
        child = self._sessions.create(
            title=f"{self._name(session, node.id)} · {session.title}", agent_id=agent.id,
            model=model, provider=provider,
            project_id=session.project_id, parent_id=session.id, node_id=node.id, hidden=True,
        )
        self._sessions.set_node_session(session.id, node.id, child.id)
        session.node_sessions[node.id] = child.id
        return child.id

    @staticmethod
    def _node(canvas: Canvas, node_id: str) -> CanvasNode:
        # Карточку «вход» холст мог ещё не сохранить: у пустого холста она подразумевается.
        node = canvas.node(node_id)
        if node is None and node_id != ENTRY_NODE_ID:
            raise AgentRuntimeError(f"Агента {node_id} нет на холсте сессии")
        return node or CanvasNode(id=ENTRY_NODE_ID, name="")

    def _name(self, session: Session, node_id: str) -> str:
        """Имя карточки; у карточки «вход» без своего имени — имя агента сессии."""
        node = self._node(session.canvas, node_id)
        agent = self._agent(session, node)
        return node.name or (agent.name if agent else node.id)

    def _about(self, session: Session, node: CanvasNode) -> str:
        agent = self._agent(session, node)
        return f"{self._name(session, node.id)}. {agent.description if agent else ''}".rstrip(". ")

    def _agent(self, session: Session, node: CanvasNode) -> Agent | None:
        return self._agents.get(session.agent_id if node.id == ENTRY_NODE_ID else node.agent_id)
