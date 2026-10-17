"""Провайдеры моделей: список, добавление из интерфейса (Ollama, LM Studio, OpenAI-совместимый API с ключом),
проверка подключения со списком моделей и модель по умолчанию.

Провайдеры из .env показываются, но меняются только в .env. Ключ API наружу не отдаётся — только его хвост.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.assembly import apply_default
from local_agent.llm.base import LLMProviderError
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.llm.store import KINDS, ProviderEntry, build_provider, masked, provider_id

router = APIRouter(prefix="/providers", tags=["providers"])
# Список моделей нужен быстро: если сервер не ответил за это время, он недоступен.
PROBE_TIMEOUT_SECONDS = 15.0


class ProviderFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern="^(" + "|".join(KINDS) + ")$")
    base_url: str = Field(min_length=1, max_length=500, pattern=r"^https?://")
    # None при правке — оставить сохранённый ключ, пустая строка — убрать его.
    api_key: str | None = Field(default=None, max_length=500)


class Probe(ProviderFields):
    # Проверка сохранённого провайдера без нового ключа берёт его сохранённый ключ.
    id: str | None = None
    name: str = "Проверка"


class Default(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


@router.get("")
def providers(request: Request) -> list[dict]:
    """Все провайдеры: из .env (editable false) и добавленные в интерфейсе."""
    state = request.app.state
    stored = {entry.id: entry for entry in state.provider_store.load().providers}
    env = _env(request)
    result = []
    for provider_id_, name in state.llm_registry.items():
        if (entry := stored.get(provider_id_)) is not None:
            result.append({
                "id": entry.id, "name": entry.name, "kind": entry.kind, "base_url": entry.base_url,
                "key": masked(entry.api_key), "editable": True,
            })
        else:
            base_url, key = env.get(provider_id_, ("", ""))
            result.append({"id": provider_id_, "name": name, "kind": "env", "base_url": base_url, "key": masked(key), "editable": False})
    return result


@router.get("/config")
def config(request: Request) -> dict:
    data = request.app.state.provider_store.load()
    return {
        "kinds": {kind: {"name": name, "base_url": url} for kind, (name, url) in KINDS.items()},
        "default_provider": data.default_provider or request.app.state.default_provider,
        "default_model": data.default_model or request.app.state.settings.default_model,
    }


@router.post("/probe")
async def probe(payload: Probe, request: Request) -> dict:
    """Подключиться и вернуть список моделей — до сохранения: так видно, что адрес и ключ верны."""
    key = payload.api_key
    if key is None and payload.id:
        key = _stored_key(request, payload.id)
    client = OpenAICompatibleProvider(
        payload.base_url, PROBE_TIMEOUT_SECONDS, _transport(request), name=payload.name, api_key=key or "",
        verify_ssl=request.app.state.settings.llm_verify_ssl,
    )
    try:
        models = await client.list_models()
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()
    return {"models": sorted(models, key=str.casefold)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(payload: ProviderFields, request: Request) -> dict:
    state = request.app.state
    data = state.provider_store.load()
    taken = {item for item, _ in state.llm_registry.items()} | {entry.id for entry in data.providers}
    entry = ProviderEntry(
        id=provider_id(payload.name, taken), name=payload.name, kind=payload.kind,
        base_url=payload.base_url, api_key=payload.api_key or "",
    )
    data.providers.append(entry)
    state.provider_store.save(data)
    await _register(request, entry)
    return {"id": entry.id}


@router.put("/default")
def set_default(payload: Default, request: Request) -> dict:
    """Модель для новых проектов и личных чатов."""
    state = request.app.state
    if state.llm_registry.get(payload.provider) is None:
        raise HTTPException(status_code=404, detail="Провайдер не найден")
    data = state.provider_store.load()
    data.default_provider, data.default_model = payload.provider, payload.model
    state.provider_store.save(data)
    apply_default(state, data.default_provider, data.default_model)
    return {"default_provider": payload.provider, "default_model": payload.model}


@router.put("/{provider_id}")
async def update(provider_id: str, payload: ProviderFields, request: Request) -> dict:
    state = request.app.state
    data = state.provider_store.load()
    entry = next((item for item in data.providers if item.id == provider_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Провайдер не найден или задан в .env")
    entry.name, entry.kind, entry.base_url = payload.name, payload.kind, payload.base_url
    if payload.api_key is not None:
        entry.api_key = payload.api_key
    state.provider_store.save(data)
    await _register(request, entry)
    return {"id": entry.id}


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(provider_id: str, request: Request) -> None:
    """Сессии с моделью этого провайдера останутся: в поле модели они покажут «нет на сервере»."""
    state = request.app.state
    data = state.provider_store.load()
    if not any(item.id == provider_id for item in data.providers):
        raise HTTPException(status_code=404, detail="Провайдер не найден или задан в .env")
    data.providers = [item for item in data.providers if item.id != provider_id]
    if data.default_provider == provider_id:
        data.default_provider = data.default_model = ""
    state.provider_store.save(data)
    if (previous := state.llm_registry.remove(provider_id)) is not None:
        await previous.close()


def _transport(request: Request):
    """Подмена HTTP-транспорта — только для тестов; в работе None, и запросы идут в сеть."""
    return getattr(request.app.state, "provider_transport", None)


async def _register(request: Request, entry: ProviderEntry) -> None:
    state = request.app.state
    previous = state.llm_registry.set(
        entry.id,
        build_provider(
            entry, state.settings.llm_timeout_seconds, _transport(request),
            verify_ssl=state.settings.llm_verify_ssl,
        ),
    )
    if previous is not None:
        await previous.close()


def _stored_key(request: Request, provider_id_: str) -> str:
    entry = next((item for item in request.app.state.provider_store.load().providers if item.id == provider_id_), None)
    if entry is not None:
        return entry.api_key
    return _env(request).get(provider_id_, ("", ""))[1]


def _env(request: Request) -> dict[str, tuple[str, str]]:
    """Провайдеры из .env: id → (адрес, ключ)."""
    settings = request.app.state.settings
    env = {"lm_studio": (settings.llm_base_url, "")}
    for item in settings.llm_providers:
        env[item.id] = (item.base_url, item.api_key)
    return env
