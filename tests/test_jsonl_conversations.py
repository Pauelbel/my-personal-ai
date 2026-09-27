"""Проверки JSONL подтверждают формат архива диалогов."""

import json

from local_agent.memory.conversation import ConversationService
from local_agent.storage.jsonl.conversation import JsonlConversationStore


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
