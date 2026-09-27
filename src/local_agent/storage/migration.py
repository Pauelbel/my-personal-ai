"""Миграция копирует SQLite-историю в JSONL и проверяет результат без удаления источника."""

from __future__ import annotations

import json
import os
from uuid import uuid4

from local_agent.sessions.models import Session
from local_agent.storage.json.sessions import JsonSessionRepository
from local_agent.storage.json.tool_settings import JsonToolSettings
from local_agent.storage.jsonl.conversation import JsonlConversationStore
from local_agent.storage.sqlite.memory import SQLiteConversationStore
from local_agent.storage.sqlite.sessions import SQLiteSessionRepository
from local_agent.storage.sqlite.tool_settings import SQLiteToolSettings


class ConversationMigration:
    def __init__(
        self,
        source: SQLiteConversationStore,
        target: JsonlConversationStore,
    ) -> None:
        self._source = source
        self._target = target
        self._marker = target.root / ".sqlite-migration.json"

    def run(
        self,
        sessions: list[Session],
        active_sessions: list[Session] | None = None,
    ) -> None:
        self._target.initialize()
        if self._marker.exists():
            self._validate_marker(active_sessions or sessions)
            return

        migrated: dict[str, dict[str, object]] = {}
        for session in sessions:
            source_messages = self._source.list(session.id)
            target_messages = self._target.list(session.id)
            if target_messages and target_messages != source_messages:
                raise ValueError(
                    f"JSONL archive for session {session.id} differs from SQLite"
                )
            if source_messages and not target_messages:
                self._target.replace(session.id, source_messages)
                target_messages = self._target.list(session.id)
            if target_messages != source_messages:
                raise ValueError(
                    f"JSONL migration verification failed for session {session.id}"
                )
            migrated[session.id] = {
                "message_count": len(source_messages),
                "last_message_id": source_messages[-1].id if source_messages else None,
            }

        payload = json.dumps(
            {"version": 1, "sessions": migrated},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temporary = self._marker.with_name(f".{self._marker.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self._marker)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_marker(self, sessions: list[Session]) -> None:
        data = json.loads(self._marker.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
            raise ValueError("Invalid SQLite migration marker")
        current_ids = {session.id for session in sessions}
        for session_id, checkpoint in data["sessions"].items():
            if session_id not in current_ids:
                continue
            if not isinstance(checkpoint, dict):
                raise ValueError("Invalid SQLite migration checkpoint")
            messages = self._target.list(session_id)
            if len(messages) != checkpoint.get("message_count"):
                raise ValueError("JSONL archive differs from the migration checkpoint")
            last_id = messages[-1].id if messages else None
            if last_id != checkpoint.get("last_message_id"):
                raise ValueError("JSONL archive differs from the migration checkpoint")


class ApplicationDataMigration:
    def __init__(
        self,
        source_sessions: SQLiteSessionRepository,
        target_sessions: JsonSessionRepository,
        source_tools: SQLiteToolSettings,
        target_tools: JsonToolSettings,
    ) -> None:
        self._source_sessions = source_sessions
        self._target_sessions = target_sessions
        self._source_tools = source_tools
        self._target_tools = target_tools
        self._marker = target_sessions.root / ".sqlite-migration.json"

    def run(self) -> list[Session]:
        source_sessions = self._source_sessions.list()
        self._target_sessions.initialize()
        if self._marker.exists():
            self._validate_marker()
            return source_sessions

        self._target_sessions.replace_all(source_sessions)
        if not self._target_tools.path.exists():
            self._target_tools.replace(self._source_tools.enabled_ids())
        if self._target_sessions.list() != source_sessions:
            raise ValueError("Session migration verification failed")
        if self._target_tools.enabled_ids() != self._source_tools.enabled_ids():
            raise ValueError("Tool settings migration verification failed")

        payload = json.dumps(
            {
                "version": 1,
                "session_count": len(source_sessions),
                "enabled_tools": sorted(self._source_tools.enabled_ids()),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temporary = self._marker.with_name(f".{self._marker.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self._marker)
        finally:
            temporary.unlink(missing_ok=True)
        return source_sessions

    def _validate_marker(self) -> None:
        data = json.loads(self._marker.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("session_count"), int):
            raise ValueError("Invalid application data migration marker")
