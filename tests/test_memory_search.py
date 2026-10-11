"""Память в промпте: короткий профиль и записи, найденные по сообщению, а не все файлы целиком."""

from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.memory.search import entry_chunks

MEMORY = {
    "profile.md": "# Профиль\n\n## Имя\n\n- Зовут Павел\n",
    "preferences.md": "# Предпочтения\n\n## Код\n\n- Любит короткие функции на Python\n\n## Еда\n\n- Пьёт кофе без сахара\n",
}


def test_entries_are_chunked_by_section():
    assert entry_chunks(MEMORY["preferences.md"], "x") == [
        ("Код", 5, "Любит короткие функции на Python"), ("Еда", 9, "Пьёт кофе без сахара"),
    ]


def test_prompt_gets_profile_and_only_relevant_entries(tmp_path):
    provider = ScriptedProvider("Ок")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        for name, content in MEMORY.items():
            client.app.state.memory_service.write_document(name, content)
        session = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Напиши функцию на Python"})

    prompt = provider.system_prompt()
    assert "Зовут Павел" in prompt and "Любит короткие функции на Python" in prompt
    assert "кофе" not in prompt


def test_remember_writes_through_patch_contract_and_is_used_next_time(tmp_path):
    provider = ScriptedProvider(
        call("remember", file="profile.md", section="Имя", content="Зовут Павел"), "Приятно познакомиться",
        "Вас зовут Павел",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        first = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{first['id']}/turns/stream", json={"content": "Привет, я Павел"}))
        duplicate = client.app.state.memory_service
        second = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{second['id']}/turns/stream", json={"content": "Как меня зовут?"})
        profile = client.get("/api/memory/profile.md").json()["content"]

    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert not result["is_error"] and "Зовут Павел" in profile
    assert "Зовут Павел" in provider.system_prompt()
    # Повтор того же факта отклоняет валидатор patch-контракта.
    try:
        duplicate.remember(first["id"], "profile.md", "Имя", "Зовут Павел")
    except Exception as exc:  # noqa: BLE001
        assert "уже есть" in str(exc)
    else:
        raise AssertionError("дубликат записан")
