"""Проверки памяти подтверждают точечные Markdown-операции и checkpoint сессии."""

import json

import pytest
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
            "version": 2,
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


class UpdatingMemoryProvider(MemoryProvider):
    async def chat(self, model: str, messages: list[ChatMessage], **options) -> ChatResult:
        self.calls.append((model, messages))
        payload = json.loads(messages[-1].content or "{}")
        source_id = next(item["id"] for item in payload["new_messages"] if item["role"] == "user")
        return ChatResult(content=json.dumps({
            "version": 2,
            "operations": [{
                "op": "update", "file": "preferences.md",
                "old_content": "Обращаться к пользователю дружище",
                "content": "Обращаться к пользователю по имени",
                "source_message_ids": [source_id],
            }],
        }))


def test_markdown_operations_preserve_manual_entries(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    store.initialize()
    store.write(
        "preferences.md",
        "# Предпочтения\n\n## Ручные\n\n- Эту строку добавил пользователь.\n",
    )
    patch = MemoryPatch.model_validate({
        "version": 2,
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
    assert "memory:id=" not in content
    store.apply(
        MemoryPatch.model_validate({
            "version": 2,
            "operations": [{
                "op": "update",
                "file": "preferences.md",
                "old_content": "Предпочитает небольшие изменения.",
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
            "version": 2,
            "operations": [{
                "op": "delete",
                "file": "preferences.md",
                "old_content": "Предпочитает короткие проверяемые изменения.",
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


def test_memory_service_updates_existing_line_without_id(tmp_path) -> None:
    provider = UpdatingMemoryProvider()
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        memory_auto_update_messages=0,
        _env_file=None,
    )
    with TestClient(create_app(settings, llm_provider=provider)) as client:
        client.put("/api/memory/preferences.md", json={
            "content": "# Предпочтения\n\n- Обращаться к пользователю дружище\n",
        })
        session = client.post("/api/sessions", json={"model": "chat-model"}).json()
        client.post(f"/api/sessions/{session['id']}/messages", json={"content": "Теперь обращайся по имени"})
        response = client.post(f"/api/sessions/{session['id']}/memory/update")
        content = client.get("/api/memory/preferences.md").json()["content"]

    assert response.status_code == 200
    assert response.json()["applied_operations"] == 1
    assert "- Обращаться к пользователю по имени\n" in content
    assert "дружище" not in content
    assert "memory:id=" not in content


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


class PartlyInvalidProvider(MemoryProvider):
    """Две корректные операции и третья в чужой файл: patch должен быть отклонён целиком."""

    async def chat(self, model: str, messages: list[ChatMessage], **options) -> ChatResult:
        self.calls.append((model, messages))
        payload = json.loads(messages[-1].content or "{}")
        source = [item["id"] for item in payload["new_messages"] if item["role"] == "user"]
        return ChatResult(content=json.dumps({
            "version": 2,
            "operations": [
                {"op": "add", "file": "preferences.md", "section": "Общение",
                 "content": "Любит короткие ответы.", "source_message_ids": source},
                {"op": "add", "file": "projects.md", "section": "Текущие",
                 "content": "Делает Meepo.", "source_message_ids": source},
                {"op": "add", "file": "notes.md", "section": "Разное",
                 "content": "Что-то ещё.", "source_message_ids": source},
            ],
        }))


def test_rejected_patch_changes_nothing_and_keeps_checkpoint(tmp_path) -> None:
    provider = PartlyInvalidProvider()
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )

    with TestClient(create_app(settings, llm_provider=provider)) as client:
        before = {item["name"]: client.get(f"/api/memory/{item['name']}").json()["content"]
                  for item in client.get("/api/memory").json()}
        session = client.post("/api/sessions", json={"model": "chat-model"}).json()
        client.post(f"/api/sessions/{session['id']}/messages", json={"content": "Отвечай коротко"})
        first = client.post(f"/api/sessions/{session['id']}/memory/update")
        second = client.post(f"/api/sessions/{session['id']}/memory/update")
        after = {item["name"]: client.get(f"/api/memory/{item['name']}").json()["content"]
                 for item in client.get("/api/memory").json()}

    assert first.status_code == 422
    assert "операция 2" in first.json()["detail"]
    assert after == before
    assert not (tmp_path / "memory" / "notes.md").exists()
    # Checkpoint не сдвинулся: второй запуск снова отправляет модели то же сообщение.
    assert second.status_code == 422
    assert len(provider.calls) == 2
    assert provider.calls[0][1][-1].content == provider.calls[1][1][-1].content


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


def test_pending_update_recovers_files_checkpoint_and_log(tmp_path, monkeypatch) -> None:
    root = tmp_path / "memory"
    store = MarkdownMemoryStore(root)
    store.initialize()
    patch = MemoryPatch.model_validate({
        "version": 2,
        "operations": [
            {"op": "add", "file": "preferences.md", "section": "Общение",
             "content": "Отвечать кратко", "source_message_ids": ["message-1"]},
            {"op": "add", "file": "projects.md", "section": "Текущие",
             "content": "Развивает Meepo", "source_message_ids": ["message-1"]},
        ],
    })
    original_write = store._atomic_write

    def interrupted(path, content):
        if path.name == "projects.md":
            raise OSError("Прерванная запись")
        original_write(path, content)

    monkeypatch.setattr(store, "_atomic_write", interrupted)
    with pytest.raises(OSError):
        store.apply(patch, session_id="session-1", last_processed_message_id="message-1")
    assert (root / ".pending.json").exists()

    recovered = MarkdownMemoryStore(root)
    recovered.initialize()
    assert "Отвечать кратко" in recovered.read("preferences.md").content
    assert "Развивает Meepo" in recovered.read("projects.md").content
    assert recovered.checkpoint("session-1") == "message-1"
    assert not (root / ".pending.json").exists()
    events = [json.loads(line) for line in (root / ".updates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["event"]["kind"] == "model_update"
    assert "Отвечать кратко" not in json.dumps(events, ensure_ascii=False)


def test_recovery_does_not_overwrite_manual_change(tmp_path, monkeypatch) -> None:
    root = tmp_path / "memory"
    store = MarkdownMemoryStore(root)
    store.initialize()
    patch = MemoryPatch.model_validate({
        "version": 2,
        "operations": [{"op": "add", "file": "preferences.md", "section": "Общение",
                        "content": "Отвечать кратко", "source_message_ids": ["message-1"]}],
    })
    original_write = store._atomic_write

    def interrupted(path, content):
        if path.name == ".state.json":
            raise OSError("Прерванная запись")
        original_write(path, content)

    monkeypatch.setattr(store, "_atomic_write", interrupted)
    with pytest.raises(OSError):
        store.apply(patch, session_id="session-1", last_processed_message_id="message-1")
    path = root / "preferences.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n- Ручная правка\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ручная проверка"):
        MarkdownMemoryStore(root).initialize()
    assert "Ручная правка" in path.read_text(encoding="utf-8")
    assert (root / ".pending.json").exists()


def test_manual_edit_has_technical_log_without_content(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    store.write("preferences.md", "# Предпочтения\n\n- Секретная ручная запись\n")
    event = json.loads(store.log_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event"] == {"kind": "manual_edit", "files": ["preferences.md"]}
    assert "Секретная" not in json.dumps(event, ensure_ascii=False)


def test_legacy_ids_are_removed_without_changing_text_or_checkpoint(tmp_path) -> None:
    root = tmp_path / "memory"
    root.mkdir()
    (root / "preferences.md").write_text(
        "# Предпочтения\n\n## Общение\n\n"
        "- <!-- memory:id=508bc36068c154d39e8b3a9f78457e52 --> Обращаться к пользователю дружище\n"
        "- Ручная запись\n",
        encoding="utf-8",
    )
    (root / ".state.json").write_text(
        json.dumps({"version": 1, "sessions": {"session-1": {"last_processed_message_id": "message-1"}}}),
        encoding="utf-8",
    )

    store = MarkdownMemoryStore(root)
    store.initialize()
    content = store.read("preferences.md").content
    assert "- Обращаться к пользователю дружище\n" in content
    assert "- Ручная запись\n" in content
    assert "memory:id=" not in content
    assert store.checkpoint("session-1") == "message-1"
    store.initialize()
    events = [json.loads(line) for line in store.log_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["event"]["kind"] == "remove_legacy_ids"


def test_concurrent_manual_edit_is_not_overwritten(tmp_path, monkeypatch) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    store.initialize()
    patch = MemoryPatch.model_validate({
        "version": 2,
        "operations": [{"op": "add", "file": "preferences.md", "section": "Общение",
                        "content": "Отвечать кратко", "source_message_ids": ["message-1"]}],
    })
    original_commit = store._commit

    def concurrent_edit(targets, event, *, expected_before=None):
        path = store.root / "preferences.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n- Ручная правка\n", encoding="utf-8")
        original_commit(targets, event, expected_before=expected_before)

    monkeypatch.setattr(store, "_commit", concurrent_edit)
    with pytest.raises(ValueError, match="изменился во время обновления"):
        store.apply(patch, session_id="session-1", last_processed_message_id="message-1")
    assert "Ручная правка" in store.read("preferences.md").content
    assert "Отвечать кратко" not in store.read("preferences.md").content
    assert store.checkpoint("session-1") is None
    assert not store.pending_path.exists()
