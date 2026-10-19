"""Карточки подтверждения и доверие в сессии.

Ход ждёт решения пользователя (`asyncio.Future`) при открытом потоке событий. Ожидающая карточка пишется в JSON
сессии (`pending`), чтобы пережить перезагрузку страницы. Перезапуск сервера ожидание сбрасывает:
`reset_pending` при старте убирает карточки и пишет в чат, что ход прерван.

Решение: {"action": "approve" | "reject" | "reply", "text": ответ словами, "trust": разрешать такие до конца сессии}.
Группы доверия: files_write, command, skill_run, skill_edit, browser.
"""

import asyncio
from collections.abc import Callable
from uuid import uuid4

ACTIONS = {"approve", "reject", "reply"}
INTERRUPTED = "Ход прерван: сервер перезапустился, пока ждал вашего решения. Напишите «продолжай», чтобы продолжить."


def normalize(value: object) -> dict:
    """Всё непонятное — отказ; «ответить» без текста — тоже отказ."""
    if not isinstance(value, dict) or value.get("action") not in ACTIONS:
        return {"action": "reject", "text": "", "trust": False}
    text = str(value.get("text") or "").strip()
    action = "reject" if value["action"] == "reply" and not text else value["action"]
    return {"action": action, "text": text, "trust": action == "approve" and value.get("trust") is True}


class Approvals:
    def __init__(self, sessions) -> None:
        self._sessions = sessions
        self._waiting: dict[str, tuple[str, asyncio.Future]] = {}

    async def ask(self, session_id: str, card: dict, emit: Callable[[dict], None]) -> dict:
        card = {"id": uuid4().hex, **card}
        future = asyncio.get_running_loop().create_future()
        self._waiting[session_id] = (card["id"], future)
        self._sessions.update(session_id, pending=card)
        emit({"type": "approval", "card": card})
        try:
            return await future
        finally:
            self._waiting.pop(session_id, None)
            self._sessions.update(session_id, pending=None)

    def decide(self, session_id: str, decision: dict) -> bool:
        """False — ход уже не ждёт решения (или ждёт по другой карточке)."""
        waiting = self._waiting.get(session_id)
        if waiting is None or waiting[1].done():
            return False
        card_id = decision.get("card_id") if isinstance(decision, dict) else None
        if card_id and card_id != waiting[0]:
            return False
        waiting[1].set_result(normalize(decision))
        return True


def reset_pending(sessions) -> None:
    """При старте сервера: ходов уже нет, карточки ждать некому."""
    for session in sessions.list():
        if session.get("pending"):
            sessions.update(session["id"], pending=None)
            sessions.add_message(session["id"], "assistant", INTERRUPTED)
