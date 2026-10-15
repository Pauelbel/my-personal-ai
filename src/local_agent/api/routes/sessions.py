"""Маршруты сессий открывают создание и чтение разговоров через HTTP API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.agent.main_agent import MAIN_AGENT_ID
from local_agent.sessions.models import DEFAULT_SESSION_TITLE, Session
from local_agent.sessions.service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default=DEFAULT_SESSION_TITLE, min_length=1)
    agent_id: str = Field(default="main", min_length=1)
    model: str | None = None
    # Провайдер и папка берутся из проекта, модель — тоже, если проект её запомнил.
    # Без проекта сессию не создать: её папку задаёт проект.
    project_id: str | None = None


class SessionConfiguration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    agent_id: str | None = None


class SessionTitle(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=100)


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service


def require_agent(request: Request, agent_id: str | None) -> None:
    if agent_id is not None and agent_id != MAIN_AGENT_ID and request.app.state.agent_registry.get(agent_id) is None:
        raise HTTPException(status_code=400, detail="Агент не найден")


@router.post("", response_model=Session, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreate,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    agent_id = payload.agent_id
    model = payload.model if payload.model is not None else request.app.state.settings.default_model
    if not payload.project_id:
        raise HTTPException(status_code=400, detail="Сначала выберите проект: сессия работает в папке проекта")
    project = request.app.state.project_service.get(payload.project_id)
    if project is None:
        raise HTTPException(status_code=400, detail="Проект не найден")
    require_agent(request, agent_id)
    # Провайдер и модель проект запоминает, а агент у новой сессии всегда тот, что в запросе, —
    # по умолчанию основной: с него начинается каждая сессия, саб-агенты работают в дочерних сессиях.
    return service.create(
        title=payload.title,
        agent_id=agent_id,
        model=project.model or model,
        provider=project.provider,
        project_id=project.id,
    )


@router.get("", response_model=list[Session])
def list_sessions(
    service: Annotated[SessionService, Depends(get_session_service)],
    include_hidden: bool = False,
) -> list[Session]:
    # Служебная сессия вики живёт на вкладке «Вики» и в списке разговоров не нужна;
    # личные чаты с агентами скрыты и приходят вместе с остальными скрытыми.
    return [session for session in service.list() if session.kind != "wiki" and (include_hidden or not session.hidden)]


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
    # Папку здесь не меняют: она у проекта и меняется в его настройках.
    session = service.configure(
        session_id,
        provider=payload.provider,
        model=payload.model,
        agent_id=payload.agent_id,
    )
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


@router.post("/{session_id}/clear", response_model=Session)
def clear_session(
    session_id: str,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    """«Очистить»: разговор начинается заново. Пока агент работает или ждёт решения, очищать нельзя."""
    if service.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    if request.app.state.turn_hub.running(session_id) is not None or request.app.state.delegation.busy(session_id):
        raise HTTPException(status_code=409, detail="Сначала дождитесь ответа или остановите его")
    return service.clear(session_id)


@router.post("/{session_id}/discard", status_code=status.HTTP_204_NO_CONTENT)
def discard_session(
    session_id: str,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> None:
    """Убрать пустую сессию, из которой ушли. Решает сервер, а не интерфейс: сессию, где есть хоть одно
    сообщение, агент или идущий ход, он не удалит, даже если интерфейс по ошибке счёл её пустой."""
    state = request.app.state
    session = service.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    busy = state.turn_hub.running(session_id) is not None or state.agent_runtime.is_busy(session_id)
    has_agents = any(item.parent_id == session_id for item in service.list())
    if (
        session.kind != "chat" or session.parent_id or session.pending or busy or has_agents
        or state.conversation_service.count(session_id)
    ):
        raise HTTPException(status_code=409, detail="Сессия не пуста")
    service.delete(session_id)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: str,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> None:
    if not service.delete(session_id):
        raise HTTPException(status_code=404, detail="Сессия не найдена")
