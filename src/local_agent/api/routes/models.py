"""Маршруты каталога: провайдеры, их модели, доступные агенты и их системные промпты."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from local_agent.agent.loader import save_prompt
from local_agent.agent.registry import AgentRegistry
from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.llm.registry import LLMRegistry

router = APIRouter(tags=["models"])


class ModelList(BaseModel):
    models: list[str]
    default_model: str


class ProviderInfo(BaseModel):
    id: str
    name: str


class AgentInfo(BaseModel):
    id: str
    name: str


class AgentPrompt(AgentInfo):
    system_prompt: str


class AgentPromptUpdate(BaseModel):
    system_prompt: str


def get_llm_registry(request: Request) -> LLMRegistry:
    return request.app.state.llm_registry


def get_agent_registry(request: Request) -> AgentRegistry:
    return request.app.state.agent_registry


@router.get("/models", response_model=ModelList)
async def list_models(
    request: Request,
    registry: Annotated[LLMRegistry, Depends(get_llm_registry)],
    provider: str = "lm_studio",
) -> ModelList:
    selected = registry.get(provider)
    if selected is None:
        raise HTTPException(status_code=400, detail="Провайдер LLM не найден")
    try:
        models = await selected.list_models()
    except LLMProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ModelList(models=models, default_model=request.app.state.settings.default_model)


@router.get("/providers", response_model=list[ProviderInfo])
def list_providers(
    registry: Annotated[LLMRegistry, Depends(get_llm_registry)],
) -> list[ProviderInfo]:
    return [ProviderInfo(id=provider_id, name=name) for provider_id, name in registry.items()]


@router.get("/agents", response_model=list[AgentInfo])
def list_agents(
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
) -> list[AgentInfo]:
    return [AgentInfo(id=agent.id, name=agent.name) for agent in registry.all()]


@router.get("/agents/{agent_id}/prompt", response_model=AgentPrompt)
def read_agent_prompt(
    agent_id: str,
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
) -> AgentPrompt:
    agent = registry.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    return AgentPrompt(id=agent.id, name=agent.name, system_prompt=agent.system_prompt)


@router.put("/agents/{agent_id}/prompt", response_model=AgentPrompt)
def save_agent_prompt(
    agent_id: str,
    payload: AgentPromptUpdate,
    request: Request,
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
) -> AgentPrompt:
    if registry.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    settings = request.app.state.settings
    try:
        agent = save_prompt(settings.agents_path, agent_id, payload.system_prompt, settings.default_model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.replace(agent)
    return AgentPrompt(id=agent.id, name=agent.name, system_prompt=agent.system_prompt)
