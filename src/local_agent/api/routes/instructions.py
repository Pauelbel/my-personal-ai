"""Правила основного агента — ваши дополнения к встроенному промпту; правятся в окне «⚙ Настройки»."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from local_agent.agents.main import MAIN_PROMPT

router = APIRouter(prefix="/instructions", tags=["instructions"])


class Instructions(BaseModel):
    text: str = ""


@router.get("")
def read(request: Request) -> dict:
    """Ваши правила и встроенный промпт: его можно посмотреть, но не изменить — на нём держатся карточки и вики."""
    return {"instructions": request.app.state.main_instructions.read(), "builtin": MAIN_PROMPT}


@router.put("")
def save(payload: Instructions, request: Request) -> dict:
    """Правила действуют со следующего хода во всех сессиях."""
    try:
        return {"instructions": request.app.state.main_instructions.write(payload.text)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
