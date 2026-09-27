"""Маршрут хода агента запускает LLM через runtime и возвращает сохранённый ответ."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from local_agent.agent.runtime import AgentRuntime, AgentRuntimeError, SessionNotFoundError
from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.memory.models import Message

router = APIRouter(prefix="/sessions/{session_id}/turns", tags=["turns"])


class TurnCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = Field(min_length=1)


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


@router.post("", response_model=Message)
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
