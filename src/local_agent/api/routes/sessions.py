"""Маршруты сессий открывают создание и чтение разговоров через HTTP API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.sessions.models import DEFAULT_SESSION_TITLE, Session
from local_agent.sessions.service import InvalidWorkspaceError, SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default=DEFAULT_SESSION_TITLE, min_length=1)
    agent_id: str = Field(default="default", min_length=1)
    model: str | None = None
    provider: str = Field(default="lm_studio", min_length=1)
    workspace: str | None = None


class SessionConfiguration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    workspace: str | None = None
    agent_id: str | None = None


class SessionTitle(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=100)


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service


def require_agent(request: Request, agent_id: str | None) -> None:
    if agent_id is not None and request.app.state.agent_registry.get(agent_id) is None:
        raise HTTPException(status_code=400, detail="Агент не найден")


@router.post("", response_model=Session, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreate,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    require_agent(request, payload.agent_id)
    try:
        return service.create(
            title=payload.title,
            agent_id=payload.agent_id,
            model=payload.model if payload.model is not None else request.app.state.settings.default_model,
            provider=payload.provider,
            workspace=payload.workspace,
        )
    except InvalidWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=list[Session])
def list_sessions(
    service: Annotated[SessionService, Depends(get_session_service)],
) -> list[Session]:
    return service.list()


@router.get("/{session_id}", response_model=Session)
def get_session(
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    session = service.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return session


@router.put("/{session_id}/config", response_model=Session)
def configure_session(
    session_id: str,
    payload: SessionConfiguration,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    require_agent(request, payload.agent_id)
    try:
        session = service.configure(
            session_id,
            provider=payload.provider,
            model=payload.model,
            workspace=payload.workspace,
            agent_id=payload.agent_id,
        )
    except InvalidWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return session


@router.put("/{session_id}/title", response_model=Session)
def rename_session(
    session_id: str,
    payload: SessionTitle,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    session = service.rename(session_id, payload.title)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> None:
    if not service.delete(session_id):
        raise HTTPException(status_code=404, detail="Сессия не найдена")
