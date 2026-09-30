"""Команда в сессии: холст агентов, send_message, очередь поручений, её границы и память."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from local_agent.agent.loader import load_agents
from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.models import ChatResult, ToolCall
from local_agent.team.models import Canvas
from local_agent.team.validator import canvas_errors
from local_agent.tools.messaging import SendMessageTool

AGENTS = {
    "default": "name: Основной агент\nmodel: main-model\ntools: read_file",
    "qa": "name: QA\ndescription: Проектирует тестирование\nmodel: qa-model\ntools: read_file\nskills: check",
    "aqa": "name: AQA\ndescription: Пишет автотесты\nmodel: aqa-model\ntools: read_file, write_file\nskills:",
}
SKILL = "---\nname: Проверка\ndescription: проверить что-нибудь\n---\nШаг 1.\n"
MEMORY_FACT = "Пользователя зовут Тестовый Секрет"
TEAM = {
    "nodes": [
        {"id": "main", "name": "Основной агент"},
        {"id": "qa", "name": "QA", "agent_id": "qa", "x": 300},
        {"id": "aqa", "name": "AQA", "agent_id": "aqa", "x": 600},
    ],
    "edges": [{"from": "main", "to": "qa"}, {"from": "qa", "to": "main"}, {"from": "qa", "to": "aqa"}],
}


def send(to, content):
    return ToolCall(f"call-{to}-{abs(hash(content))}", "send_message", json.dumps({"to": to, "content": content}))


class TeamProvider:
    """Отвечает по сценарию своей модели: каждый вызов забирает следующий шаг."""

    def __init__(self, scripts):
        self.scripts = {model: list(steps) for model, steps in scripts.items()}
        self.calls = []

    async def chat(self, model, messages, tools=None):
        self.calls.append((model, list(messages), tools or []))
        step = self.scripts[model].pop(0)
        if isinstance(step, ToolCall):
            return ChatResult(content=None, tool_calls=(step,))
        return ChatResult(content=step)

    def tools_of(self, model):
        return [{item["function"]["name"] for item in tools} for called, _, tools in self.calls if called == model]

    async def close(self):
        pass


@pytest.fixture
def root(tmp_path):
    (tmp_path / "agents").mkdir()
    for agent_id, header in AGENTS.items():
        (tmp_path / "agents" / f"{agent_id}.md").write_text(f"---\n{header}\n---\nТы {agent_id}.\n", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "check.md").write_text(SKILL, encoding="utf-8")
    (tmp_path / "project").mkdir()
    return tmp_path


def settings(root, **overrides):
    return Settings(
        agents_path=root / "agents", skills_path=root / "skills", sessions_path=root / "sessions",
        projects_path=root / "projects", conversations_path=root / "conversations", memory_path=root / "memory",
        memory_auto_update_messages=0, _env_file=None, **overrides,
    )


def open_session(client, root, canvas=TEAM):
    """Сессия проекта с холстом команды и фактом в личной памяти."""
    session = client.post("/api/sessions", json={"model": "main-model", "workspace": str(root / "project")}).json()
    saved = client.put(f"/api/sessions/{session['id']}/canvas", json=canvas)
    assert saved.status_code == 200 and saved.json()["errors"] == []
    name = client.get("/api/memory").json()[0]["name"]
    client.put(f"/api/memory/{name}", json={"content": MEMORY_FACT})
    return session["id"]


def events_of(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_agent_file_fields(root):
    (root / "agents" / "plain.md").write_text("---\nname: Простой\nmax_tool_rounds: 3\n---\nТекст\n", encoding="utf-8")
    agents = {agent.id: agent for agent in load_agents(root / "agents", "")}

    assert agents["qa"].description == "Проектирует тестирование"
    assert agents["qa"].skills == ("check",)
    # Пустая строка skills — без навыков, отсутствие строки — все навыки.
    assert agents["aqa"].skills == ()
    assert agents["plain"].skills is None and agents["plain"].max_tool_rounds == 3

    (root / "agents" / "bad.md").write_text("---\nmax_tool_rounds: 0\n---\nТекст\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_tool_rounds"):
        load_agents(root / "agents", "")


def test_canvas_validation_reports_broken_references():
    canvas = Canvas.model_validate({
        "nodes": [
            {"id": "main", "name": "Вход"},
            {"id": "a", "name": "A", "agent_id": "ghost"}, {"id": "a", "name": "A2", "agent_id": "qa"},
        ],
        "edges": [{"from": "a", "to": "b"}, {"from": "a", "to": "b"}, {"from": "main", "to": "main"}],
    })
    errors = canvas_errors(canvas, lambda agent_id: agent_id == "qa")

    assert any("Повторяются id узлов: a" in error for error in errors)
    assert any("агент ghost не найден" in error for error in errors)
    assert any("несуществующий узел" in error for error in errors)
    assert any("повторяется" in error for error in errors)
    assert any("в тот же узел" in error for error in errors)
    # Входной карточке агент не нужен: это агент сессии.
    assert not any("Вход" in error for error in errors)


def test_send_message_allows_only_canvas_edges():
    tool = SendMessageTool({"qa": "QA. Проектирует тестирование"})

    allowed = asyncio.run(tool.execute({"to": "qa", "content": " Задача "}, None))
    forbidden = asyncio.run(tool.execute({"to": "aqa", "content": "Задача"}, None))
    empty = asyncio.run(tool.execute({"to": "qa", "content": "  "}, None))

    assert not allowed.is_error and tool.sent == [("qa", "Задача")]
    assert forbidden.is_error and empty.is_error
    assert tool.parameters["properties"]["to"]["enum"] == ["qa"]
    assert "QA. Проектирует тестирование" in tool.description


def test_empty_canvas_is_a_plain_chat(root):
    provider = TeamProvider({"main-model": ["Привет!"]})
    with TestClient(create_app(settings(root), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "main-model"}).json()
        answer = client.post(f"/api/sessions/{session['id']}/turns", json={"content": "Привет"})

    assert answer.status_code == 200 and answer.json()["content"] == "Привет!"
    assert "send_message" not in provider.tools_of("main-model")[0]


@pytest.mark.parametrize("approved", [True, False])
def test_session_agent_delegates_and_gets_results_back(root, approved):
    provider = TeamProvider({
        "main-model": [send("qa", "Спроектируй тесты"), "Передал QA", "Итог: тесты готовы"],
        "qa-model": [send("aqa", "Напиши автотест"), "Жду AQA", "Тест-план готов, автотест от AQA получен", "Привет, я QA"],
        "aqa-model": [
            ToolCall("write", "write_file", json.dumps({"path": "test_login.py", "content": "def test(): pass"})),
            "Автотест записан",
        ],
    })
    with TestClient(create_app(settings(root), llm_provider=provider)) as client:
        session_id = open_session(client, root)
        coordinator = client.app.state.team_coordinator
        runtime = client.app.state.agent_runtime
        session = client.app.state.session_service.get(session_id)
        events = []

        async def exercise():
            async for event in coordinator.turn(session, "Проверь форму входа", "test"):
                events.append(event)
                if event["type"] == "approval_required":
                    assert event["node_id"] == "aqa"
                    assert not (root / "project" / "test_login.py").exists()
                    runtime.resolve_approval(event["session_id"], event["call"]["id"], approved)

        client.portal.call(exercise)
        chat = client.get(f"/api/sessions/{session_id}/messages").json()
        owner = client.get(f"/api/sessions/{session_id}").json()
        qa_session = client.get(f"/api/sessions/{owner['node_sessions']['qa']}").json()
        visible = {item["id"] for item in client.get("/api/sessions").json()}
        memory_blocked = client.post(f"/api/sessions/{qa_session['id']}/memory/update").status_code
        # Пользователь может написать агенту с холста напрямую: тот отвечает в своей переписке.
        direct = events_of(client.post(f"/api/sessions/{qa_session['id']}/turns/stream", json={"content": "Привет"}))
        qa_chat = client.get(f"/api/sessions/{qa_session['id']}/messages").json()
        chat_after = client.get(f"/api/sessions/{session_id}/messages").json()

    assert events[-1] == {"type": "team_finished", "steps": 5} and not coordinator.is_busy(session_id)
    assert (root / "project" / "test_login.py").exists() is approved
    # В чате сессии: вопрос пользователя, поручение QA, ответ QA от его имени и итог.
    assert [(item["role"], item["sender"]) for item in chat if item["role"] != "tool"] == [
        ("user", None), ("assistant", None), ("assistant", None), ("user", "QA"), ("assistant", None),
    ]
    assert chat[-1]["content"] == "Итог: тесты готовы"
    assert chat[-2]["content"].startswith("[ответ: QA]")
    # Ответ AQA пришёл в QA, а итог QA ушёл агенту сессии, а не обратно в AQA.
    qa_inputs = [messages[-1].content for model, messages, _ in provider.calls if model == "qa-model"]
    assert any(text.startswith("[ответ: AQA]") for text in qa_inputs)
    # У основного агента строки skills нет — ему доступен весь каталог навыков.
    assert provider.tools_of("main-model")[0] == {"read_file", "send_message", "use_skill"}
    assert provider.tools_of("qa-model")[0] == {"read_file", "send_message", "use_skill"}
    # У AQA нет стрелок: писать ему некому, а навыков нет.
    assert provider.tools_of("aqa-model")[0] == {"read_file", "write_file"}
    # Агенты с холста не видят личную память пользователя, агент сессии — видит.
    assert MEMORY_FACT in provider.calls[0][1][0].content
    assert all(MEMORY_FACT not in messages[0].content for model, messages, _ in provider.calls if model != "main-model")
    assert memory_blocked == 422 and direct[-1] == {"type": "team_finished", "steps": 1}
    assert qa_chat[-2]["content"] == "Привет" and qa_chat[-2]["sender"] is None
    assert qa_chat[-1]["content"] == "Привет, я QA" and chat_after == chat
    assert qa_session["hidden"] and qa_session["parent_id"] == session_id and qa_session["node_id"] == "qa"
    assert session_id in visible and qa_session["id"] not in visible


def test_step_limit_and_forbidden_edges_over_http(root):
    provider = TeamProvider({
        "main-model": [send("qa", "Вопрос"), "Спросил", send("aqa", "В обход QA"), send("qa", "Ещё вопрос"), "Ок"],
        "qa-model": [send("main", "Встречный вопрос"), "Спросил"] * 3,
    })
    with TestClient(create_app(settings(root, max_run_steps=3), llm_provider=provider)) as client:
        session_id = open_session(client, root)
        response = client.post(f"/api/sessions/{session_id}/turns/stream", json={"content": "Задача"})
        events = events_of(response)
        owner = client.get(f"/api/sessions/{session_id}").json()
        chat = client.get(f"/api/sessions/{session_id}/messages").json()
        refused = [event for event in events if event["type"] == "tool_result" and event["message"]["is_error"]]
        non_stream = client.post(f"/api/sessions/{session_id}/turns", json={"content": "Без потока"})

    assert response.status_code == 200 and events[-1] == {"type": "team_finished", "steps": 3}
    assert any(event["type"] == "notice" for event in events)
    assert chat[-1]["content"] == "_Команда остановлена: достигнут лимит 3 шагов._"
    # Стрелки main → aqa нет: инструмент отказал, и в AQA ничего не ушло.
    assert "Этому получателю писать нельзя" in refused[0]["message"]["content"]
    assert "aqa" not in owner["node_sessions"]
    assert non_stream.status_code == 400


def test_canvas_agent_keeps_its_session_between_turns(root):
    provider = TeamProvider({
        "main-model": [send("qa", "Первое"), "Ок", "Готово 1", send("qa", "Второе"), "Ок", "Готово 2"],
        "qa-model": ["Ответ 1", "Ответ 2"],
    })
    with TestClient(create_app(settings(root), llm_provider=provider)) as client:
        session_id = open_session(client, root)
        for content in ("Раз", "Два"):
            client.post(f"/api/sessions/{session_id}/turns/stream", json={"content": content})
        owner = client.get(f"/api/sessions/{session_id}").json()
        qa_chat = client.get(f"/api/sessions/{owner['node_sessions']['qa']}/messages").json()

    # QA помнит первое поручение, когда получает второе: это одна и та же его сессия.
    assert [item["content"] for item in qa_chat if item["role"] == "user"] == [
        "[от: Основной агент]\n\nПервое", "[от: Основной агент]\n\nВторое",
    ]


def test_canvas_errors_block_the_team_and_deleting_session_removes_agent_sessions(root):
    provider = TeamProvider({"main-model": [send("qa", "Задача"), "Передал", "Итог"], "qa-model": ["Готово"]})
    with TestClient(create_app(settings(root), llm_provider=provider)) as client:
        session_id = open_session(client, root)
        client.post(f"/api/sessions/{session_id}/turns/stream", json={"content": "Задача"})
        child = client.get(f"/api/sessions/{session_id}").json()["node_sessions"]["qa"]
        broken = client.put(f"/api/sessions/{session_id}/canvas", json={
            **TEAM, "nodes": [*TEAM["nodes"], {"id": "pm", "name": "PM", "agent_id": "ghost"}],
        })
        blocked = events_of(client.post(f"/api/sessions/{session_id}/turns/stream", json={"content": "Ещё"}))
        child_canvas = client.put(f"/api/sessions/{child}/canvas", json=TEAM)
        deleted = client.delete(f"/api/sessions/{session_id}")
        child_after = client.get(f"/api/sessions/{child}")

    assert broken.status_code == 200 and "агент ghost не найден" in broken.json()["errors"][0]
    assert blocked[-1]["type"] == "error" and "На холсте ошибки" in blocked[-1]["message"]
    assert child_canvas.status_code == 409
    assert deleted.status_code == 204 and child_after.status_code == 404


def test_busy_team_session_rejects_second_message_and_cancel_frees_it(root):
    provider = TeamProvider({"main-model": [send("qa", "Задача"), "Передал"], "qa-model": []})
    with TestClient(create_app(settings(root), llm_provider=provider)) as client:
        session_id = open_session(client, root)
        coordinator = client.app.state.team_coordinator
        session = client.app.state.session_service.get(session_id)

        async def exercise():
            events = coordinator.turn(session, "Задача", "test")
            async for event in events:
                if event["type"] == "node_started" and event["node_id"] == "qa":
                    break
            busy = coordinator.is_busy(session_id)
            await events.aclose()
            return busy

        was_busy = client.portal.call(exercise)
        coordinator._busy.add(session_id)
        second = client.post(f"/api/sessions/{session_id}/turns/stream", json={"content": "Ещё"})
        coordinator._busy.discard(session_id)

    assert was_busy and not coordinator.is_busy(session_id)
    assert second.status_code == 409


def test_agent_replies_do_not_count_as_user_messages_for_memory(root):
    with TestClient(create_app(settings(root))) as client:
        session = client.post("/api/sessions", json={"model": "m"}).json()
        conversation = client.app.state.conversation_service
        conversation.add_user_message(session["id"], "Я люблю чай")
        conversation.add_user_message(session["id"], "[ответ: QA]\n\nГотово", sender="QA")
        pending = client.app.state.memory_service.pending_user_messages(session["id"])
        stored = client.get(f"/api/sessions/{session['id']}/messages").json()

    assert pending == 1
    assert stored[1]["sender"] == "QA" and stored[0]["sender"] is None


def test_every_new_session_starts_with_the_main_agent(root):
    provider = TeamProvider({"main-model": [send("qa", "Задача"), "Передал", "Итог"], "qa-model": ["Готово"]})
    with TestClient(create_app(settings(root), llm_provider=provider)) as client:
        first = client.post("/api/sessions", json={"model": "main-model", "workspace": str(root / "project")}).json()
        # Проект запоминает агента, выбранного в его сессии, но новая сессия всё равно начинает с основного.
        client.put(f"/api/sessions/{first['id']}/config", json={"provider": "lm_studio", "model": "main-model", "agent_id": "qa"})
        fresh = client.post("/api/sessions", json={"project_id": first["project_id"]}).json()
        listed = [agent["id"] for agent in client.get("/api/agents").json()]
        # Карточка «вход» без своего имени называется по агенту сессии.
        client.put(f"/api/sessions/{fresh['id']}/canvas", json={**TEAM, "nodes": [{"id": "main", "name": ""}, *TEAM["nodes"][1:]]})
        client.post(f"/api/sessions/{fresh['id']}/turns/stream", json={"content": "Задача"})
        qa_input = next(messages[-1].content for model, messages, _ in provider.calls if model == "qa-model")

    assert fresh["agent_id"] == "default" and listed[0] == "default"
    assert qa_input.startswith("[от: Основной агент]")


def test_agent_conversation_opens_before_any_delegation(root):
    with TestClient(create_app(settings(root))) as client:
        session_id = open_session(client, root)
        first = client.post(f"/api/sessions/{session_id}/canvas/qa/session")
        again = client.post(f"/api/sessions/{session_id}/canvas/qa/session")
        entry = client.post(f"/api/sessions/{session_id}/canvas/main/session")
        missing = client.post(f"/api/sessions/{session_id}/canvas/pm/session")
        nested = client.post(f"/api/sessions/{first.json()['id']}/canvas/qa/session")

    assert first.status_code == 200 and first.json()["parent_id"] == session_id and first.json()["hidden"]
    assert again.json()["id"] == first.json()["id"]
    assert entry.json()["id"] == session_id
    assert missing.status_code == 400 and nested.status_code == 404
