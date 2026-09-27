"""SQLite-адаптер сохраняет полный лог сообщений и обновляет время сессии."""

from __future__ import annotations

import sqlite3

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
        try:
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
        except sqlite3.OperationalError:
            return []
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

    def after(self, session_id: str, message_id: str | None) -> list[Message]:
        with self._database.connect() as connection:
            if message_id is None:
                rows = connection.execute(
                    """
                    SELECT id, session_id, role, content, created_at
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                checkpoint = connection.execute(
                    "SELECT rowid FROM messages WHERE session_id = ? AND id = ?",
                    (session_id, message_id),
                ).fetchone()
                if checkpoint is None:
                    raise ValueError("Memory checkpoint message was not found")
                rows = connection.execute(
                    """
                    SELECT id, session_id, role, content, created_at
                    FROM messages
                    WHERE session_id = ? AND rowid > ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_id, checkpoint[0]),
                ).fetchall()
        return [Message.model_validate(dict(row)) for row in rows]

    def delete(self, session_id: str) -> None:
        with self._database.connect() as connection:
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            connection.commit()
