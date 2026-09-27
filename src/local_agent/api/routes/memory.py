"""Маршруты памяти дают UI доступ к Markdown и ручному обновлению текущей сессии."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from local_agent.api.routes.sessions import get_session_service
from local_agent.memory.markdown import MemoryDocument
from local_agent.memory.service import MemoryService, MemoryServiceError, MemoryUpdateResult
from local_agent.sessions.service import SessionService

router = APIRouter(tags=["memory"])


class MemoryDocumentSummary(BaseModel):
    name: str
    title: str


class MemoryDocumentUpdate(BaseModel):
    content: str


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


@router.get("/memory", response_model=list[MemoryDocumentSummary])
def list_memory(
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> list[MemoryDocumentSummary]:
    return [
        MemoryDocumentSummary(name=document.name, title=document.title)
        for document in service.list_documents()
    ]


@router.get("/memory/{name}", response_model=MemoryDocument)
def read_memory(
    name: str,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryDocument:
    try:
        return service.read_document(name)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Файл памяти не найден") from exc


@router.put("/memory/{name}", response_model=MemoryDocument)
def write_memory(
    name: str,
    payload: MemoryDocumentUpdate,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryDocument:
    try:
        return service.write_document(name, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимое имя файла памяти") from exc


@router.post(
    "/sessions/{session_id}/memory/update",
    response_model=MemoryUpdateResult,
)
async def update_memory(
    session_id: str,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryUpdateResult:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    try:
        return await service.update_session(session)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
