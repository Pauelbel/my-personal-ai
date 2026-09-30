"""Маршруты сессий открывают создание и чтение разговоров через HTTP API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.projects.service import ProjectError
from local_agent.sessions.models import DEFAULT_SESSION_TITLE, Session
from local_agent.sessions.service import InvalidWorkspaceError, SessionService
from local_agent.team.models import ENTRY_NODE_ID, Canvas

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default=DEFAULT_SESSION_TITLE, min_length=1)
    agent_id: str = Field(default="default", min_length=1)
    model: str | None = None
    provider: str = Field(default="lm_studio", min_length=1)
    workspace: str | None = None
    # В проекте агент, провайдер, модель и папка берутся из проекта.
    project_id: str | None = None


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
    agent_id, provider = payload.agent_id, payload.provider
    model = payload.model if payload.model is not None else request.app.state.settings.default_model
    project_id = payload.project_id
    projects = request.app.state.project_service
    if project_id is None and payload.workspace:
        # Папка бывает только у проекта: сессия с папкой сразу попадает в проект этой папки.
        try:
            project_id = projects.for_workspace(
                payload.workspace, agent_id=agent_id, provider=provider, model=model
            ).id
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif project_id is not None:
        project = projects.get(project_id)
        if project is None:
            raise HTTPException(status_code=400, detail="Проект не найден")
        # Провайдер и модель проект запоминает, а агент у новой сессии всегда тот, что в запросе, —
        # по умолчанию основной: с него начинается каждая сессия, помощники добавляются на холст.
        provider, model = project.provider, project.model or model
    require_agent(request, agent_id)
    try:
        return service.create(
            title=payload.title,
            agent_id=agent_id,
            model=model,
            provider=provider,
            workspace=None,
            project_id=project_id,
        )
    except InvalidWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=list[Session])
def list_sessions(
    service: Annotated[SessionService, Depends(get_session_service)],
    include_hidden: bool = False,
) -> list[Session]:
    return [session for session in service.list() if include_hidden or not session.hidden]


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
    current = service.get(session_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    try:
        # Папка у сессии без проекта означает переход в проект этой папки.
        if current.project_id is None and payload.workspace:
            project = request.app.state.project_service.for_workspace(
                payload.workspace, agent_id=current.agent_id, provider=payload.provider, model=payload.model
            )
            service.assign_project(session_id, project.id)
        session = service.configure(
            session_id,
            provider=payload.provider,
            model=payload.model,
            workspace=None,
            agent_id=payload.agent_id,
        )
    except (InvalidWorkspaceError, ProjectError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return session


class CanvasView(BaseModel):
    canvas: Canvas
    # Холст сохраняется и с ошибками, чтобы не терять правки; пока ошибки есть, команда не запустится.
    errors: list[str]


@router.put("/{session_id}/canvas", response_model=CanvasView)
def save_canvas(
    session_id: str,
    canvas: Canvas,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> CanvasView:
    current = service.get(session_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    if current.parent_id:
        raise HTTPException(status_code=409, detail="У сессии агента с холста своего холста нет")
    session = service.set_canvas(session_id, canvas)
    return CanvasView(canvas=session.canvas, errors=request.app.state.team_coordinator.errors(session.canvas))


@router.post("/{session_id}/canvas/{node_id}/session", response_model=Session)
def open_node_session(
    session_id: str,
    node_id: str,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Session:
    """Переписка агента с холста: чтобы написать ему напрямую, даже если ему ещё ничего не поручали."""
    owner = service.get(session_id)
    if owner is None or owner.parent_id:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    coordinator = request.app.state.team_coordinator
    if node_id != ENTRY_NODE_ID and (owner.canvas.node(node_id) is None or coordinator.errors(owner.canvas)):
        raise HTTPException(status_code=400, detail="Агента нет на холсте или на холсте ошибки")
    return service.get(coordinator.thread(owner, node_id))


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
