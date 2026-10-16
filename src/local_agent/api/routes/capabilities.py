"""Возможности агента для UI: активные и архив, просмотр файла, архивирование, восстановление, перечитывание с диска."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from local_agent.agent.loader import load_agents, load_skills
from local_agent.agent.main_agent import MAIN_AGENT_ID, MAIN_PROMPT

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class CapabilitySource(BaseModel):
    kind: str
    id: str
    path: str
    files: dict[str, str]
    # Встроенное (промпт основного агента) только показывается: на нём держатся карточки, propose и вики.
    readonly: bool = False


@router.get("")
def overview(request: Request) -> dict:
    state = request.app.state
    data = state.capabilities.overview()
    data["mcp"] = [
        {"name": server.name, "description": server.description, "status": server.status, "tools": server.tools}
        for server in state.mcp.servers.values()
    ]
    return data


@router.get("/{kind}/{item_id}", response_model=CapabilitySource)
def source(kind: str, item_id: str, request: Request) -> CapabilitySource:
    """Файлы возможности как есть: их можно открыть в редакторе и поправить руками."""
    capabilities = request.app.state.capabilities
    if kind == "agent" and item_id == MAIN_AGENT_ID:
        rules = request.app.state.main_instructions.read()
        return CapabilitySource(
            kind=kind, id=item_id, readonly=True,
            path="встроен в приложение (src/local_agent/agent/main_agent.py); свои правила — кнопкой «правила»",
            files={"Встроенный промпт": MAIN_PROMPT, "Ваши правила — добавляются в конец промпта": rules or "(нет)"},
        )
    if kind == "tool" and capabilities.is_script_tool(item_id):
        folder = capabilities.paths.tools / item_id
        files = {name: (folder / name).read_text(encoding="utf-8") for name in ("tool.md", "parameters.json", "script.py")}
        return CapabilitySource(kind=kind, id=item_id, path=str(folder), files=files)
    if kind in {"skill", "agent"} and capabilities.exists(kind, item_id):
        root = capabilities.paths.skills if kind == "skill" else capabilities.paths.agents
        path = root / f"{item_id}.md"
        return CapabilitySource(kind=kind, id=item_id, path=str(path), files={path.name: path.read_text(encoding="utf-8")})
    raise HTTPException(status_code=404, detail="Возможность не найдена или встроена в приложение")


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


@router.delete("/packages/{name}", status_code=204)
async def uninstall_package(name: str, request: Request) -> None:
    """Пакет из окружения песочницы удаляет только пользователь, из интерфейса."""
    env = request.app.state.capabilities.env
    if env is None:
        raise HTTPException(status_code=404, detail="Окружение песочницы не настроено")
    result = await env.uninstall(name)
    if result.is_error:
        raise HTTPException(status_code=400, detail=result.content)


@router.post("/reload")
def reload(request: Request) -> dict:
    """Файлы поправили руками: реестры перечитываются без перезапуска сервера."""
    state = request.app.state
    settings = state.settings
    try:
        agents = load_agents(settings.agents_path)
        skills = load_skills(settings.skills_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for agent in state.agent_registry.all():
        state.agent_registry.remove(agent.id)
    for agent in agents:
        state.agent_registry.replace(agent)
    for skill in state.skill_registry.all():
        state.skill_registry.remove(skill.id)
    for skill in skills:
        state.skill_registry.replace(skill)
    for tool in state.tool_registry.all():
        if state.capabilities.is_script_tool(tool.id):
            state.tool_registry.unregister(tool.id)
    state.capabilities.load()
    return overview(request)


# Общие маршруты /{kind}/… — последними: иначе они перехватили бы /secrets/… и /packages/….
class SourceUpdate(BaseModel):
    files: dict[str, str]


@router.put("/{kind}/{item_id}", status_code=204)
def save_source(kind: str, item_id: str, body: SourceUpdate, request: Request) -> None:
    """Правка файлов из окна просмотра: неверный файл не сохраняется, ошибка возвращается как есть."""
    try:
        request.app.state.capabilities.save_source(kind, item_id, body.files)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{kind}/archive/{archive_id}", status_code=204)
def purge(kind: str, archive_id: str, request: Request) -> None:
    if kind not in {"tool", "skill", "agent"}:
        raise HTTPException(status_code=404, detail="Нет такого вида возможностей")
    try:
        request.app.state.capabilities.purge(kind, archive_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
