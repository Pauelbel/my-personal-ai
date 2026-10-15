"""Рост возможностей: propose → карточка → утверждение → файлы на диске; архив; делегирование саб-агенту."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session
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
        session = post_session(client, {"model": "test"}).json()
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
        session = post_session(client, {"model": "test"}).json()
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
        session = post_session(client, {"model": "test"}).json()
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


def test_agent_is_revised_in_place_keeping_links_and_tools(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "helper.md").write_text("---\nname: Помощник\ntools: \n---\nПомогаю.\n", encoding="utf-8")
    (agents / "qa.md").write_text(
        "---\nname: Тестировщик\ndescription: По документации\ntools: read_file\nmax_tool_rounds: 7\n"
        "agents: helper\n---\nПиши тест-кейсы по документации.\n", encoding="utf-8",
    )
    provider = ScriptedProvider(
        call("propose", kind="agent", name="qa", title="Тестировщик", description="По документации и задачам",
             reason="Пользователь просит", content="Пиши тест-кейсы по документации и по описанию задачи."),
        "Обновил",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Доработай тестировщика"}))[-1]
        events_of(client.post(url + "/resume", json={"action": "approve"}))
        overview = client.get("/api/capabilities").json()

    pending = card["pending"]
    assert pending["title"].startswith("Изменить") and pending["update"]
    assert pending["proposal"]["previous"] == "Пиши тест-кейсы по документации."
    assert pending["proposal"]["tools"] == ["read_file"]
    text = (agents / "qa.md").read_text(encoding="utf-8")
    assert "по описанию задачи" in text and "agents: helper" in text and "max_tool_rounds: 7" in text
    assert "tools: read_file" in text
    assert sorted(agent["id"] for agent in overview["agents"]) == ["helper", "qa"]


def test_reply_to_proposal_lets_agent_revise_it(tmp_path):
    provider = ScriptedProvider(PROPOSE_TOOL, PROPOSE_TOOL, "Создал")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
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
        session = post_session(client, {"model": "test"}).json()
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
    provider = RoutingProvider(
        call("delegate", agent="qa", task="Проверь вход в систему"), "Поручил тестировщику", "Тестировщик проверил вход: всё работает",
    )
    configured = settings(tmp_path)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "qa.md").write_text(
        "---\nname: Тестировщик\ndescription: Проверяет\ntools: read_file\n---\nТы тестировщик.\n", encoding="utf-8",
    )
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        client.app.state.memory_service.write_document("profile.md", "# Профиль\n\n- Зовут Павел\n")
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Проверь вход"}))
        assert events[-1]["message"]["content"] == "Поручил тестировщику"
        history = []
        for _ in range(100):
            history = client.get(url + "/messages").json()
            if history[-1]["content"].startswith("Тестировщик проверил"):
                break
            time.sleep(0.05)
        tasks = client.get("/api/attention").json()["tasks"]
        listed = [item["id"] for item in client.get("/api/sessions").json()]

    report, reaction = history[-2], history[-1]
    assert report["role"] == "user" and report["sender"] == "Тестировщик"
    assert "Проверил: всё работает" in report["content"]
    # Итог поручения сам запускает ход основного: он докладывает, не дожидаясь сообщения пользователя.
    assert reaction["role"] == "assistant" and reaction["content"] == "Тестировщик проверил вход: всё работает"
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
        session = post_session(client, {"model": "test"}).json()
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


def test_agents_address_each_other_in_their_session_dialogs_without_cycles(tmp_path):
    provider = TeamProvider(
        [call("delegate", agent="qa", task="Проверь корзину и запиши заметку"), "Поручил"],
        тестировщик=[call("ask_agent", agent="notes", question="Запиши: корзина работает"), "Проверил, заметку записали"],
        # Заметковед пробует спросить тестировщика в ответ — это цикл, его не пустят.
        заметковед=[call("ask_agent", agent="qa", question="А что именно проверено?"), "Заметка готова"],
    )
    (tmp_path / "agents").mkdir()
    for agent_id, name, role in (("qa", "Тестировщик", "тестировщик"), ("notes", "Заметковед", "заметковед")):
        (tmp_path / "agents" / f"{agent_id}.md").write_text(
            f"---\nname: {name}\ndescription: Помогает\ntools: read_file\n---\nТы {role}.\n", encoding="utf-8",
        )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Проверь корзину"})
        history = []
        for _ in range(100):
            history = client.get(url + "/messages").json()
            if any(message.get("sender") == "Тестировщик" for message in history):
                break
            time.sleep(0.05)
        team = {node["id"]: node for node in client.get(url + "/team").json()["nodes"]}
        notes_dialog = client.get(f"/api/sessions/{team['notes']['session_id']}/messages").json()
        log = client.get(url + "/team/log").json()
        tasks = {task["agent"]: task for task in client.get("/api/attention").json()["tasks"]}

    # Связей рисовать не нужно: тестировщик обратился к заметковеду сам, ответ вернулся ему результатом инструмента.
    [after_answer] = [messages for messages in provider.seen["Ты тестировщик"] if any(item.role == "tool" for item in messages)]
    assert "Ответ notes:\n\nЗаметка готова" in after_answer[-1].content
    assert "- notes — Заметковед: Помогает" in after_answer[0].content
    assert any(message.get("sender") == "Тестировщик" and "заметку записали" in message["content"] for message in history)
    # Вопрос лёг в разговор заметковеда в этой же сессии — он теперь участник; обратный вопрос отбит как цикл.
    assert team["notes"]["member"] and notes_dialog[0]["sender"] == "Тестировщик"
    assert any(message["role"] == "tool" and "цикл" in message["content"] for message in notes_dialog)
    # Кто кому поручил — для дерева задач и строки активности под кружками.
    assert team["notes"]["from"] == "Тестировщик" and team["qa"]["from"] == "Основной"
    assert tasks["Заметковед"]["from"] == "Тестировщик" and tasks["Тестировщик"]["from"] == "Основной"
    # Лента команды для основного чата: кто кому написал и что ответил.
    assert [(item["from"], item["to"], item["kind"]) for item in log] == [
        ("Тестировщик", "Заметковед", "ask"), ("Заметковед", "Тестировщик", "answer"),
    ]


def test_team_prompt_lists_reachable_agents_and_restriction_narrows_them(tmp_path):
    from local_agent.agent.models import Agent
    from local_agent.agent.registry import AgentRegistry
    from local_agent.agent.team import reachable, team_graph, team_prompt

    def agent(agent_id, links=()):
        return Agent(id=agent_id, name=agent_id.upper(), system_prompt="x", llm_provider="", model="",
                     description=f"про {agent_id}", agents=tuple(links))

    registry = AgentRegistry([agent("qa"), agent("notes"), agent("auto", links=("qa",))])
    assert reachable(registry.get("qa"), registry) == ("notes", "auto")
    assert reachable(registry.get("auto"), registry) == ("qa",)
    prompt = team_prompt(registry.get("qa"), registry)
    assert "- notes — NOTES: про notes" in prompt and "- auto — AUTO" in prompt and "руководитель" in prompt
    graph = team_graph(registry)
    assert {"from": "main", "to": "auto"} in graph["edges"] and {"from": "auto", "to": "notes"} not in graph["edges"]
    assert [node["restricted"] for node in graph["nodes"]] == [False, False, False, True]

def wait_for(check, attempts=100):
    for _ in range(attempts):
        if result := check():
            return result
        time.sleep(0.05)
    return check()


def write_agents(tmp_path, *agents):
    (tmp_path / "agents").mkdir(exist_ok=True)
    for agent_id, name in agents:
        (tmp_path / "agents" / f"{agent_id}.md").write_text(
            f"---\nname: {name}\ndescription: Помогает\ntools: read_file\nagents: {'notes' if agent_id == 'qa' else ''}\n---\nТы тестировщик.\n",
            encoding="utf-8",
        )


def test_agent_keeps_one_dialog_in_session_tasks_queue_clear_and_remove(tmp_path):
    provider = RoutingProvider(
        call("delegate", agent="qa", task="Проверь вход"), call("delegate", agent="qa", task="Проверь корзину"), "Поручил",
    )
    write_agents(tmp_path, ("qa", "Тестировщик"), ("notes", "Заметковед"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Проверь вход и корзину"})
        def reports():
            return [m for m in client.get(url + "/messages").json() if m.get("sender") == "Тестировщик"]

        wait_for(lambda: len(reports()) == 2)
        reported = reports()
        children = [s for s in client.get("/api/sessions?include_hidden=true").json() if s["parent_id"] == session["id"]]
        dialog = client.get(f"/api/sessions/{children[0]['id']}/messages").json()
        team = {node["id"]: node for node in client.get(url + "/team").json()["nodes"]}

        cleared = client.post(f"/api/sessions/{children[0]['id']}/clear")
        after_clear = client.get(f"/api/sessions/{children[0]['id']}/messages").json()
        removed = client.delete(url + "/team/qa")
        left = [s for s in client.get("/api/sessions?include_hidden=true").json() if s["parent_id"] == session["id"]]
        team_after = {node["id"]: node for node in client.get(url + "/team").json()["nodes"]}

    assert len(reported) == 2
    # Оба поручения — в одном разговоре агента, по очереди: второе видит первое.
    [child] = children
    assert [m["content"] for m in dialog if m["role"] == "user"] == ["Проверь вход", "Проверь корзину"]
    assert child["task"]["status"] == "done"
    assert team["qa"]["member"] and not team["notes"]["member"] and team["qa"]["session_id"] == child["id"]
    # «Очистить» начинает разговор заново, агент остаётся в сессии; «убрать» удаляет разговор.
    assert cleared.status_code == 200 and cleared.json()["id"] == child["id"] and after_clear == []
    assert removed.status_code == 204 and left == [] and not team_after["qa"]["member"]


def test_agents_are_listed_edited_and_have_a_personal_chat(tmp_path):
    provider = RoutingProvider()
    write_agents(tmp_path, ("qa", "Тестировщик"), ("notes", "Заметковед"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.memory_service.write_document("profile.md", "# Профиль\n\n- Зовут Павел\n")
        post_session(client, {"model": "test"})
        listed = client.get("/api/agents").json()
        created = client.post("/api/agents", json={
            "id": "analyst", "name": "Аналитик", "description": "Разбирает требования",
            "system_prompt": "Ты аналитик.", "tools": ["read_file"], "skills": [],
        })
        duplicate = client.post("/api/agents", json={"id": "qa", "name": "x", "system_prompt": "x"})
        bad_tool = client.put("/api/agents/qa", json={"name": "Тестировщик", "system_prompt": "x", "tools": ["nope"]})
        edited = client.put("/api/agents/qa", json={
            "name": "Тестировщик", "description": "Пишет кейсы и по задачам", "system_prompt": "Ты тестировщик, пиши кейсы.",
            "tools": ["read_file"], "skills": None,
        })
        stored = (tmp_path / "agents" / "qa.md").read_text(encoding="utf-8")

        chat = client.post("/api/agents/qa/chat", json={"model": "test"}).json()
        again = client.post("/api/agents/qa/chat", json={}).json()
        events = events_of(client.post(f"/api/sessions/{chat['id']}/turns/stream", json={"content": "Кто ты?"}))
        listed_after = {item["id"]: item for item in client.get("/api/agents").json()}
        visible = [item["id"] for item in client.get("/api/sessions").json()]

    assert [item["id"] for item in listed] == ["main", "notes", "qa"] and listed[1]["status"] == "idle"
    assert created.status_code == 201 and created.json()["skills"] == []
    assert duplicate.status_code == 409 and bad_tool.status_code == 400
    # Правка меняет роль, а связь с холста остаётся; без skills агенту по-прежнему доступны все скиллы.
    assert edited.json()["skills"] is None and "skills:" not in stored
    assert "agents: notes" in stored and "пиши кейсы" in stored
    # Личный чат: один на агента, вне проектов, за самим агентом и без личной памяти пользователя.
    assert chat["kind"] == "agent" and chat["project_id"] is None and again["id"] == chat["id"]
    assert events[-1]["message"]["content"] == "Проверил: всё работает"
    sub = provider.requests[-1]
    assert sub.get("sub") and "Зовут Павел" not in sub["messages"][0].content
    assert listed_after["qa"]["chat_id"] == chat["id"] and chat["id"] not in visible


def test_main_agent_has_general_chat_and_user_instructions(tmp_path):
    provider = ScriptedProvider("Привет", "Готово", "Ок")
    write_agents(tmp_path, ("qa", "Тестировщик"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.memory_service.write_document("profile.md", "# Профиль\n\n- Зовут Павел\n")
        saved = client.put("/api/agents/main/instructions", json={"text": "Тест-кейсы всегда поручай тестировщику."})
        too_long = client.put("/api/agents/main/instructions", json={"text": "x" * 5000})
        session = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Привет"})
        general = client.post("/api/agents/main/chat", json={"model": "test"}).json()
        client.post(f"/api/sessions/{general['id']}/turns/stream", json={"content": "Как меня зовут?"})
        [main] = [item for item in client.get("/api/agents").json() if item["id"] == "main"]
        cleared = client.put("/api/agents/main/instructions", json={"text": ""})
        stored = (tmp_path / "main-instructions.md").exists()

    assert saved.json() == {"instructions": "Тест-кейсы всегда поручай тестировщику."} and too_long.status_code == 400
    project_turn, general_turn = provider.requests[0], provider.requests[1]
    # Свои правила стоят после встроенных и действуют и в сессии проекта, и в общем чате.
    assert "Дополнительные правила от пользователя:\nТест-кейсы всегда поручай тестировщику." in project_turn["messages"][0].content
    assert "Тест-кейсы всегда поручай тестировщику." in general_turn["messages"][0].content
    # Общий чат — разговор с пользователем: память на месте, а поручать вне проекта некуда.
    assert "Зовут Павел" in general_turn["messages"][0].content
    tools = {item["function"]["name"] for item in general_turn["tools"]}
    assert "remember" in tools and "delegate" not in tools
    assert general["kind"] == "agent" and general["agent_id"] == "main" and general["project_id"] is None
    assert main["builtin"] and main["chat_id"] == general["id"] and main["name"] == "Основной"
    assert cleared.json() == {"instructions": ""} and not stored


def test_only_a_really_empty_session_is_discarded_and_personal_chats_stay_outside_projects(tmp_path):
    provider = RoutingProvider("Привет")
    write_agents(tmp_path, ("qa", "Тестировщик"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        talked = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{talked['id']}/turns/stream", json={"content": "Привет"})
        with_agent = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{with_agent['id']}/team/qa/chat")
        empty = post_session(client, {"model": "test"}).json()
        personal = client.post("/api/agents/qa/chat", json={"model": "test"}).json()

        # Интерфейс мог ошибиться — сервер всё равно не удалит сессию с историей, с агентом и личный чат.
        refused = [client.post(f"/api/sessions/{item['id']}/discard").status_code for item in (talked, with_agent, personal)]
        discarded = client.post(f"/api/sessions/{empty['id']}/discard").status_code
        left = {item["id"] for item in client.get("/api/sessions?include_hidden=true").json()}

    # Перезапуск: миграция раздаёт проекты сессиям без проекта, но личный чат с агентом живёт вне проектов.
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        reopened = client.get(f"/api/sessions/{personal['id']}").json()

    assert refused == [409, 409, 409] and discarded == 204
    assert {talked["id"], with_agent["id"], personal["id"]} <= left and empty["id"] not in left
    assert reopened["project_id"] is None


def test_trusting_browser_once_skips_its_cards_for_the_rest_of_the_session(tmp_path):
    from tests.test_web_and_mcp import _browser_tool

    provider = TeamProvider(
        [], браузерщик=[call("browser_browser_type", target="e1", text="Pavel"),
                        call("browser_browser_type", target="e2", text="Belov"), "Заполнил"],
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "web.md").write_text(
        "---\nname: Браузерщик\ndescription: Ходит по сайтам\ntools: browser_browser_type\n---\nТы браузерщик.\n", encoding="utf-8",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.tool_registry.register(_browser_tool("browser_type", "ok"))
        session = post_session(client, {"model": "test"}).json()
        dialog = client.post(f"/api/sessions/{session['id']}/team/web/chat").json()
        url = f"/api/sessions/{dialog['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Заполни форму"}))[-1]
        finished = events_of(client.post(url + "/resume", json={"action": "approve", "trust": True}))
        trusted = client.get(url).json()["trusted"]
        other = client.get(f"/api/sessions/{session['id']}").json()["trusted"]

    assert card["type"] == "interrupt" and card["pending"]["trust_group"] == "browser"
    # Первое действие утверждено «для всей сессии» — второе прошло без карточки.
    assert [event["type"] for event in finished if event["type"] in {"interrupt", "done"}] == ["done"]
    assert finished[-1]["message"]["content"] == "Заполнил"
    assert trusted == ["browser"] and other == []


def test_team_reference_shows_everyone_connected_until_restricted(tmp_path):
    (tmp_path / "agents").mkdir()
    for agent_id, name in (("qa", "Тестировщик"), ("notes", "Заметковед"), ("auto", "Автотестер")):
        (tmp_path / "agents" / f"{agent_id}.md").write_text(
            f"---\nname: {name}\ndescription: Помогает\ntools: read_file\n---\nТы {name}.\n", encoding="utf-8",
        )
    with TestClient(create_app(settings(tmp_path), llm_provider=ScriptedProvider())) as client:
        before = client.get("/api/agents/team").json()
        restricted = client.put("/api/agents/auto", json={"name": "Автотестер", "system_prompt": "x", "agents": ["qa"]})
        kept = client.put("/api/agents/auto", json={"name": "Автотестер", "system_prompt": "y"})
        after = client.get("/api/agents/team").json()

    edges = lambda graph: {(edge["from"], edge["to"]) for edge in graph["edges"]}  # noqa: E731
    # По умолчанию все со всеми, основной — ко всем.
    assert {("main", "qa"), ("qa", "auto"), ("auto", "notes"), ("notes", "qa")} <= edges(before)
    # Ограничение: автотестер обращается только к тестировщику; правка без поля agents его не сбрасывает.
    assert restricted.json()["agents"] == ["qa"] and kept.json()["agents"] == ["qa"]
    assert ("auto", "qa") in edges(after) and ("auto", "notes") not in edges(after) and ("qa", "auto") in edges(after)


def test_subagent_asks_user_and_requests_missing_tools(tmp_path):
    provider = TeamProvider(
        [], автотестер=[
            call("request_tools", tools=["write_file"], reason="Нужно записать файл автотеста"),
            call("ask_user", question="Какой браузер брать: Chromium или Firefox?"),
            "Готово: тест для Chromium",
        ],
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "auto.md").write_text(
        "---\nname: Автотестер\ndescription: Пишет автотесты\ntools: read_file\n---\nТы автотестер.\n", encoding="utf-8",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        dialog = client.post(f"/api/sessions/{session['id']}/team/auto/chat").json()
        url = f"/api/sessions/{dialog['id']}"
        tools_card = events_of(client.post(url + "/turns/stream", json={"content": "Напиши автотест"}))[-1]
        question_card = events_of(client.post(url + "/resume", json={"action": "approve"}))[-1]
        finished = events_of(client.post(url + "/resume", json={"action": "reply", "text": "Chromium"}))[-1]
        stored = (tmp_path / "agents" / "auto.md").read_text(encoding="utf-8")
        history = client.get(url + "/messages").json()

    assert tools_card["pending"]["kind"] == "tools_request" and tools_card["pending"]["tools"] == ["write_file"]
    assert question_card["pending"]["kind"] == "question" and "Chromium или Firefox" in question_card["pending"]["question"]
    assert finished["type"] == "done" and finished["message"]["content"] == "Готово: тест для Chromium"
    # Инструмент записан в файл агента и сразу появился у него в этом же ходе; ответ пользователя пришёл агенту.
    assert "tools: read_file, write_file" in stored
    after_grant = provider.seen["Ты автотестер"][1]
    assert any("Подключено: write_file" in item.content for item in after_grant if item.role == "tool")
    assert any(message["role"] == "tool" and message["content"] == "Ответ пользователя: Chromium" for message in history)
    # В промпте агента — как попросить пользователя и инструменты.
    assert "ask_user" in after_grant[0].content and "request_tools" in after_grant[0].content


def test_main_asks_who_does_the_task_and_remembers_the_route(tmp_path):
    provider = ScriptedProvider(
        call("choose_route", kind="Автотесты ", agent="auto", task="Автотест корзины"), "Сделал сам", "Снова сам",
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "auto.md").write_text(
        "---\nname: Автотестер\ndescription: Пишет автотесты\ntools: read_file\n---\nТы автотестер.\n", encoding="utf-8",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Напиши автотест корзины"}))[-1]
        finished = events_of(client.post(url + "/resume", json={"action": "reply", "text": "Сделай сам", "choice": "main"}))[-1]
        project = next(item for item in client.get("/api/projects").json() if item["id"] == session["project_id"])
        events_of(client.post(url + "/turns/stream", json={"content": "Ещё автотест для оплаты"}))
        remembered = provider.system_prompt()
        forgotten = client.delete(f"/api/projects/{project['id']}/routes", params={"kind": "автотесты"}).json()

    pending = card["pending"]
    assert pending["kind"] == "route" and pending["suggested"] == "auto" and pending["task_kind"] == "автотесты"
    assert [agent["id"] for agent in pending["agents"]] == ["auto"]
    assert finished["type"] == "done" and finished["message"]["content"] == "Сделал сам"
    # Выбор пришёл основному агенту результатом инструмента и запомнился в проекте маршрутом.
    assert any(item.role == "tool" and "делаешь сам" in item.content for item in provider.requests[1]["messages"])
    assert project["routes"] == {"автотесты": "main"}
    # В следующем ходе маршрут — в промпте основного: такие задачи он делает без вопроса.
    assert "Маршруты проекта" in remembered and "- автотесты → ты сам" in remembered
    assert forgotten["routes"] == {}


def test_subagent_hands_off_off_profile_task_to_main_agent(tmp_path):
    provider = TeamProvider(
        ["Поручу автотестеру"],
        тестировщик=[call("handoff", task="Напиши автотест корзины", reason="я пишу тест-кейсы, а не автотесты"), "Передал основному агенту"],
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "qa.md").write_text(
        "---\nname: Тестировщик\ndescription: Пишет тест-кейсы\ntools: read_file\n---\nТы тестировщик.\n", encoding="utf-8",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        dialog = client.post(f"/api/sessions/{session['id']}/team/qa/chat").json()
        answer = events_of(client.post(f"/api/sessions/{dialog['id']}/turns/stream", json={"content": "Напиши автотест корзины"}))[-1]
        history = []
        for _ in range(100):
            history = client.get(f"/api/sessions/{session['id']}/messages").json()
            if any(message["role"] == "assistant" for message in history):
                break
            time.sleep(0.05)

    assert answer["type"] == "done" and answer["message"]["content"] == "Передал основному агенту"
    # Задача ушла основному агенту от имени тестировщика, и он сразу взялся её распределять.
    handed = history[0]
    assert handed["sender"] == "Тестировщик" and "Напиши автотест корзины" in handed["content"]
    assert "не по его профилю" in handed["content"] and history[-1]["content"] == "Поручу автотестеру"
    # Правило «чужое — основному» есть в промпте саб-агента.
    assert "handoff" in provider.seen["Ты тестировщик"][0][0].content
