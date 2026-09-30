"""Маршруты агентов: список для выбора и холста, создание, правка всех параметров, удаление в архив и промпт."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.agent.loader import archive_agent, save_prompt, write_agent
from local_agent.agent.models import Agent
from local_agent.agent.registry import AgentRegistry
from local_agent.tools.registry import normalize_tool_ids

router = APIRouter(prefix="/agents", tags=["agents"])

# На нём работают обычные сессии и к нему переходят сессии удалённых агентов.
DEFAULT_AGENT_ID = "default"


class AgentInfo(BaseModel):
    id: str
    name: str


class AgentSummary(AgentInfo):
    """Все параметры агента: их показывают карточка на холсте и форма агента."""

    description: str
    provider: str
    model: str
    tools: list[str]
    # None — агенту доступны все навыки.
    skills: list[str] | None
    max_tool_rounds: int | None
    system_prompt: str


class AgentPrompt(AgentInfo):
    system_prompt: str


class AgentPromptUpdate(BaseModel):
    system_prompt: str


class AgentPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    provider: str = Field(default="lm_studio", min_length=1)
    model: str = ""
    tools: list[str] = Field(default_factory=list)
    skills: list[str] | None = None
    max_tool_rounds: int | None = None
    system_prompt: str = Field(min_length=1)


class AgentCreate(AgentPayload):
    # Необязательный: без него id генерируется. Узлы графа и send_message называют агента по id.
    id: str | None = None


class ToolOption(BaseModel):
    id: str
    name: str
    description: str
    requires_approval: bool


class AgentOptions(BaseModel):
    """Из чего собирается агент: инструменты и навыки, которые есть в приложении."""

    tools: list[ToolOption]
    skills: list[AgentInfo]


def get_agent_registry(request: Request) -> AgentRegistry:
    return request.app.state.agent_registry


Agents = Annotated[AgentRegistry, Depends(get_agent_registry)]


def summary(agent: Agent) -> AgentSummary:
    return AgentSummary(
        id=agent.id, name=agent.name, description=agent.description, provider=agent.llm_provider,
        model=agent.model, tools=list(agent.tools), skills=None if agent.skills is None else list(agent.skills),
        max_tool_rounds=agent.max_tool_rounds, system_prompt=agent.system_prompt,
    )


@router.get("", response_model=list[AgentSummary])
def list_agents(agents: Agents) -> list[AgentSummary]:
    # Основной агент первым: с него начинается каждая сессия.
    return [summary(agent) for agent in sorted(agents.all(), key=lambda agent: agent.id != DEFAULT_AGENT_ID)]


@router.get("/options", response_model=AgentOptions)
def agent_options(request: Request) -> AgentOptions:
    return AgentOptions(
        tools=[
            ToolOption(id=tool.id, name=tool.name, description=tool.description, requires_approval=tool.requires_approval)
            for tool in request.app.state.tool_registry.all()
        ],
        skills=[AgentInfo(id=skill.id, name=skill.name) for skill in request.app.state.skill_registry.all()],
    )


@router.post("", response_model=AgentSummary, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, request: Request, agents: Agents) -> AgentSummary:
    agent_id = payload.id or f"agent-{uuid4().hex[:6]}"
    if agents.get(agent_id) is not None:
        raise HTTPException(status_code=409, detail="Агент с таким ID уже есть")
    agent = _save(request, agent_id, payload, create=True)
    agents.replace(agent)
    return summary(agent)


@router.put("/{agent_id}", response_model=AgentSummary)
def update_agent(agent_id: str, payload: AgentPayload, request: Request, agents: Agents) -> AgentSummary:
    if agents.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    agent = _save(request, agent_id, payload, create=False)
    agents.replace(agent)
    return summary(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: str, request: Request, agents: Agents) -> None:
    if agent_id == DEFAULT_AGENT_ID:
        raise HTTPException(status_code=400, detail="Основного агента удалить нельзя: на нём работают обычные сессии")
    if agents.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    # Холст с пропавшим агентом нельзя было бы запустить: сначала агента убирают с холстов.
    owners = [
        session.title for session in request.app.state.session_service.list()
        if any(node.agent_id == agent_id for node in session.canvas.nodes)
    ]
    if owners:
        raise HTTPException(status_code=409, detail=f"Агент стоит на холсте сессий: {', '.join(owners)}")
    archive_agent(request.app.state.settings.agents_path, agent_id)
    agents.remove(agent_id)
    if agents.get(DEFAULT_AGENT_ID) is not None:
        request.app.state.session_service.reassign_agent(agent_id, DEFAULT_AGENT_ID)


@router.get("/{agent_id}/prompt", response_model=AgentPrompt)
def read_agent_prompt(agent_id: str, agents: Agents) -> AgentPrompt:
    agent = agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    return AgentPrompt(id=agent.id, name=agent.name, system_prompt=agent.system_prompt)


@router.put("/{agent_id}/prompt", response_model=AgentPrompt)
def save_agent_prompt(agent_id: str, payload: AgentPromptUpdate, request: Request, agents: Agents) -> AgentPrompt:
    if agents.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    settings = request.app.state.settings
    try:
        agent = save_prompt(settings.agents_path, agent_id, payload.system_prompt, settings.default_model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    agents.replace(agent)
    return AgentPrompt(id=agent.id, name=agent.name, system_prompt=agent.system_prompt)


def _save(request: Request, agent_id: str, payload: AgentPayload, *, create: bool) -> Agent:
    state = request.app.state
    if state.llm_registry.get(payload.provider) is None:
        raise HTTPException(status_code=400, detail="Провайдер LLM не найден")
    agent = Agent(
        id=agent_id, name=payload.name, system_prompt=payload.system_prompt, llm_provider=payload.provider,
        model=payload.model, tools=tuple(normalize_tool_ids(payload.tools)), description=payload.description,
        skills=None if payload.skills is None else tuple(payload.skills), max_tool_rounds=payload.max_tool_rounds,
    )
    try:
        return write_agent(
            state.settings.agents_path, agent,
            known_tools={tool.id for tool in state.tool_registry.all()},
            known_skills={skill.id for skill in state.skill_registry.all()},
            default_model=state.settings.default_model, create=create,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="Агент с таким ID уже есть") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
