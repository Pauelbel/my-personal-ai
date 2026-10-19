"""Раздел «Память»: файлы памяти — просмотр и ручная правка; обновление памяти из разговора кнопкой."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from meepo.llm.client import LLMError

router = APIRouter(prefix="/api")


class DocumentBody(BaseModel):
    content: str


@router.get("/memory")
def list_memory(request: Request) -> list[dict]:
    store = request.app.state.services.memory
    return [{"name": name, "content": store.read(name)} for name in store.names()]


@router.put("/memory/{name}")
def save_memory(name: str, body: DocumentBody, request: Request) -> dict:
    store = request.app.state.services.memory
    try:
        store.write(name, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"name": name, "content": store.read(name)}


@router.post("/sessions/{session_id}/memory/update")
async def update_memory(session_id: str, request: Request) -> dict:
    services = request.app.state.services
    if services.sessions.get(session_id) is None:
        raise HTTPException(404, "Сессия не найдена")
    try:
        return await services.memory_updater.update(session_id)
    except (LLMError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
