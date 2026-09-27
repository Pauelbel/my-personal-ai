"""Обёртка SQLite задаёт единые параметры соединений и создаёт схему при запуске."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from local_agent.storage.schema import MESSAGES_SCHEMA, SESSIONS_SCHEMA, TOOL_SETTINGS_SCHEMA


class SQLiteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SESSIONS_SCHEMA + MESSAGES_SCHEMA + TOOL_SETTINGS_SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            if "context_tokens" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN context_tokens INTEGER")
            connection.commit()
