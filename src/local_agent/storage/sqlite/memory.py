"""SQLite-адаптер сохраняет полный лог сообщений и обновляет время сессии."""

from __future__ import annotations

from local_agent.memory.models import Message
from local_agent.storage.database import SQLiteDatabase


class SQLiteConversationStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def save(self, message: Message) -> Message:
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.session_id,
                    message.role,
                    message.content,
                    message.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (message.created_at.isoformat(), message.session_id),
            )
            connection.commit()
        return message

    def list(self, session_id: str) -> list[Message]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
        return [Message.model_validate(dict(row)) for row in rows]

    def recent(self, session_id: str, limit: int) -> list[Message]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [Message.model_validate(dict(row)) for row in reversed(rows)]

    def count(self, session_id: str) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row[0])
