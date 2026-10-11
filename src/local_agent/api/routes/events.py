"""Поток событий для вкладок: «что-то изменилось» — повод перечитать сессии, карточки и команду."""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["events"])

# Комментарий-пинг раз в 15 секунд держит соединение живым через прокси и показывает, что сервер на месте.
KEEPALIVE_SECONDS = 15


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus = request.app.state.events
    queue = bus.subscribe()

    async def stream():
        try:
            # Браузер переподключается сам; через 3 секунды, а не через стандартные 5.
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
