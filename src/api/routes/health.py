"""Маршрут health сообщает, что HTTP-приложение запущено и отвечает."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
