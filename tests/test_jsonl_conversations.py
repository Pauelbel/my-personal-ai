"""Проверки JSONL подтверждают формат архива и безопасную миграцию из SQLite."""

import json
import sqlite3

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.memory.conversation import ConversationService
from local_agent.sessions.service import SessionService
from local_agent.storage.database import SQLiteDatabase
from local_agent.storage.jsonl.conversation import JsonlConversationStore
from local_agent.storage.sqlite.memory import SQLiteConversationStore
from local_agent.storage.sqlite.sessions import SQLiteSessionRepository


def test_jsonl_store_uses_one_file_per_session(tmp_path) -> None:
    store = JsonlConversationStore(tmp_path / "conversations")
    conversation = ConversationService(store)

    first = conversation.add_user_message("session-1", "Привет")
    second = conversation.add_assistant_message("session-1", "Здравствуйте")

    assert store.count("session-1") == 2
    assert store.list("session-1") == [first, second]
    assert store.recent("session-1", 1) == [second]
    assert store.after("session-1", first.id) == [second]

    lines = (tmp_path / "conversations" / "session-1.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    records = [json.loads(line) for line in lines]
    assert [record["role"] for record in records] == ["user", "assistant"]
    assert all("timestamp" in record and "created_at" not in record for record in records)

    store.delete("session-1")
    assert not (tmp_path / "conversations" / "session-1.jsonl").exists()


def test_app_migrates_sqlite_messages_and_keeps_source(tmp_path) -> None:
    database_path = tmp_path / "agent.sqlite3"
    database = SQLiteDatabase(database_path)
    database.initialize()
    repository = SQLiteSessionRepository(database)
    sessions = SessionService(repository)
    session = sessions.create(
        title="Старая сессия",
        agent_id="default",
        model="test-model",
        provider="lm_studio",
        workspace=None,
    )
    legacy = ConversationService(SQLiteConversationStore(database))
    first = legacy.add_user_message(session.id, "Старое сообщение")
    second = legacy.add_assistant_message(session.id, "Старый ответ")

    settings = Settings(
        database_path=database_path,
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        tool_settings_path=tmp_path / "settings" / "tools.json",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/sessions/{session.id}/messages")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [first.id, second.id]
    assert (settings.conversations_path / f"{session.id}.jsonl").exists()
    assert (settings.conversations_path / ".sqlite-migration.json").exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2

    with TestClient(create_app(settings)) as client:
        repeated = client.get(f"/api/sessions/{session.id}/messages")
    assert repeated.json() == response.json()
