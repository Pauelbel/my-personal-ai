"""Валидатор — граница безопасности памяти: любой нарушенный пункт контракта отклоняет весь patch."""

import json
from datetime import UTC, datetime

import pytest

from local_agent.memory.models import Message
from local_agent.memory.operations import AddOperation, DeleteOperation, UpdateOperation
from local_agent.memory.validator import MemoryPatchRejected, validate_patch

SESSION = "session-1"
PREFERENCE_ID = "a" * 32
PROJECT_ID = "b" * 32
MANUAL_ID = "c" * 32
DOCUMENTS = {
    "preferences.md": (
        "# Предпочтения\n\n## Рабочий процесс\n\n"
        f"- <!-- memory:id={PREFERENCE_ID} --> Предпочитает небольшие изменения.\n"
        "- Пьёт чай без сахара.\n"
        # Маркер не в начале строки: это ручная запись, а не управляемая.
        f"- Работает по утрам <!-- memory:id={MANUAL_ID} -->\n"
    ),
    "projects.md": f"# Проекты\n\n- <!-- memory:id={PROJECT_ID} --> Разрабатывает Meepo.\n",
    "profile.md": "# Профиль\n",
}


def message(message_id: str, role: str = "user", session_id: str = SESSION) -> Message:
    return Message(
        id=message_id, session_id=session_id, role=role, content="текст", created_at=datetime.now(UTC)
    )


NEW_MESSAGES = [
    message("user-1"),
    message("assistant-1", role="assistant"),
    message("tool-1", role="tool"),
    message("user-2"),
]


def add(content: str = "Любит короткие ответы.", **fields) -> dict:
    return {
        "op": "add", "file": "preferences.md", "section": "Общение",
        "content": content, "source_message_ids": ["user-1"], **fields,
    }


def update(entry_id: str = PREFERENCE_ID, file: str = "preferences.md", **fields) -> dict:
    return {
        "op": "update", "file": file, "entry_id": entry_id,
        "content": "Предпочитает небольшие проверяемые изменения.", "source_message_ids": ["user-2"], **fields,
    }


def delete(entry_id: str = PROJECT_ID, file: str = "projects.md", **fields) -> dict:
    return {"op": "delete", "file": file, "entry_id": entry_id, "source_message_ids": ["user-1"], **fields}


def validate(operations: list | None = None, *, raw: str | None = None, new_messages=NEW_MESSAGES):
    if raw is None:
        raw = json.dumps({"version": 1, "operations": operations or []}, ensure_ascii=False)
    return validate_patch(raw, session_id=SESSION, new_messages=new_messages, documents=DOCUMENTS)


def rejected(operations: list | None = None, **options) -> MemoryPatchRejected:
    with pytest.raises(MemoryPatchRejected) as error:
        validate(operations, **options)
    return error.value


def test_empty_patch_is_valid() -> None:
    assert validate([]) == []


def test_valid_add_update_delete() -> None:
    operations = validate([add(), update(), delete()])

    assert [type(operation) for operation in operations] == [AddOperation, UpdateOperation, DeleteOperation]
    assert operations[1].entry_id == PREFERENCE_ID
    assert operations[2].file == "projects.md"


def test_patch_in_code_fence_is_accepted() -> None:
    raw = "```json\n" + json.dumps({"version": 1, "operations": [add()]}) + "\n```"

    assert len(validate(raw=raw)) == 1


def test_null_in_foreign_field_counts_as_absent() -> None:
    assert len(validate([add(entry_id=None)])) == 1


@pytest.mark.parametrize("raw", ["not json", "{\"version\": 1, \"operations\": [", "[]", "null"])
def test_invalid_json_is_rejected(raw: str) -> None:
    error = rejected(raw=raw)

    assert error.index is None


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_unsupported_version_is_rejected(version) -> None:
    error = rejected(raw=json.dumps({"version": version, "operations": []}))

    assert error.index is None
    assert "верси" in error.reason


def test_missing_version_is_rejected() -> None:
    assert rejected(raw=json.dumps({"operations": []})).index is None


@pytest.mark.parametrize("op", ["replace", "ADD", None, 1])
def test_unknown_operation_is_rejected(op) -> None:
    error = rejected([{**add(), "op": op}])

    assert error.index == 0
    assert "неизвестная операция" in error.reason


def test_extra_patch_field_is_rejected() -> None:
    error = rejected(raw=json.dumps({"version": 1, "operations": [], "comment": "x"}))

    assert error.index is None
    assert "comment" in error.reason


