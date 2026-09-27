"""Маршрут моделей показывает доступные модели выбранного LLM-провайдера."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.llm.registry import LLMRegistry

router = APIRouter(prefix="/models", tags=["models"])


class ModelList(BaseModel):
    models: list[str]
    default_model: str


def get_llm_registry(request: Request) -> LLMRegistry:
    return request.app.state.llm_registry


@router.get("", response_model=ModelList)
async def list_models(
    request: Request,
    registry: Annotated[LLMRegistry, Depends(get_llm_registry)],
    provider: str = "lm_studio",
) -> ModelList:
    selected = registry.get(provider)
    if selected is None:
        raise HTTPException(status_code=400, detail="LLM provider not found")
    try:
        models = await selected.list_models()
    except LLMProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ModelList(models=models, default_model=request.app.state.settings.default_model)
