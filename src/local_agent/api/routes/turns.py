"""Маршруты хода агента: обычный ответ, потоковый ответ (SSE) и подтверждение вызовов инструментов."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from local_agent.agent.runtime import (
    AgentRuntime,
    AgentRuntimeError,
    SessionBusyError,
    SessionNotFoundError,
)
from local_agent.api.routes.sessions import get_session_service
from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.memory.models import Message
from local_agent.sessions.service import SessionService

router = APIRouter(prefix="/sessions/{session_id}", tags=["turns"])


class TurnCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = Field(min_length=1)


class ApprovalDecision(BaseModel):
    approved: bool


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
        return await runtime.run_turn(
            session_id, payload.content, request.state.request_id
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
    if sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    if runtime.is_busy(session_id):
        raise HTTPException(status_code=409, detail="В этой сессии ещё выполняется предыдущий ход")

    async def events():
        saved = False
        try:
            async for event in runtime.stream_turn(session_id, payload.content, request.state.request_id):
                saved = saved or event["type"] == "user_message"
                yield _sse(event)
        except (AgentRuntimeError, LLMProviderError) as exc:
            yield _sse({"type": "error", "message": str(exc), "user_message_saved": saved})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/approvals/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
def decide_approval(
    session_id: str,
    call_id: str,
    payload: ApprovalDecision,
    runtime: Annotated[AgentRuntime, Depends(get_agent_runtime)],
) -> None:
    if not runtime.resolve_approval(session_id, call_id, payload.approved):
        raise HTTPException(status_code=404, detail="Запрос подтверждения уже не активен")


def _sse(event: dict) -> str:
    data = {
        key: value.model_dump(mode="json") if isinstance(value, Message) else value
        for key, value in event.items()
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
