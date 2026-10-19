"""Ходы, которые идут независимо от вкладки браузера.

Ход — фоновая задача, которая копит свои события. Вкладка читает их потоком (SSE), а после перезагрузки
подключается к тому же ходу и получает всё с начала. Закрытая вкладка ход не останавливает: для этого «Стоп».
Последнее событие хода — done (в том числе {"stopped": true} после «Стоп») или error.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from meepo.agent.loop import TurnError
from meepo.llm.client import LLMError

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


class TurnHub:
    def __init__(self) -> None:
        self._turns: dict[str, Turn] = {}

    def running(self, session_id: str) -> Turn | None:
        return self._turns.get(session_id)

    def start(self, session_id: str, work: Callable[[Callable[[dict], None]], Awaitable[None]]) -> Turn:
        """work(emit) — сам ход; события, переданные в emit, копятся в Turn."""
        if session_id in self._turns:
            raise TurnError("В этой сессии ещё идёт предыдущий ход")
        turn = Turn(session_id)
        self._turns[session_id] = turn
        turn.task = asyncio.create_task(self._drive(turn, work(turn.push)), name=f"turn:{session_id}")
        return turn

    async def follow(self, turn: Turn, start: int = 0) -> AsyncIterator[dict]:
        """События хода с номера start: накопленные сразу, новые — по мере появления."""
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

    async def close(self) -> None:
        tasks = [turn.task for turn in self._turns.values() if turn.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _drive(self, turn: Turn, work: Awaitable[None]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            turn.push({"type": "done", "stopped": True})
        except (TurnError, LLMError) as exc:
            turn.push({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 — сбой хода должен дойти до вкладки, а не пропасть в логе
            logger.exception("Ход упал session_id=%s", turn.session_id)
            turn.push({"type": "error", "message": f"Внутренняя ошибка: {type(exc).__name__}: {exc}"})
        finally:
            if self._turns.get(turn.session_id) is turn:
                del self._turns[turn.session_id]
            turn.finish()