@pytest.mark.parametrize("operation", [
    add(comment="почему"),
    add(entry_id=PREFERENCE_ID),
    update(section="Общение"),
    delete(content="старый текст"),
    delete(path="../projects.md"),
])
def test_extra_operation_field_is_rejected(operation: dict) -> None:
    error = rejected([operation])

    assert error.index == 0
    assert "лишнее поле" in error.reason


@pytest.mark.parametrize("file", ["notes.md", "../profile.md", "memory/profile.md", "profile.txt", "Profile.md"])
def test_file_outside_memory_is_rejected(file: str) -> None:
    assert rejected([add(file=file)]).index == 0


@pytest.mark.parametrize("sources", [None, []])
def test_operation_without_sources_is_rejected(sources) -> None:
    operation = add()
    if sources is None:
        del operation["source_message_ids"]
    else:
        operation["source_message_ids"] = sources

    assert rejected([operation]).index == 0


def test_source_from_other_session_is_rejected() -> None:
    error = rejected(
        [add(source_message_ids=["foreign-1"])],
        new_messages=[*NEW_MESSAGES, message("foreign-1", session_id="session-2")],
    )

    assert error.index == 0
    assert "другой сессии" in error.reason


@pytest.mark.parametrize("source", ["assistant-1", "tool-1"])
def test_source_not_from_user_is_rejected(source: str) -> None:
    error = rejected([add(source_message_ids=[source])])

    assert error.index == 0
    assert "не пользователь" in error.reason


@pytest.mark.parametrize("sources", [["before-checkpoint"], ["user-1", "before-checkpoint"]])
def test_source_outside_new_messages_is_rejected(sources: list[str]) -> None:
    error = rejected([add(source_message_ids=sources)])

    assert error.index == 0
    assert "после checkpoint" in error.reason


@pytest.mark.parametrize("operation", [update(entry_id="d" * 32), delete(entry_id="d" * 32)])
def test_unknown_entry_id_is_rejected(operation: dict) -> None:
    error = rejected([operation])

    assert error.index == 0
    assert "нет" in error.reason


@pytest.mark.parametrize("operation", [
    update(entry_id=PROJECT_ID, file="preferences.md"),
    delete(entry_id=PREFERENCE_ID, file="projects.md"),
])
def test_entry_in_other_file_is_rejected(operation: dict) -> None:
    error = rejected([operation])

    assert error.index == 0
    assert "находится в" in error.reason


@pytest.mark.parametrize("operation", [update(entry_id=MANUAL_ID), delete(entry_id=MANUAL_ID, file="preferences.md")])
def test_manual_entry_is_not_available(operation: dict) -> None:
    error = rejected([operation])

    assert error.index == 0
    assert "ручные записи" in error.reason


def test_entry_duplicated_across_files_is_rejected() -> None:
    documents = {**DOCUMENTS, "profile.md": f"# Профиль\n\n- <!-- memory:id={PROJECT_ID} --> Копия.\n"}

    with pytest.raises(MemoryPatchRejected) as error:
        validate_patch(
            json.dumps({"version": 1, "operations": [delete()]}),
            session_id=SESSION, new_messages=NEW_MESSAGES, documents=documents,
        )
    assert error.value.index == 0


def test_same_entry_twice_in_patch_is_rejected() -> None:
    error = rejected([update(), delete(entry_id=PREFERENCE_ID, file="preferences.md")])

    assert error.index == 1


@pytest.mark.parametrize("content", [
    "Предпочитает небольшие изменения.",
    "  предпочитает   НЕБОЛЬШИЕ изменения. ",
    "Пьёт чай без сахара.",
    "Разрабатывает Meepo.",
])
def test_add_with_existing_text_is_rejected(content: str) -> None:
    error = rejected([add(content=content, file="profile.md")])

    assert error.index == 0
    assert "уже есть" in error.reason


def test_add_duplicate_inside_patch_is_rejected() -> None:
    error = rejected([add(), add(file="profile.md")])

    assert error.index == 1


@pytest.mark.parametrize("bad_index", [0, 1, 2])
def test_one_invalid_operation_rejects_whole_patch_with_its_index(bad_index: int) -> None:
    operations = [add(), update(), delete()]
    operations[bad_index] = {**operations[bad_index], "file": "notes.md"}

    error = rejected(operations)

    assert error.index == bad_index
    assert f"операция {bad_index}" in str(error)


def test_validator_does_not_change_documents() -> None:
    snapshot = dict(DOCUMENTS)

    validate([add(), update(), delete()])
    rejected([add(), update(entry_id="d" * 32)])

    assert DOCUMENTS == snapshot
