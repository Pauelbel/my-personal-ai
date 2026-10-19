"""Раздел «Настройки»: data/settings.json и список моделей сервера."""

from fastapi import APIRouter, HTTPException, Request

from meepo.llm.client import LLMError
from meepo.settings import UserSettings

router = APIRouter(prefix="/api")


@router.get("/settings")
def get_settings(request: Request) -> dict:
    services = request.app.state.services
    settings = services.settings()
    return {**settings.__dict__, "env_base_url": services.env.llm_base_url, "env_model": services.env.default_model,
            "active_model": settings.model or services.env.default_model}


@router.put("/settings")
def put_settings(body: dict, request: Request) -> dict:
    services = request.app.state.services
    current = services.settings().__dict__
    unknown = set(body) - set(current)
    if unknown:
        raise HTTPException(400, f"Неизвестные настройки: {', '.join(sorted(unknown))}")
    UserSettings(**{**current, **body}).save(services.settings_path)
    return get_settings(request)


@router.get("/models")
async def list_models(request: Request) -> list[str]:
    try:
        return await request.app.state.services.llm().list_models()
    except LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
