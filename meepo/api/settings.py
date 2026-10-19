"""Раздел «Настройки»: data/settings.json и список моделей сервера."""

from fastapi import APIRouter, HTTPException, Request

from meepo.llm.client import LLMError
from meepo.settings import UserSettings, default_guards

router = APIRouter(prefix="/api")

# Числовые настройки: допустимые границы.
LIMITS = {"default_context_tokens": (1024, 1_048_576), "max_tool_rounds": (1, 200), "memory_auto_update_messages": (0, 1000)}


def check(fields: dict) -> str | None:
    known = UserSettings.__dataclass_fields__
    if unknown := sorted(set(fields) - set(known)):
        return f"Неизвестные настройки: {', '.join(unknown)}"
    for name, value in fields.items():
        if name in LIMITS:
            low, high = LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                return f"{name} — целое число от {low} до {high}"
        elif name == "guards":
            if not isinstance(value, dict) or set(value) - set(default_guards()) or not all(isinstance(on, bool) for on in value.values()):
                return f"guards — {{{', '.join(default_guards())}: true/false}}"
        elif name == "browser_headed":
            if not isinstance(value, bool):
                return "browser_headed — true или false"
        elif not isinstance(value, str):
            return f"{name} — строка"
    base = fields.get("llm_base_url", "")
    if base and not base.startswith(("http://", "https://")):
        return "Адрес сервера модели — http://… или https://…, с /v1 на конце"
    return None


@router.get("/settings")
def get_settings(request: Request) -> dict:
    services = request.app.state.services
    settings = services.settings()
    return {**settings.__dict__, "env_base_url": services.env.llm_base_url, "env_model": services.env.default_model,
            "active_model": settings.model or services.env.default_model,
            "active_base_url": settings.llm_base_url or services.env.llm_base_url}


@router.put("/settings")
def put_settings(body: dict, request: Request) -> dict:
    services = request.app.state.services
    if problem := check(body):
        raise HTTPException(400, problem)
    current = services.settings().__dict__
    UserSettings(**{**current, **body}).save(services.settings_path)
    return get_settings(request)


@router.get("/models")
async def list_models(request: Request) -> list[str]:
    try:
        return await request.app.state.services.llm().list_models()
    except LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
