"""Память: remember, выбор строк для промпта, автообновление по новым сообщениям, ручная правка через API."""

import json

from fastapi.testclient import TestClient

from meepo.app import create_app
from meepo.llm.fake import FakeLLM
from meepo.memory.store import MemoryStore


def settings(h, **values):
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text(json.dumps(values), encoding="utf-8")


def operations(messages, *ops):
    """Ответ модели памяти: операции со ссылкой на последнюю реплику пользователя из запроса."""
    request = json.loads(messages[-1]["content"])
    user_id = [item["id"] for item in request["new_messages"] if item["role"] == "user"][-1]
    return json.dumps({"operations": [{**op, "source_message_ids": [user_id]} for op in ops]}, ensure_ascii=False)


def test_name_is_remembered_and_known_in_new_session(harness):
    h = harness([
        "Приятно познакомиться, Павел!",
        lambda messages: operations(messages, {"op": "add", "file": "profile.md", "section": "Обращение", "content": "Зовут Павел"}),
        "Вас зовут Павел.",
        '{"operations": []}',
    ])
    settings(h, memory_auto_update_messages=1)
    h.turn("меня зовут Павел")
    profile = h.services.memory.read("profile.md")
    assert "## Обращение\n\n- Зовут Павел" in profile
    memory_request = h.llm.requests[1]
    assert memory_request["response_format"]["json_schema"]["name"] == "memory"
    # Новая сессия: имя уже в системном промпте.
    h.session = h.services.sessions.create(h.project["id"])
    h.turn("как меня зовут?")
    assert "- Зовут Павел" in h.llm.requests[-2]["messages"][0]["content"]


def test_update_skips_bad_operations_and_moves_checkpoint(harness):
    h = harness([
        "Ок.",
        lambda messages: json.dumps({"operations": [
            {"op": "add", "file": "profile.md", "section": "Роль", "content": "QA-инженер",
             "source_message_ids": [json.loads(messages[-1]["content"])["new_messages"][0]["id"]]},
            {"op": "add", "file": "secret.md", "section": "x", "content": "y", "source_message_ids": ["нет"]},
            {"op": "delete", "file": "profile.md", "old_content": "нет такой строки", "source_message_ids": ["нет"]},
        ]}),
    ])
    settings(h, memory_auto_update_messages=1)
    h.turn("я QA-инженер")
    assert "- QA-инженер" in h.services.memory.read("profile.md")
    assert h.services.memory.checkpoint(h.sid) == h.messages()[-1]["id"]
    # Новых реплик нет — модель не зовётся.
    import asyncio
    result = asyncio.run(h.services.memory_updater.update(h.sid))
    assert result == {"processed": 0, "applied": 0, "skipped": []}


def test_auto_update_waits_for_threshold(harness):
    h = harness(["Ок.", "Ок."])
    settings(h, memory_auto_update_messages=3)
    h.turn("раз")
    h.turn("два")
    assert len(h.llm.requests) == 2


def test_remember_tool(harness):
    h = harness([
        {"calls": [("remember", {"file": "preferences.md", "section": "Тест-кейсы", "content": "Оформлять таблицей"})]},
        {"calls": [("remember", {"file": "preferences.md", "section": "Тест-кейсы", "content": "Оформлять таблицей"}),
                   ("remember", {"file": "nope.md", "section": "x", "content": "y"})]},
        "Запомнил.",
    ])
    h.turn("запомни: тест-кейсы оформляй таблицей")
    assert h.cards() == []
    assert "- Оформлять таблицей" in h.services.memory.read("preferences.md")
    duplicate, missing = [m for m in h.messages() if m["role"] == "tool"][1:]
    assert "уже есть" in duplicate["content"] and "Файла памяти nope.md нет" in missing["content"]


def test_prompt_context_profile_always_rest_by_words(tmp_path):
    store = MemoryStore(tmp_path)
    store.initialize()
    store.add("profile.md", "Обращение", "- Зовут Павел")
    store.add("preferences.md", "Ответы", "Коротко и по делу")
    store.add("projects.md", "Работа", "Автотесты на Playwright для портала заказов")
    store.add("solutions.md", "Jira", "Задачи из Jira — через скилл jira-search")
    context = store.prompt_context("напиши автотесты для портала")
    assert "- Зовут Павел (Обращение)" in context and "- Коротко и по делу (Ответы)" in context
    assert "Playwright" in context and "Jira" not in context


def test_memory_api_edit(tmp_path):
    app = create_app(tmp_path_env(tmp_path), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        names = [item["name"] for item in client.get("/api/memory").json()]
        assert names == ["preferences.md", "profile.md", "projects.md", "solutions.md"]
        saved = client.put("/api/memory/profile.md", json={"content": "# Профиль\n\n- Зовут Павел"}).json()
        assert saved["content"] == "# Профиль\n\n- Зовут Павел\n"
        assert client.put("/api/memory/../x.md", json={"content": ""}).status_code in (400, 404)


def tmp_path_env(tmp_path):
    from meepo.settings import Env
    return Env(data_path=tmp_path, default_model="fake")
