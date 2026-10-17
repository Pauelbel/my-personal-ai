"""Шина событий для интерфейса: что-то изменилось в сессии или проекте — открытые вкладки узнают сразу.

Событие — только повод перечитать данные, а не сами данные: если вкладка что-то пропустила
(переполнилась очередь, оборвалось соединение), следующее событие или запасной опрос всё догонят.
Публиковать можно из любого потока: хранилища пишут и из asyncio.to_thread.
"""

import asyncio
import contextlib

# Медленная вкладка не должна копить события без конца: лишние просто отбрасываются.
MAX_PENDING = 100


class EventBus:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(event)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._deliver, event)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def _deliver(self, event: dict) -> None:
        for queue in list(self._queues):
            if queue.qsize() < MAX_PENDING:
                queue.put_nowait(event)
