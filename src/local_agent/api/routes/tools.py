"""API инструментов показывает каталог и меняет общие переключатели доступа."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from local_agent.tools.settings import ToolSettings
from local_agent.tools.registry import ToolRegistry

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolInfo(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool


class ToolToggle(BaseModel):
    enabled: bool


def get_registry(request: Request) -> ToolRegistry:
    return request.app.state.tool_registry


def get_settings(request: Request) -> ToolSettings:
    return request.app.state.tool_settings


@router.get("", response_model=list[ToolInfo])
def list_tools(
    registry: Annotated[ToolRegistry, Depends(get_registry)],
    settings: Annotated[ToolSettings, Depends(get_settings)],
) -> list[ToolInfo]:
    enabled = settings.enabled_ids()
    return [
        ToolInfo(id=tool.id, name=tool.name, description=tool.description, enabled=tool.id in enabled)
        for tool in registry.all()
    ]


@router.put("/{tool_id}", response_model=ToolInfo)
def set_tool(
    tool_id: str,
    payload: ToolToggle,
    registry: Annotated[ToolRegistry, Depends(get_registry)],
    settings: Annotated[ToolSettings, Depends(get_settings)],
) -> ToolInfo:
    tool = registry.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    settings.set_enabled(tool_id, payload.enabled)
    return ToolInfo(id=tool.id, name=tool.name, description=tool.description, enabled=payload.enabled)
