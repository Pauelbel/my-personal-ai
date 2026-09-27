"""Проверки памяти подтверждают точечные Markdown-операции и checkpoint сессии."""

import json

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.models import ChatMessage, ChatResult
from local_agent.memory.markdown import MarkdownMemoryStore
from local_agent.memory.operations import MemoryPatch


class MemoryProvider:
    def __init__(self, invalid: bool = False) -> None:
        self.invalid = invalid
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def chat(self, model: str, messages: list[ChatMessage], **options) -> ChatResult:
        self.calls.append((model, messages))
        if self.invalid:
            return ChatResult(content="not json")
        payload = json.loads(messages[-1].content or "{}")
        source_id = next(
            item["id"] for item in payload["new_messages"] if item["role"] == "user"
        )
        return ChatResult(content=json.dumps({
            "version": 1,
            "operations": [{
                "op": "add",
                "file": "preferences.md",
                "section": "Рабочий процесс",
                "content": "Предпочитает небольшие проверяемые изменения.",
                "source_message_ids": [source_id],
            }],
        }))

    async def list_models(self) -> list[str]:
        return ["chat-model", "memory-model"]

    async def close(self) -> None:
        pass


def test_markdown_operations_preserve_manual_entries(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    store.initialize()
    store.write(
        "preferences.md",
        "# Предпочтения\n\n## Ручные\n\n- Эту строку добавил пользователь.\n",
    )
    patch = MemoryPatch.model_validate({
        "version": 1,
        "operations": [{
            "op": "add",
            "file": "preferences.md",
            "section": "Рабочий процесс",
            "content": "Предпочитает небольшие изменения.",
            "source_message_ids": ["message-1"],
        }],
    })

    store.apply(
        patch,
        session_id="session-1",
        last_processed_message_id="message-1",
    )
    content = store.read("preferences.md").content
    entry_id = content.split("memory:id=", 1)[1].split(" -->", 1)[0]
    store.apply(
        MemoryPatch.model_validate({
            "version": 1,
            "operations": [{
                "op": "update",
                "file": "preferences.md",
                "entry_id": entry_id,
                "content": "Предпочитает короткие проверяемые изменения.",
                "source_message_ids": ["message-2"],
            }],
        }),
        session_id="session-1",
        last_processed_message_id="message-2",
    )

    updated = store.read("preferences.md").content
    assert "Эту строку добавил пользователь" in updated
    assert "Предпочитает короткие проверяемые изменения" in updated
    store.apply(
        MemoryPatch.model_validate({
            "version": 1,
            "operations": [{
                "op": "delete",
                "file": "preferences.md",
                "entry_id": entry_id,
                "source_message_ids": ["message-3"],
            }],
        }),
        session_id="session-1",
        last_processed_message_id="message-3",
    )
    deleted = store.read("preferences.md").content
    assert "Эту строку добавил пользователь" in deleted
    assert "Предпочитает короткие проверяемые изменения" not in deleted
    assert store.checkpoint("session-1") == "message-3"


def test_update_memory_uses_only_new_messages_and_memory_model(tmp_path) -> None:
    provider = MemoryProvider()
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        memory_model="memory-model",
        _env_file=None,
    )

    with TestClient(create_app(settings, llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "chat-model"}).json()
        created = client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "Мне удобны небольшие изменения"},
        ).json()
        first = client.post(f"/api/sessions/{session['id']}/memory/update")
        second = client.post(f"/api/sessions/{session['id']}/memory/update")
        documents = client.get("/api/memory").json()
        memory = client.get("/api/memory/preferences.md").json()

    assert first.status_code == 200
    assert first.json() == {
        "processed_messages": 1,
        "applied_operations": 1,
        "last_processed_message_id": created["id"],
    }
    assert second.json()["processed_messages"] == 0
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == "memory-model"
    assert any(item["name"] == "preferences.md" for item in documents)
    assert "Предпочитает небольшие проверяемые изменения" in memory["content"]


def test_invalid_model_response_does_not_advance_checkpoint(tmp_path) -> None:
    provider = MemoryProvider(invalid=True)
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )

    with TestClient(create_app(settings, llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "chat-model"}).json()
        client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "Устойчивое предпочтение"},
        )
        first = client.post(f"/api/sessions/{session['id']}/memory/update")
        second = client.post(f"/api/sessions/{session['id']}/memory/update")

    assert first.status_code == 422
    assert second.status_code == 422
    assert len(provider.calls) == 2
    assert provider.calls[0][0] == "chat-model"


def test_update_requires_memory_or_session_model(tmp_path) -> None:
    provider = MemoryProvider()
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )

    with TestClient(create_app(settings, llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": ""}).json()
        client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "Устойчивое предпочтение"},
        )
        response = client.post(f"/api/sessions/{session['id']}/memory/update")

    assert response.status_code == 422
    assert "MEMORY_MODEL" in response.json()["detail"]
    assert provider.calls == []
