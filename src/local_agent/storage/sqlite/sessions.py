"""Репозиторий сессий сохраняет и читает метаданные разговоров из SQLite."""

from datetime import datetime, timezone

from local_agent.sessions.models import DEFAULT_SESSION_TITLE, Session
from local_agent.storage.database import SQLiteDatabase


class SQLiteSessionRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def save(self, session: Session) -> Session:
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, title, created_at, updated_at, agent_id, model, provider, workspace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.title,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.agent_id,
                    session.model,
                    session.provider,
                    session.workspace,
                ),
            )
            connection.commit()
        return session

    def get(self, session_id: str) -> Session | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return Session.model_validate(dict(row)) if row is not None else None

    def update(self, session: Session) -> Session:
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?, provider = ?, model = ?, workspace = ?, context_tokens = ?
                WHERE id = ?
                """,
                (
                    session.updated_at.isoformat(),
                    session.provider,
                    session.model,
                    session.workspace,
                    session.context_tokens,
                    session.id,
                ),
            )
            connection.commit()
        return session

    def rename(
        self, session_id: str, title: str, only_if_default: bool
    ) -> Session | None:
        query = "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?"
        params = [title, datetime.now(timezone.utc).isoformat(), session_id]
        if only_if_default:
            query += " AND title = ?"
            params.append(DEFAULT_SESSION_TITLE)
        with self._database.connect() as connection:
            connection.execute(query, params)
            connection.commit()
        return self.get(session_id)

    def delete(self, session_id: str) -> bool:
        with self._database.connect() as connection:
            connection.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            deleted = connection.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            ).rowcount
            connection.commit()
        return deleted > 0

    def set_context_tokens(self, session_id: str, count: int | None) -> None:
        with self._database.connect() as connection:
            connection.execute(
                "UPDATE sessions SET context_tokens = ?, updated_at = ? WHERE id = ?",
                (count, datetime.now(timezone.utc).isoformat(), session_id),
            )
            connection.commit()

    def list(self) -> list[Session]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [Session.model_validate(dict(row)) for row in rows]
