"""Агенты и инструменты: архив, файлы, создание, восстановление и перечитывание с диска."""

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.files import load_agents
from core.graph import prepare_arguments

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class CapabilitySource(BaseModel):
    kind: str
    id: str
    path: str
    files: dict[str, str]


@router.get("")
def overview(request: Request) -> dict:
    state = request.app.state
    data = state.capabilities.overview()
    # Шаблоны агента и его шагов открываются в редакторе вместо пустого поля.
    data["templates"] = state.capabilities.templates()
    # Серверы MCP с переключателями: у работающего — все его инструменты, выключенные тоже.
    data["mcp"] = {"started": state.mcp.started, "servers": [_server(server) for server in state.mcp.servers.values()]}
    return data


def _server(server) -> dict:
    return {
        "name": server.name, "description": server.description, "status": server.status, "enabled": server.enabled,
        "tools": [
            {"id": tool.id, "name": tool.remote_name, "description": tool.description,
             "enabled": tool.remote_name not in server.disabled}
            for tool in server.known.values()
        ],
    }


@router.get("/{kind}/{item_id}", response_model=CapabilitySource)
def source(kind: str, item_id: str, request: Request) -> CapabilitySource:
    """Файлы возможности как есть: их можно открыть в редакторе и поправить руками."""
    try:
        folder, files = request.app.state.capabilities.source(kind, item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CapabilitySource(kind=kind, id=item_id, path=str(folder), files=files)


@router.post("/{kind}/{item_id}/archive", status_code=204)
def archive(kind: str, item_id: str, request: Request) -> None:
    try:
        request.app.state.capabilities.archive(kind, item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{kind}/restore/{archive_id}")
def restore(kind: str, archive_id: str, request: Request) -> dict:
    try:
        return {"id": request.app.state.capabilities.restore(kind, archive_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileExistsError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class SecretValue(BaseModel):
    value: str


@router.put("/secrets/{name}", status_code=204)
def set_secret(name: str, body: SecretValue, request: Request) -> None:
    """Значение только записывается: обратно API его не отдаёт ни здесь, ни в обзоре."""
    try:
        request.app.state.secrets.set(name, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/secrets/{name}", status_code=204)
def delete_secret(name: str, request: Request) -> None:
    try:
        request.app.state.secrets.delete(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/connectors/{tool_id}/check")
async def check_connector(tool_id: str, request: Request) -> dict:
    """«Проверить подключение» в форме коннектора: тот же скрипт, что вызывает агент, с action check."""
    capabilities = request.app.state.capabilities
    if not capabilities.is_script_tool(tool_id):
        raise HTTPException(status_code=404, detail="Коннектора нет: он в архиве или удалён")
    result = await request.app.state.tool_registry.get(tool_id).execute({"action": "check"}, None)
    if result.is_error:
        raise HTTPException(status_code=400, detail=result.content)
    return {"message": result.content}


class TryArguments(BaseModel):
    arguments: dict[str, object] = {}


@router.post("/connectors/{tool_id}/try")
async def try_connector(tool_id: str, body: TryArguments, request: Request) -> dict:
    """«Попробовать» на карточке инструмента: тот же вызов, что сделает модель, и ответ — ровно то, что она увидит.
    Только у инструментов без подтверждения: папка проекта для них только на чтение, а здесь её нет вовсе."""
    capabilities = request.app.state.capabilities
    if not capabilities.is_script_tool(tool_id):
        raise HTTPException(status_code=404, detail="Коннектора нет: он в архиве или удалён")
    tool = request.app.state.tool_registry.get(tool_id)
    if tool.requires_approval:
        raise HTTPException(status_code=400, detail="Попробовать здесь можно только инструмент без подтверждения")
    arguments = _form_arguments(tool.parameters, body.arguments)
    if problem := prepare_arguments(tool, arguments):
        raise HTTPException(status_code=400, detail=problem)
    started = time.monotonic()
    result = await tool.execute(arguments, None)
    return {"content": result.content, "is_error": result.is_error, "seconds": round(time.monotonic() - started, 1)}


def _form_arguments(schema: dict, raw: dict[str, object]) -> dict[str, object]:
    """Значения из полей формы: пустое поле — параметра нет, у числового параметра строка цифр — число."""
    properties = schema.get("properties") or {}
    arguments: dict[str, object] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            if (properties.get(name) or {}).get("type") == "integer" and value.lstrip("-").isdigit():
                value = int(value)
        arguments[name] = value
    return arguments


class Packages(BaseModel):
    requirements: list[str]


@router.post("/packages")
async def install_packages(body: Packages, request: Request) -> dict:
    """Пакеты для коннектора ставит пользователь кнопкой «установить» — это и есть его согласие."""
    env = request.app.state.capabilities.env
    if env is None:
        raise HTTPException(status_code=404, detail="Окружение песочницы не настроено")
    result = await env.install(body.requirements)
    if result.is_error:
        raise HTTPException(status_code=400, detail=result.content)
    return {"message": result.content}


@router.delete("/packages/{name}", status_code=204)
async def uninstall_package(name: str, request: Request) -> None:
    """Пакет из окружения песочницы удаляет только пользователь, из интерфейса."""
    env = request.app.state.capabilities.env
    if env is None:
        raise HTTPException(status_code=404, detail="Окружение песочницы не настроено")
    result = await env.uninstall(name)
    if result.is_error:
        raise HTTPException(status_code=400, detail=result.content)


class Switch(BaseModel):
    enabled: bool


@router.put("/mcp/{server}")
async def switch_server(server: str, body: Switch, request: Request) -> dict:
    """Сервер MCP выключен — не запущен, его инструментов нет ни у агента, ни у его шагов. Сохраняется в mcp.json."""
    manager = request.app.state.mcp
    if not manager.started:
        raise HTTPException(status_code=409, detail="MCP выключен в .env (MCP_ENABLED=false)")
    if server not in manager.servers:
        raise HTTPException(status_code=404, detail="Такого сервера MCP нет")
    return _server(await manager.set_server(server, body.enabled))


@router.put("/mcp/{server}/tools/{tool_id}")
async def switch_tool(server: str, tool_id: str, body: Switch, request: Request) -> dict:
    """Выключенный инструмент MCP уходит из реестра сразу; включённый возвращается."""
    manager = request.app.state.mcp
    state = manager.servers.get(server)
    if state is None or tool_id not in state.known:
        raise HTTPException(status_code=404, detail="Такого инструмента у работающего сервера нет")
    return _server(manager.set_tool(server, tool_id, body.enabled))


@router.post("/library/{kind}/{item_id}")
def enable(kind: str, item_id: str, request: Request) -> dict:
    """«Включить» готового агента или коннектор."""
    try:
        return {"id": request.app.state.capabilities.enable(kind, item_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/reload")
def reload(request: Request) -> dict:
    """Файлы поправили руками: реестры перечитываются без перезапуска сервера."""
    state = request.app.state
    settings = state.settings
    try:
        agents = load_agents(settings.data_path("agents_path", "agents"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for agent in state.agent_registry.all():
        state.agent_registry.remove(agent.id)
    for agent in agents:
        state.agent_registry.replace(agent)
    for tool in state.tool_registry.all():
        if state.capabilities.is_script_tool(tool.id):
            state.tool_registry.unregister(tool.id)
    state.capabilities.load()
    return overview(request)


# Общие маршруты /{kind}/… — последними: иначе они перехватили бы /secrets/… и /packages/….
class SourceUpdate(BaseModel):
    # Путь файла в папке возможности → новый текст; None — удалить steps.md или скрипт агента.
    files: dict[str, str | None]


class NewCapability(BaseModel):
    id: str
    text: str


@router.post("/{kind}", status_code=201)
def create(kind: str, body: NewCapability, request: Request) -> dict:
    """Новый AGENT.md проверяется и записывается, только если он рабочий."""
    try:
        return {"id": request.app.state.capabilities.create(kind, body.id.strip(), body.text)}
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{kind}/{item_id}", status_code=204)
def save_source(kind: str, item_id: str, body: SourceUpdate, request: Request) -> None:
    """Правка файлов из окна просмотра: неверный файл не сохраняется, ошибка возвращается как есть."""
    try:
        request.app.state.capabilities.save_source(kind, item_id, body.files)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileExistsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{kind}/archive/{archive_id}", status_code=204)
def purge(kind: str, archive_id: str, request: Request) -> None:
    if kind not in {"tool", "agent"}:
        raise HTTPException(status_code=404, detail="Нет такого вида возможностей")
    try:
        request.app.state.capabilities.purge(kind, archive_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
