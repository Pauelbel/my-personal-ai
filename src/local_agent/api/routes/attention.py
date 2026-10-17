"""Что требует внимания пользователя: карточки подтверждений и идущие ходы."""

from fastapi import APIRouter, Request

router = APIRouter(prefix="/attention", tags=["attention"])


@router.get("")
def attention(request: Request, session_id: str | None = None) -> dict:
    """session_id — открытая сессия: её карточки идут первыми, остальные следом."""
    state = request.app.state
    # Карточки и ход проверки вики видны на вкладке «Вики», а не в общей панели.
    sessions = [session for session in state.session_service.list() if session.kind != "wiki"]
    cards = [
        {"session_id": session.id, "owner_id": session.id, "session_title": session.title, "pending": session.pending}
        for session in sorted(sessions, key=lambda item: item.updated_at, reverse=True)
        # Решение по карточке уже принято и ход продолжается: карточка висела бы серой до конца хода.
        if session.pending and not state.agent_runtime.is_busy(session.id)
    ]
    if session_id:
        cards.sort(key=lambda card: card["owner_id"] != session_id)
    # Сессии, в которых сейчас идёт ход, — даже если ни одна вкладка его не смотрит.
    running = [session.id for session in sessions if state.agent_runtime.is_busy(session.id)]
    return {"cards": cards, "running": running}
