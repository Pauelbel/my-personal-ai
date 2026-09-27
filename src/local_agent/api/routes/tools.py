"""API инструментов показывает каталог и меняет переключатели доступа конкретной сессии."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from local_agent.api.routes.sessions import get_session_service
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService
from local_agent.tools.base import Tool
from local_agent.tools.registry import ToolRegistry

router = APIRouter(prefix="/sessions/{session_id}/tools", tags=["tools"])


class ToolInfo(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool


class ToolToggle(BaseModel):
    enabled: bool


def get_registry(request: Request) -> ToolRegistry:
    return request.app.state.tool_registry


def _info(tool: Tool, session: Session) -> ToolInfo:
    return ToolInfo(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        enabled=tool.id in session.enabled_tools,
    )


@router.get("", response_model=list[ToolInfo])
def list_tools(
    session_id: str,
    registry: Annotated[ToolRegistry, Depends(get_registry)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
) -> list[ToolInfo]:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return [_info(tool, session) for tool in registry.all()]


@router.put("/{tool_id}", response_model=ToolInfo)
def set_tool(
    session_id: str,
    tool_id: str,
    payload: ToolToggle,
    registry: Annotated[ToolRegistry, Depends(get_registry)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
) -> ToolInfo:
    tool = registry.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    session = sessions.set_tool_enabled(session_id, tool_id, payload.enabled)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return _info(tool, session)
