"""Ходы, которые идут независимо от вкладки браузера.

Ход запускается фоновой задачей и копит свои события. Вкладка читает их потоком, а после
перезагрузки подключается к тому же ходу и получает всё с начала. Закрытая вкладка ход больше
не останавливает: для этого есть stop.

Пока ход идёт, новые сообщения встают в очередь сессии. Следующее уходит, только когда ход
закончился ответом: после остановки, ошибки или карточки, которая ждёт решения, очередь ждёт
пользователя — иначе сообщение ушло бы не туда (например, стало бы ответом на карточку).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from local_agent.agent.errors import AgentRuntimeError, SessionBusyError
from local_agent.llm.base import LLMProviderError

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    session_id: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    task: asyncio.Task | None = None
    # Будит тех, кто ждёт следующего события; на каждое событие — новый.
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    def push(self, event: dict) -> None:
        self.events.append(event)
        self._wake()

    def finish(self) -> None:
        self.done = True
        self._wake()

    def _wake(self) -> None:
        signal, self.signal = self.signal, asyncio.Event()
        signal.set()

    @property
    def outcome(self) -> str | None:
        return self.events[-1]["type"] if self.events else None


class TurnHub:
    def __init__(self, runtime, sessions, publish: Callable[[dict], None]) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._publish = publish
        self._turns: dict[str, Turn] = {}
        self._queues: dict[str, list[dict]] = {}

    def running(self, session_id: str) -> Turn | None:
        return self._turns.get(session_id)

    def start(self, session_id: str, events: AsyncIterator[dict]) -> Turn:
        """events — поток хода из runtime: stream_turn или resume."""
        if session_id in self._turns:
            raise SessionBusyError("В этой сессии ещё выполняется предыдущий ход")
        turn = Turn(session_id)
        self._turns[session_id] = turn
        turn.task = asyncio.create_task(self._drive(turn, events), name=f"turn:{session_id}")
        self._changed(session_id)
        return turn

    async def follow(self, turn: Turn, start: int = 0) -> AsyncIterator[dict]:
        """События хода с номера start: уже накопленные сразу, новые — по мере появления."""
        index = start
        while True:
            while index < len(turn.events):
                yield turn.events[index]
                index += 1
            if turn.done:
                return
            await turn.signal.wait()

    def stop(self, session_id: str) -> bool:
        turn = self._turns.get(session_id)
        if turn is None or turn.task is None:
            return False
        turn.task.cancel()
        return True

    def queue(self, session_id: str) -> list[dict]:
        return list(self._queues.get(session_id, []))

    def enqueue(self, session_id: str, content: str) -> dict:
        item = {"id": uuid4().hex, "content": content}
        self._queues.setdefault(session_id, []).append(item)
        self._changed(session_id)
        # Ход мог закончиться, пока сообщение шло: тогда оно уходит сразу.
        if session_id not in self._turns:
            self._drain(session_id)
        return item

    def remove(self, session_id: str, item_id: str) -> bool:
        items = self._queues.get(session_id, [])
        kept = [item for item in items if item["id"] != item_id]
        if len(kept) == len(items):
            return False
        self._queues[session_id] = kept
        self._changed(session_id)
        return True

    async def close(self) -> None:
        tasks = [turn.task for turn in self._turns.values() if turn.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _drive(self, turn: Turn, events: AsyncIterator[dict]) -> None:
        saved = False
        try:
            async for event in events:
                saved = saved or event["type"] == "user_message"
                turn.push(event)
        except (AgentRuntimeError, LLMProviderError) as exc:
            turn.push({"type": "error", "message": str(exc), "user_message_saved": saved})
        except asyncio.CancelledError:
            # «Стоп»: runtime уже сохранил то, что модель успела написать.
            turn.push({"type": "stopped"})
        except Exception as exc:  # noqa: BLE001 — сбой хода должен дойти до вкладки, а не пропасть в логе
            logger.exception("Ход упал session_id=%s", turn.session_id)
            turn.push({"type": "error", "message": f"Внутренняя ошибка: {type(exc).__name__}", "user_message_saved": saved})
        finally:
            with contextlib.suppress(Exception):
                await events.aclose()
            if self._turns.get(turn.session_id) is turn:
                del self._turns[turn.session_id]
            turn.finish()
            self._changed(turn.session_id)
        if turn.outcome == "done":
            self._drain(turn.session_id)

    def _drain(self, session_id: str) -> None:
        items = self._queues.get(session_id)
        if not items or session_id in self._turns or self._runtime.is_busy(session_id):
            return
        session = self._sessions.get(session_id)
        if session is None:
            self._queues.pop(session_id, None)
            return
        if session.pending:
            return
        item = items.pop(0)
        self.start(session_id, self._runtime.stream_turn(session_id, item["content"], f"queue-{item['id'][:8]}"))

    def _changed(self, session_id: str) -> None:
        self._publish({"type": "changed", "session_id": session_id})
