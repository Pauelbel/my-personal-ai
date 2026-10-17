"""Маршруты хода агента: обычный ответ, потоковый ответ (SSE) и продолжение хода после решения по карточке.

Потоковый ход идёт в TurnHub независимо от вкладки: к нему можно подключиться заново, остановить его
отдельным запросом и поставить следующее сообщение в очередь. Поток заканчивается событием done
(ответ готов), interrupt (ход ждёт решения), stopped (остановлен) или error."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_agent.api.routes.sessions import get_session_service
from local_agent.core.runtime import AgentRuntime, AgentRuntimeError, SessionBusyError, SessionNotFoundError
from local_agent.core.turns import Turn, TurnHub
from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.memory.models import Message
from local_agent.sessions.service import SessionService

router = APIRouter(prefix="/sessions/{session_id}", tags=["turns"])


class TurnCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = ""
    # Картинки, вставленные в реплику: имена из POST /attachments. С картинкой текст можно не писать.
    attachments: list[str] = []
    # Повтор после сбоя: реплика уже сохранена и осталась без ответа.
    retry: bool = False

    @model_validator(mode="after")
    def not_empty(self):
        if not self.content and not self.attachments:
            raise ValueError("Пустое сообщение")
        return self


class Decision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    action: Literal["approve", "reject", "reply"]
    text: str = ""
    # Для карточки со списком: номера пунктов, которые пользователь отметил.
    selected: list[int] | None = None
    # Утвердить и дальше не спрашивать про эту группу инструментов (например, браузер) в этой сессии.
    trust: bool = False
    # Карточка «Кто сделает»: id выбранного агента или main.
    choice: str | None = Field(default=None, max_length=64)


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


@router.post("/turns", response_model=Message)
async def create_turn(
    session_id: str,
    payload: TurnCreate,
    request: Request,
    runtime: Annotated[AgentRuntime, Depends(get_agent_runtime)],
) -> Message:
    try:
        _check_attachments(request, payload.attachments)
        return await runtime.run_turn(
            session_id, payload.content, request.state.request_id, tuple(payload.attachments),
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMProviderUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"message": str(exc), "user_message_saved": True},
        ) from exc
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": str(exc), "user_message_saved": True},
        ) from exc


@router.post("/turns/stream")
async def stream_turn(
    session_id: str,
    payload: TurnCreate,
    request: Request,
    runtime: Annotated[AgentRuntime, Depends(get_agent_runtime)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
) -> StreamingResponse:
    # Эти ошибки проверяем до ответа 200, чтобы клиент получил обычный HTTP-статус.
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    hub = get_turn_hub(request)
    if hub.running(session_id) is not None or runtime.is_busy(session_id):
        raise HTTPException(status_code=409, detail="В этой сессии ещё выполняется предыдущий ход: сообщение можно поставить в очередь")
    _check_attachments(request, payload.attachments)
    if payload.attachments and session.pending:
        # Сообщение при карточке — ответ на неё словами, картинку к такому ответу не приложить.
        raise HTTPException(status_code=409, detail="Ход ждёт решения по карточке: сначала ответьте на неё, картинку пришлите следующим сообщением")
    turn = hub.start(session_id, runtime.stream_turn(
        session_id, payload.content, request.state.request_id, retry=payload.retry, attachments=tuple(payload.attachments),
    ))
    return _follow(hub, turn)


@router.post("/resume")
async def resume_turn(
    session_id: str,
    payload: Decision,
    request: Request,
    runtime: Annotated[AgentRuntime, Depends(get_agent_runtime)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
) -> StreamingResponse:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    hub = get_turn_hub(request)
    if hub.running(session_id) is not None or runtime.is_busy(session_id):
        raise HTTPException(status_code=409, detail="В этой сессии ещё выполняется предыдущий ход")
    if not session.pending:
        raise HTTPException(status_code=409, detail="Ход уже не ждёт решения")
    if payload.action == "reply" and not payload.text:
        raise HTTPException(status_code=400, detail="Пустой ответ")
    turn = hub.start(session_id, runtime.resume(session_id, payload.model_dump(), request.state.request_id))
    return _follow(hub, turn)


@router.get("/turn")
async def turn_state(session_id: str, request: Request) -> dict:
    """Идёт ли ход (к нему можно подключиться потоком) и что стоит в очереди."""
    hub = get_turn_hub(request)
    turn = hub.running(session_id)
    return {"running": turn is not None, "events": len(turn.events) if turn else 0, "queue": hub.queue(session_id)}


@router.get("/turn/stream")
async def follow_turn(session_id: str, request: Request, start: int = 0) -> StreamingResponse:
    """Подключиться к идущему ходу, например после перезагрузки страницы: события с номера start."""
    hub = get_turn_hub(request)
    turn = hub.running(session_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="Ход не идёт")
    return _follow(hub, turn, max(start, 0))


# Обработчики хаба асинхронные: синхронные FastAPI выполняет в потоке без цикла событий, а хаб запускает задачи.
@router.post("/turn/stop", status_code=204)
async def stop_turn(session_id: str, request: Request) -> None:
    if not get_turn_hub(request).stop(session_id):
        raise HTTPException(status_code=404, detail="Ход не идёт")


class QueuedMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = ""
    attachments: list[str] = []

    @model_validator(mode="after")
    def not_empty(self):
        if not self.content and not self.attachments:
            raise ValueError("Пустое сообщение")
        return self


@router.post("/queue", status_code=201)
async def enqueue(
    session_id: str, payload: QueuedMessage, request: Request,
    sessions: Annotated[SessionService, Depends(get_session_service)],
) -> dict:
    if sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    _check_attachments(request, payload.attachments)
    return get_turn_hub(request).enqueue(session_id, payload.content, tuple(payload.attachments))


@router.delete("/queue/{item_id}", status_code=204)
async def dequeue(session_id: str, item_id: str, request: Request) -> None:
    if not get_turn_hub(request).remove(session_id, item_id):
        raise HTTPException(status_code=404, detail="Сообщения в очереди нет")


def _check_attachments(request: Request, names: list[str]) -> None:
    store = request.app.state.attachments
    if missing := [name for name in names if store.path(name) is None]:
        raise HTTPException(status_code=400, detail=f"Картинки не найдены: {', '.join(missing)} — вставьте их заново")


def get_turn_hub(request: Request) -> TurnHub:
    return request.app.state.turn_hub


def _follow(hub: TurnHub, turn: Turn, start: int = 0) -> StreamingResponse:
    # Вкладка закрылась — перестаём отдавать события, но сам ход идёт дальше.
    async def events():
        async for event in hub.follow(turn, start):
            yield sse_event(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def sse_event(event: dict) -> str:
    data = {
        key: value.model_dump(mode="json") if isinstance(value, Message) else value
        for key, value in event.items()
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
