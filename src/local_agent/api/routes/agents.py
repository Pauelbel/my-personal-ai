"""Блок «Агенты»: основной агент и саб-агенты с тем, чем каждый занят; создание и правка вручную, личный чат.

Основной агент встроен: удалить и переписать его промпт нельзя, но можно дописать свои правила поверх."""

from dataclasses import replace

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.agent.main_agent import MAIN_AGENT_DESCRIPTION, MAIN_AGENT_ID, MAIN_AGENT_NAME
from local_agent.agent.models import Agent
from local_agent.agent.team import team_graph

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    system_prompt: str = Field(min_length=1)
    tools: list[str] = []
    # None — агенту доступны все скиллы, список — только эти, пустой список — ни одного.
    skills: list[str] | None = None
    # К кому агент может обращаться: пустой список — ко всем агентам команды, иначе только к этим.
    # None при правке — оставить как было.
    agents: list[str] | None = None


class NewAgent(AgentFields):
    id: str = Field(min_length=1, max_length=64)


class Instructions(BaseModel):
    text: str = ""


class ChatModel(BaseModel):
    provider: str | None = None
    model: str | None = None


@router.get("")
def agents(request: Request) -> list[dict]:
    """status: running — где-то идёт его ход или ждёт поручение, waiting — ждёт решения по карточке, idle — свободен."""
    state = request.app.state
    every = state.session_service.list()
    # Основной — первым: его сессии — разговоры проектов (без родителя) и общий чат.
    main = [session for session in every if session.agent_id == MAIN_AGENT_ID and not session.parent_id]
    general = next((session for session in main if session.kind == "agent"), None)
    result = [{
        "id": MAIN_AGENT_ID, "name": MAIN_AGENT_NAME, "description": MAIN_AGENT_DESCRIPTION, "builtin": True,
        "instructions": state.main_instructions.read(), "chat_id": general.id if general else None,
        "status": _status(request, main),
    }]
    sessions = [session for session in every if session.agent_id != MAIN_AGENT_ID]
    for agent in state.agent_registry.all():
        own = [session for session in sessions if session.agent_id == agent.id]
        personal = next((session for session in own if session.kind == "agent"), None)
        result.append({
            **_fields(agent), "chat_id": personal.id if personal else None, "status": _status(request, own),
        })
    return result


@router.get("/team")
def team(request: Request) -> dict:
    """Справочник «Команда»: все агенты и кто кому может поручать — без привязки к сессии."""
    return team_graph(request.app.state.agent_registry)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(payload: NewAgent, request: Request) -> dict:
    if request.app.state.agent_registry.get(payload.id) is not None:
        raise HTTPException(status_code=409, detail=f"Агент {payload.id} уже есть")
    agent = Agent(
        id=payload.id, name=payload.name, system_prompt=payload.system_prompt, llm_provider="lm_studio", model="",
        tools=tuple(payload.tools), description=payload.description, skills=_skills(payload.skills),
        agents=tuple(payload.agents or ()),
    )
    return _fields(_save(request, agent))


@router.put("/{agent_id}")
def update(agent_id: str, payload: AgentFields, request: Request) -> dict:
    """Правка роли: связи с холста, закреплённая модель и лимит раундов остаются как были."""
    current = request.app.state.agent_registry.get(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    return _fields(_save(request, replace(
        current, name=payload.name, description=payload.description, system_prompt=payload.system_prompt,
        tools=tuple(payload.tools), skills=_skills(payload.skills),
        agents=current.agents if payload.agents is None else tuple(payload.agents),
    )))


@router.put("/main/instructions")
def save_instructions(payload: Instructions, request: Request) -> dict:
    """Дополнительные правила основного агента: действуют со следующего хода во всех сессиях."""
    try:
        return {"instructions": request.app.state.main_instructions.write(payload.text)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{agent_id}/chat")
def personal_chat(agent_id: str, payload: ChatModel, request: Request) -> dict:
    """Личный чат с агентом вне проектов. Модель — выбранная в интерфейсе, иначе последнего проекта."""
    state = request.app.state
    projects = sorted(state.project_service.list(), key=lambda project: project.updated_at, reverse=True)
    provider = payload.provider or (projects[0].provider if projects else "lm_studio")
    model = payload.model or (projects[0].model if projects else "") or state.settings.default_model
    try:
        session = state.delegation.personal(agent_id, provider, model)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.model_dump(mode="json")


def _save(request: Request, agent: Agent) -> Agent:
    try:
        return request.app.state.capabilities.save_agent(agent)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _fields(agent: Agent) -> dict:
    return {
        "id": agent.id, "name": agent.name, "description": agent.description, "system_prompt": agent.system_prompt,
        "tools": list(agent.tools), "skills": None if agent.skills is None else list(agent.skills),
        "agents": list(agent.agents),
    }


def _skills(skills: list[str] | None) -> tuple[str, ...] | None:
    return None if skills is None else tuple(skills)


def _status(request: Request, sessions) -> str:
    runtime, delegation = request.app.state.agent_runtime, request.app.state.delegation
    if any(session.pending for session in sessions):
        return "waiting"
    if any(runtime.is_busy(session.id) or delegation.busy(session.id) for session in sessions):
        return "running"
    return "idle"
