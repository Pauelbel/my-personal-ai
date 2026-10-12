"""Ход на графе: компактный реестр, activate, interrupt вместо подтверждения, resume после перезапуска."""

from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def settings(tmp_path) -> Settings:
    return Settings(
        agents_path=tmp_path / "agents", skills_path=tmp_path / "skills", sessions_path=tmp_path / "sessions",
        projects_path=tmp_path / "projects", conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory", memory_auto_update_messages=0,
        mcp_enabled=False, _env_file=None,
    )


def project_session(client, workspace) -> dict:
    project = client.post("/api/projects", json={"name": "Проект", "workspace": str(workspace)}).json()
    return post_session(client, {"model": "test", "project_id": project["id"]}).json()


def workspace_with_note(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_text("Встреча в пятницу", encoding="utf-8")
    return workspace


def test_registry_is_compact_and_activate_binds_schemas(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("activate", names=["git"]), "Готово", "Ещё раз")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Что в git?"}))
        project = client.get("/api/projects").json()
        other = post_session(client, {"model": "test", "project_id": session["project_id"]}).json()
        client.post(f"/api/sessions/{other['id']}/turns/stream", json={"content": "Снова"})

    assert events[-1]["type"] == "done"
    # Сначала схемы базовых инструментов и чтения файлов, остальные — строкой реестра.
    first = provider.tool_names(0)
    assert {"activate", "remember", "propose", "list_files", "read_file"} <= set(first) and "git" not in first
    assert "- git — " in provider.system_prompt(0) and "Скиллов пока нет" in provider.system_prompt(0)
    assert "git" in provider.tool_names(1)
    # Подключённое запоминается на проект: новая сессия начинает с него.
    assert next(item for item in project if item["id"] == session["project_id"])["active_tools"] == ["list_files", "read_file", "git"]
    assert "git" in provider.tool_names(-1)


def test_answer_keeps_turn_summary(tmp_path):
    provider = ScriptedProvider(call("activate", names=["git"]), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Подключи git"}))
        history = client.get(url + "/messages").json()

    stats = events[-1]["message"]["stats"]
    # Два запроса к модели: вызов инструмента и ответ; токены считает только тот, о котором сервер их сообщил.
    assert stats["rounds"] == 2 and stats["input_tokens"] == 100 and stats["output_tokens"] == 5
    assert stats["seconds"] >= 0
    assert history[-1]["stats"] == stats
    assert all(message["stats"] is None for message in history[:-1])


def test_read_only_project_hides_tools_that_change_files(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="x"), "Не могу писать")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        client.put(f"/api/projects/{session['project_id']}", json={"name": "Проект", "workspace": str(workspace), "mode": "read"})
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Запиши"}))

    assert events[-1]["type"] == "done" and not (workspace / "out.txt").exists()
    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert result["is_error"] and "Инструмента «write_file» нет" in result["content"]
    assert "только для чтения" in provider.system_prompt(0)
    assert "- write_file" not in provider.system_prompt(0)


def test_retry_does_not_duplicate_the_unanswered_message(tmp_path):
    provider = ScriptedProvider("", "", "", "Ответ")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        failed = events_of(client.post(url + "/turns/stream", json={"content": "Привет"}))
        retried = events_of(client.post(url + "/turns/stream", json={"content": "Привет", "retry": True}))
        history = client.get(url + "/messages").json()

    assert failed[-1]["type"] == "error" and retried[-1]["type"] == "done"
    assert [message["role"] for message in history] == ["user", "assistant"]


def test_write_waits_for_decision_and_survives_restart(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="итог"), "Записал")
    configured = settings(tmp_path)
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Запиши итог"}))
        assert events[-1]["type"] == "interrupt"
        assert events[-1]["pending"]["call"]["name"] == "write_file"
        assert not (workspace / "out.txt").exists()

    # Сервер перезапустили: карточка на месте, решение продолжает тот же ход.
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        assert client.get(url).json()["pending"]["kind"] == "tool_call"
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))
        assert events[-1]["type"] == "done"
        assert client.get(url).json()["pending"] is None
        assert client.post(url + "/resume", json={"action": "approve"}).status_code == 409
        history = client.get(url + "/messages").json()

    assert (workspace / "out.txt").read_text(encoding="utf-8") == "итог"
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]


def test_message_while_waiting_is_a_reply_to_the_card(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="итог"), "Хорошо, сначала план")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Запиши итог"})
        events = events_of(client.post(url + "/turns/stream", json={"content": "Нет, сначала покажи план"}))
        history = client.get(url + "/messages").json()

    assert [event["type"] for event in events if event["type"] in {"tool_result", "user_message", "done"}] == [
        "tool_result", "user_message", "done",
    ]
    assert not (workspace / "out.txt").exists()
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "user", "assistant"]
    assert "ответил" in history[2]["content"] and history[3]["content"] == "Нет, сначала покажи план"
    # Модель видит и отказ, и реплику пользователя после него.
    assert [message.role for message in provider.requests[-1]["messages"][-2:]] == ["tool", "user"]


def test_missing_capability_is_reported_not_invented(tmp_path):
    provider = ScriptedProvider(call("search_jira", query="баги"), "Инструмента для Jira нет, могу предложить создать")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Найди баги"}))

    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert result["is_error"] and "propose" in result["content"]


def test_incomplete_call_is_returned_to_model_before_the_card(tmp_path):
    edit = {"kind": "fix", "notes": ["заметка.md"], "problem": "Опечатка", "change": "Исправить"}
    provider = ScriptedProvider(call("wiki_propose_edits"), call("wiki_propose_edits", edits=[edit]))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.wiki_service.write("заметка.md", "# Заметка\n\nТекст")
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "/lint"}))

    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert "непустой список правок" in result["content"] and "edits (array, обязательный)" in result["content"]
    # Карточку пользователь видит только для исправленного вызова.
    assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["title"].endswith("правок — 1")


def test_empty_model_answer_is_retried(tmp_path):
    provider = ScriptedProvider("", "", "Ответ со второй попытки")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Привет"}))

    assert events[-1]["type"] == "done" and events[-1]["message"]["content"] == "Ответ со второй попытки"
    # Последняя попытка — без инструментов: так модель точно ответит текстом.
    assert provider.requests[-1]["tools"] is None


def test_looping_model_is_cut_and_retried(tmp_path):
    from local_agent.llm.models import ChatResult, ReasoningDelta

    class Looping(ScriptedProvider):
        async def chat_stream(self, model, messages, tools=None, options=None):
            self.requests.append({"model": model, "messages": list(messages), "tools": tools})
            if len(self.requests) == 1:
                while True:
                    yield ReasoningDelta("I'll click Checkout.\nI'll type Ivan.\nI'll click Finish.\nI'll report the result.\n")
            yield "Ответ со второй попытки"
            yield ChatResult(content="Ответ со второй попытки", input_tokens=100, output_tokens=5)

    provider = Looping()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Оформи заказ"}))

    assert {"type": "retry"} in events
    assert events[-1]["type"] == "done" and events[-1]["message"]["content"] == "Ответ со второй попытки"
    assert len(provider.requests) == 2


def test_attention_lists_pending_cards(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="итог"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Запиши"})
        attention = client.get("/api/attention", params={"session_id": session["id"]}).json()

    [card] = attention["cards"]
    assert card["session_id"] == session["id"] and card["pending"]["call"]["name"] == "write_file"
