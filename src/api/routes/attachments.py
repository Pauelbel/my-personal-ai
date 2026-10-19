"""Картинки для чата: интерфейс загружает вставленную картинку и получает её имя для сообщения."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(prefix="/attachments", tags=["attachments"])


class Upload(BaseModel):
    # Картинка как data URL: data:image/png;base64,…
    data: str


@router.post("", status_code=201)
def upload(payload: Upload, request: Request) -> dict:
    try:
        name = request.app.state.attachments.save(payload.data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"name": name, "url": f"/api/attachments/{name}"}


@router.get("/{name}")
def download(name: str, request: Request) -> FileResponse:
    path = request.app.state.attachments.path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Картинки нет")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
