"""Хранилище сохраняет общие переключатели инструментов между запусками."""

import sqlite3

from local_agent.storage.database import SQLiteDatabase


class SQLiteToolSettings:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def enabled_ids(self) -> set[str]:
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    "SELECT tool_id FROM tool_settings WHERE enabled = 1"
                ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {row[0] for row in rows}

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        with self._database.connect() as connection:
            connection.execute(
                "INSERT INTO tool_settings (tool_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(tool_id) DO UPDATE SET enabled = excluded.enabled",
                (tool_id, int(enabled)),
            )
            connection.commit()
