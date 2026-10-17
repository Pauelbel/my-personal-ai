"""Команда сессии для холста: основной агент, его саб-агенты, кто кому поручал и кто чем занят."""

from fastapi import APIRouter, HTTPException, Request

from local_agent.core.delegation import DELEGATOR
from local_agent.core.errors import SessionBusyError
from local_agent.sessions.models import Session

router = APIRouter(prefix="/sessions/{session_id}/team", tags=["team"])


def _owner(request: Request, session_id: str) -> Session:
    sessions = request.app.state.session_service
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    owner = sessions.get(session.parent_id) if session.parent_id else session
    if owner is None:
        raise HTTPException(status_code=404, detail="Основная сессия удалена")
    return owner


def _status(request: Request, session: Session) -> str:
    if request.app.state.agent_runtime.is_busy(session.id):
        return "running"
    if session.pending:
        return "waiting"
    task = session.task or {}
    return task.get("status") if task.get("status") in {"queued", "running", "failed", "done"} else "idle"


@router.get("")
def team(session_id: str, request: Request) -> dict:
    """Узлы: основной агент и все саб-агенты (с задачами в этой сессии и свободные).
    Связи: поручения и вопросы, которые были в этой сессии, и связи с холста — кто кого может спросить."""
    state = request.app.state
    owner = _owner(request, session_id)
    every = state.session_service.list()
    by_id = {item.id: item for item in every}
    # Все потомки основной сессии: поручения основного агента и вопросы саб-агентов друг другу.
    family = [item for item in every if item.id != owner.id and root_of(item, by_id) == owner.id]
    registered = {agent.id: agent for agent in state.agent_registry.all()}
    nodes = [{
        "id": "main", "name": "Основной агент", "description": "Говорит с вами и раздаёт задачи",
        "session_id": owner.id, "status": _status(request, owner), "task": None, "tasks": 0, "archived": False, "member": True,
    }]
    edges: dict[tuple[str, str], dict] = {}
    for agent_id in dict.fromkeys([*registered, *(child.agent_id for child in family)]):
        own = sorted((child for child in family if child.agent_id == agent_id and child.kind == "chat"), key=lambda item: item.updated_at)
        # Разговор с агентом открывается в его сессии у основной; вопросы от других агентов — только для статуса.
        direct = [child for child in own if child.parent_id == owner.id]
        latest = own[-1] if own else None
        agent = registered.get(agent_id)
        nodes.append({
            "id": agent_id,
            "name": agent.name if agent else (latest.task or {}).get("agent_name", agent_id),
            "description": agent.description if agent else "в архиве",
            "session_id": (direct[-1] if direct else latest).id if latest else None,
            "status": _status(request, latest) if latest else "idle",
            "task": (latest.task or {}).get("title") if latest else None,
            # Кто поручил последнюю задачу: основной агент или другой саб-агент.
            "from": (latest.task or {}).get("from_name") if latest else None,
            "tasks": sum(1 for child in own if child.task),
            "archived": agent is None,
            # Участник сессии: у агента есть разговор прямо в ней, а не только ответы на вопросы других агентов.
            "member": bool(direct),
        })
        for child in own:
            if not child.task:
                continue
            parent = by_id.get(child.parent_id)
            source = "main" if parent is None or parent.id == owner.id else parent.agent_id
            edge = edges.setdefault((source, agent_id), {"from": source, "to": agent_id, "tasks": 0, "link": False})
            edge["tasks"] += 1
    for agent in registered.values():
        for target in agent.agents:
            if target in registered:
                edges.setdefault((agent.id, target), {"from": agent.id, "to": target, "tasks": 0, "link": False})["link"] = True
    return {"owner_id": owner.id, "nodes": nodes, "edges": list(edges.values())}


def root_of(session: Session, by_id: dict[str, Session]) -> str:
    """Основная сессия, от которой пошла цепочка поручений и вопросов."""
    seen = set()
    while session.parent_id and session.parent_id in by_id and session.id not in seen:
        seen.add(session.id)
        session = by_id[session.parent_id]
    return session.parent_id or session.id


@router.get("/log")
def log(session_id: str, request: Request) -> list[dict]:
    """Лента команды для основного чата: кто из саб-агентов кому что поручил или спросил и что ответил.
    Поручения основного агента и их итоги и так видны в основном чате, здесь — переписка агентов между собой."""
    state = request.app.state
    owner = _owner(request, session_id)
    names = {agent.id: agent.name for agent in state.agent_registry.all()}
    events = []
    for dialog in state.session_service.list():
        if dialog.parent_id != owner.id or dialog.kind != "chat":
            continue
        agent = names.get(dialog.agent_id, (dialog.task or {}).get("agent_name", dialog.agent_id))
        asker = None
        for message in state.conversation_service.list(dialog.id):
            if message.role == "user":
                # Пишет другой агент (не основной и не пользователь) — это обращение агента к агенту.
                asker = message.sender if message.sender and message.sender != DELEGATOR else None
                if asker:
                    events.append(_event(message, asker, agent, "ask", dialog.id))
            elif asker and message.role == "assistant" and not message.tool_calls and message.content.strip():
                events.append(_event(message, agent, asker, "answer", dialog.id))
                asker = None
    return sorted(events, key=lambda item: item["at"])



def _event(message, sender: str, recipient: str, kind: str, dialog_id: str) -> dict:
    return {
        "at": message.created_at.isoformat(), "from": sender, "to": recipient, "kind": kind,
        "text": message.content, "session_id": dialog_id,
    }


@router.post("/{agent_id}/chat", response_model=Session)
def open_chat(session_id: str, agent_id: str, request: Request) -> Session:
    """Открыть разговор с саб-агентом: его последнюю сессию у этой основной или новую."""
    owner = _owner(request, session_id)
    try:
        return request.app.state.delegation.chat(owner, agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{agent_id}", status_code=204)
def remove(session_id: str, agent_id: str, request: Request) -> None:
    """Убрать агента из сессии: его разговор в ней удаляется."""
    owner = _owner(request, session_id)
    try:
        request.app.state.delegation.remove(owner, agent_id)
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


links_router = APIRouter(prefix="/agents/{agent_id}/links", tags=["team"])


@links_router.put("/{target}", status_code=204)
def link(agent_id: str, target: str, request: Request) -> None:
    """Связь с холста: agent_id может спрашивать target посреди своей задачи (инструмент ask_agent)."""
    _set_link(request, agent_id, target, True)


@links_router.delete("/{target}", status_code=204)
def unlink(agent_id: str, target: str, request: Request) -> None:
    _set_link(request, agent_id, target, False)


def _set_link(request: Request, agent_id: str, target: str, connected: bool) -> None:
    try:
        request.app.state.capabilities.link(agent_id, target, connected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
