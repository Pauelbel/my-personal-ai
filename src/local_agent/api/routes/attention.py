"""Что требует внимания пользователя: карточки подтверждений, задачи саб-агентов и идущие ходы."""

from fastapi import APIRouter, Request

from local_agent.api.routes.team import root_of

router = APIRouter(prefix="/attention", tags=["attention"])


@router.get("")
def attention(request: Request, session_id: str | None = None) -> dict:
    """session_id — открытая сессия: её карточки и задачи её саб-агентов идут первыми, остальные следом."""
    state = request.app.state
    # Карточки и ход проверки вики видны на вкладке «Вики», а не в общей панели.
    sessions = [session for session in state.session_service.list() if session.kind != "wiki"]
    titles = {session.id: session.title for session in sessions}
    by_id = {session.id: session for session in sessions}
    cards, tasks = [], []
    for session in sorted(sessions, key=lambda item: item.updated_at, reverse=True):
        # Хозяйка — основная сессия цепочки: вопросы саб-агентов друг другу тоже видны в её доке.
        owner = root_of(session, by_id)
        # Решение по карточке уже принято и ход продолжается: карточка висела бы серой до конца хода.
        if session.pending and not state.agent_runtime.is_busy(session.id):
            cards.append({
                "session_id": session.id, "owner_id": owner, "session_title": titles.get(owner, session.title),
                "agent": (session.task or {}).get("agent_name"), "pending": session.pending,
            })
        if session.task:
            status = "waiting" if session.pending else session.task.get("status", "running")
            tasks.append({
                "session_id": session.id, "owner_id": owner, "session_title": titles.get(owner, ""),
                "agent": session.task.get("agent_name"), "title": session.task.get("title"), "status": status,
                # Кто поручил: по этому строится дерево задач «основной → агент → агент».
                "from": session.task.get("from_name"),
                "error": session.task.get("error"), "updated_at": session.updated_at.isoformat(),
            })
    if session_id:
        cards.sort(key=lambda card: card["owner_id"] != session_id)
        tasks.sort(key=lambda task: task["owner_id"] != session_id)
    # Сессии, в которых сейчас идёт ход, — даже если ни одна вкладка его не смотрит.
    running = [session.id for session in sessions if state.agent_runtime.is_busy(session.id)]
    return {"cards": cards, "tasks": tasks[:30], "running": running}
