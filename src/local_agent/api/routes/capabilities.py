"""Возможности агента для UI: активные и архив, просмотр файла, архивирование, восстановление, перечитывание с диска."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from local_agent.agent.loader import load_agents, load_skills

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
    data["mcp"] = [
        {"name": server.name, "description": server.description, "status": server.status, "tools": server.tools}
        for server in state.mcp.servers.values()
    ]
    return data


@router.get("/{kind}/{item_id}", response_model=CapabilitySource)
def source(kind: str, item_id: str, request: Request) -> CapabilitySource:
    """Файлы возможности как есть: их можно открыть в редакторе и поправить руками."""
    capabilities = request.app.state.capabilities
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

