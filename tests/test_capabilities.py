"""Рост возможностей: propose → карточка → утверждение → файлы на диске; архив; делегирование саб-агенту."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import project_session, settings

from local_agent.api.app import create_app
from local_agent.capabilities.sandbox import bwrap_path
from local_agent.llm.models import ChatResult, ToolCall

GREET = 'import json, sys\nargs = json.load(sys.stdin)\nprint(f"Привет, {args[\'name\']}")\n'
PROPOSE_TOOL = call(
    "propose", kind="tool", name="greet", title="Приветствие", description="Здоровается по имени",
    reason="Пользователь хочет приветствия", content=GREET,
    parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    requires_approval=False,
)


@pytest.mark.skipif(bwrap_path() is None, reason="нужен bubblewrap")
def test_proposed_tool_is_shown_whole_created_on_approval_and_runs_in_sandbox(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = ScriptedProvider(PROPOSE_TOOL, "Создал", call("greet", name="Павел"), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Сделай приветствие"}))[-1]
        assert card["type"] == "interrupt" and card["pending"]["kind"] == "proposal"
        assert card["pending"]["proposal"]["content"] == GREET and not card["pending"]["update"]
        assert not (tmp_path / "tools" / "greet").exists()
        client.post(url + "/resume", json={"action": "approve"})
        events = events_of(client.post(url + "/turns/stream", json={"content": "Поздоровайся с Павлом"}))
        overview = client.get("/api/capabilities").json()

    folder = tmp_path / "tools" / "greet"
    assert {path.name for path in folder.iterdir()} == {"tool.md", "parameters.json", "script.py"}
    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert result["content"] == "Привет, Павел" and not result["is_error"]
    assert any(tool["id"] == "greet" and tool["origin"] == "agent" for tool in overview["tools"])


def test_invalid_proposal_goes_back_to_the_model_without_a_card(tmp_path):
    broken = call("propose", kind="tool", name="Bad Name", title="x", description="d", reason="r", content="def (")
    provider = ScriptedProvider(broken, "Исправлю")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Сделай"}))

    assert events[-1]["type"] == "done"
    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert result["is_error"] and "не показан пользователю" in result["content"]


def test_small_model_omissions_in_proposal_are_repaired(tmp_path):
    sloppy = call(
        "propose", kind="tool", name="count_lines", title="Подсчёт строк", reason="Нужно часто", content="print(1)",
        parameters={"properties": {"mask": {"type": "string"}}, "required": [{"in": "mask"}]},
    )
    provider = ScriptedProvider(sloppy)
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        card = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Сделай"}))[-1]

    proposal = card["pending"]["proposal"]
    assert card["type"] == "interrupt" and proposal["description"] == "Подсчёт строк"
    assert proposal["parameters"] == {"type": "object", "properties": {"mask": {"type": "string"}}, "required": ["mask"]}


def test_skill_and_agent_are_created_archived_and_restored(tmp_path):
    provider = ScriptedProvider(
        call("propose", kind="skill", name="test-cases", title="Тест-кейсы", description="Как писать тест-кейсы",
             reason="Задача повторяется", content="1. Прочитай требования.\n2. Выпиши проверки."),
        call("propose", kind="agent", name="qa", title="Тестировщик", description="Пишет тест-кейсы",
             reason="Направление", content="Ты тестировщик.", tools=["read_file"], skills=["test-cases"]),
        call("archive", action="archive", kind="agent", name="qa", reason="Больше не нужен"),
        call("archive", action="restore", kind="agent", name="qa", reason="Снова нужен"),
        "Готово",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        cards = [events_of(client.post(url + "/turns/stream", json={"content": "Оформи"}))[-1]]
        for _ in range(3):
            cards.append(events_of(client.post(url + "/resume", json={"action": "approve"}))[-1])
        last = events_of(client.post(url + "/resume", json={"action": "approve"}))[-1]
        overview = client.get("/api/capabilities").json()

    assert [card["pending"]["title"].split(" ")[0] for card in cards] == ["Создать", "Создать", "Убрать", "Вернуть"]
    assert last["type"] == "done"
    assert (tmp_path / "skills" / "test-cases.md").exists() and (tmp_path / "agents" / "qa.md").exists()
    assert [agent["id"] for agent in overview["agents"]] == ["qa"]
    assert len(list((tmp_path / "agents" / ".deleted").iterdir())) == 0
    # Новый скилл и саб-агент сразу видны в реестре следующего запроса.
    prompt = provider.system_prompt()
    assert "- test-cases — Как писать тест-кейсы" in prompt and "- qa — Пишет тест-кейсы" in prompt


def test_reply_to_proposal_lets_agent_revise_it(tmp_path):
    provider = ScriptedProvider(PROPOSE_TOOL, PROPOSE_TOOL, "Создал")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Сделай"})
        revised = events_of(client.post(url + "/resume", json={"action": "reply", "text": "Добавь фамилию"}))[-1]
        assert revised["type"] == "interrupt"
        assert "Добавь фамилию" in provider.requests[-1]["messages"][-1].content
        assert events_of(client.post(url + "/resume", json={"action": "reject"}))[-1]["type"] == "done"

    assert not (tmp_path / "tools" / "greet").exists()


def test_extra_tool_is_offered_from_archive(tmp_path):
    provider = ScriptedProvider(call("archive", action="restore", kind="tool", name="generate_test_cases", reason="Нужны тест-кейсы"), "Подключил")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Подключи генерацию"})
        assert "- инструмент generate_test_cases" in provider.system_prompt(0)
        client.post(url + "/resume", json={"action": "approve"})
        tools = [tool["id"] for tool in client.get("/api/capabilities").json()["tools"]]

    assert "generate_test_cases" in tools


class RoutingProvider(ScriptedProvider):
    """Саб-агент отвечает своим текстом: ходы основного агента и саб-агента идут параллельно."""

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        if messages[0].content.startswith("Ты тестировщик"):
            self.requests.append({"model": model, "messages": list(messages), "tools": tools, "sub": True})
            return ChatResult(content="Проверил: всё работает")
        return await super().chat(model, messages, tools, response_format, options)


def test_delegated_task_runs_in_background_and_reports_back(tmp_path):
    provider = RoutingProvider(call("delegate", agent="qa", task="Проверь вход в систему"), "Поручил тестировщику")
    configured = settings(tmp_path)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "qa.md").write_text(
        "---\nname: Тестировщик\ndescription: Проверяет\ntools: read_file\n---\nТы тестировщик.\n", encoding="utf-8",
    )
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        client.app.state.memory_service.write_document("profile.md", "# Профиль\n\n- Зовут Павел\n")
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Проверь вход"}))
        assert events[-1]["message"]["content"] == "Поручил тестировщику"
        history = []
        for _ in range(100):
            history = client.get(url + "/messages").json()
            if len(history) > 4:
                break
            time.sleep(0.05)
        tasks = client.get("/api/attention").json()["tasks"]
        listed = [item["id"] for item in client.get("/api/sessions").json()]

    report = history[-1]
    assert report["role"] == "user" and report["sender"] == "Тестировщик"
    assert "Проверил: всё работает" in report["content"]
    [task] = tasks
    assert task["status"] == "done" and task["owner_id"] == session["id"]
    assert task["session_id"] not in listed
    sub = next(request for request in provider.requests if request.get("sub"))
    # Саб-агенту — его инструменты и вики, но не личная память и не создание возможностей.
    assert "Зовут Павел" not in sub["messages"][0].content
    names = {item["function"]["name"] for item in sub["tools"]}
    assert {"read_file", "wiki_search"} <= names and not {"remember", "propose", "delegate"} & names


def test_api_archive_and_restore_skill(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "notes.md").write_text("---\nname: Заметки\ndescription: d\n---\nШаг\n", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path), llm_provider=ScriptedProvider())) as client:
        assert client.post("/api/capabilities/skill/notes/archive").status_code == 204
        [entry] = [item for item in client.get("/api/capabilities").json()["archive"] if item["kind"] == "skill"]
        restored = client.post(f"/api/capabilities/skill/restore/{entry['archive_id']}")
        source = client.get("/api/capabilities/skill/notes").json()

    assert restored.json() == {"id": "notes"}
    assert "Шаг" in source["files"]["notes.md"]


def test_team_shows_links_and_direct_chat_is_not_reported_to_main(tmp_path):
    provider = RoutingProvider(call("delegate", agent="qa", task="Проверь корзину"), "Поручил")
    (tmp_path / "agents").mkdir()
    for agent_id, name in (("qa", "Тестировщик"), ("notes", "Заметковед")):
        (tmp_path / "agents" / f"{agent_id}.md").write_text(
            f"---\nname: {name}\ndescription: Помогает\ntools: read_file\n---\nТы тестировщик.\n", encoding="utf-8",
        )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Проверь корзину"})
        for _ in range(100):
            if len(client.get(url + "/messages").json()) > 4:
                break
            time.sleep(0.05)
        team = client.get(url + "/team").json()
        chat = client.post(url + "/team/qa/chat").json()
        client.post(f"/api/sessions/{chat['id']}/turns/stream", json={"content": "Что ты сделал?"})
        time.sleep(0.3)
        main_history = client.get(url + "/messages").json()
        fresh = client.post(url + "/team/notes/chat").json()
        sub_team = client.get(f"/api/sessions/{chat['id']}/team").json()

    nodes = {node["id"]: node for node in team["nodes"]}
    assert set(nodes) == {"main", "qa", "notes"}
    assert team["edges"] == [{"from": "main", "to": "qa", "tasks": 1, "link": False}]
    assert nodes["qa"]["task"] == "Проверь корзину" and nodes["notes"]["session_id"] is None
    # Разговор с саб-агентом — его последняя сессия; ответ на реплику пользователя не уходит основному.
    assert chat["id"] == nodes["qa"]["session_id"]
    assert sum(1 for message in main_history if message.get("sender") == "Тестировщик") == 1
    assert fresh["parent_id"] == session["id"] and fresh["agent_id"] == "notes"
    assert sub_team["owner_id"] == session["id"]


class TeamProvider(ScriptedProvider):
    """Свой сценарий у каждого агента: агента узнаём по началу системного промпта."""

    def __init__(self, main, **agents) -> None:
        super().__init__(*main)
        self.scripts = {f"Ты {name}": list(steps) for name, steps in agents.items()}
        self.seen: dict[str, list] = {prefix: [] for prefix in self.scripts}

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        for prefix, steps in self.scripts.items():
            if messages[0].content.startswith(prefix):
                self.seen[prefix].append(list(messages))
                step = steps.pop(0) if steps else "Готово"
                if isinstance(step, str):
                    return ChatResult(content=step)
                name, arguments = step
                return ChatResult(content=None, tool_calls=(ToolCall(f"{name}-{len(self.seen[prefix])}", name, json.dumps(arguments, ensure_ascii=False)),))
        return await super().chat(model, messages, tools, response_format, options)


def test_linked_subagent_asks_another_and_gets_the_answer(tmp_path):
    provider = TeamProvider(
        [call("delegate", agent="qa", task="Проверь корзину и запиши заметку"), "Поручил"],
        тестировщик=[call("ask_agent", agent="notes", question="Запиши: корзина работает"), "Проверил, заметку записали"],
        заметковед=["Заметка готова"],
    )
    (tmp_path / "agents").mkdir()
    for agent_id, name, role in (("qa", "Тестировщик", "тестировщик"), ("notes", "Заметковед", "заметковед")):
        (tmp_path / "agents" / f"{agent_id}.md").write_text(
            f"---\nname: {name}\ndescription: Помогает\ntools: read_file\n---\nТы {role}.\n", encoding="utf-8",
        )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        linked = client.put("/api/agents/qa/links/notes")
        self_link = client.put("/api/agents/qa/links/qa")
        stored = (tmp_path / "agents" / "qa.md").read_text(encoding="utf-8")
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Проверь корзину"})
        history = []
        for _ in range(100):
            history = client.get(url + "/messages").json()
            if any(message.get("sender") == "Тестировщик" for message in history):
                break
            time.sleep(0.05)
        team = client.get(url + "/team").json()
        tasks = client.get("/api/attention", params={"session_id": session["id"]}).json()["tasks"]
        client.delete("/api/agents/qa/links/notes")
        unlinked = (tmp_path / "agents" / "qa.md").read_text(encoding="utf-8")

    assert linked.status_code == 204 and self_link.status_code == 400
    assert "agents: notes" in stored and "agents:" not in unlinked
    # Ответ заметковеда пришёл тестировщику результатом инструмента, а основной чат получил итог тестировщика.
    [after_answer] = [messages for messages in provider.seen["Ты тестировщик"] if any(item.role == "tool" for item in messages)]
    assert "Ответ notes:\n\nЗаметка готова" in after_answer[-1].content
    assert any(message.get("sender") == "Тестировщик" and "заметку записали" in message["content"] for message in history)
    edges = {(edge["from"], edge["to"]): edge for edge in team["edges"]}
    assert edges[("main", "qa")]["tasks"] == 1 and edges[("qa", "notes")] == {"from": "qa", "to": "notes", "tasks": 1, "link": True}
    # Вопрос саб-агента другому виден в доке основной сессии.
    assert {(task["agent"], task["status"]) for task in tasks} == {("Тестировщик", "done"), ("Заметковед", "done")}
