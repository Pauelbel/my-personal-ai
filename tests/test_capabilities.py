"""Рост возможностей: propose → карточка → утверждение → файлы на диске; архив; правила основного агента."""


import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session
from tests.test_graph_flow import project_session, settings

from local_agent.api.app import create_app
from local_agent.connectors.sandbox import bwrap_path

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
    # Скрипт из data/tools — коннектор, даже без доступов: «настроить» у него нет, полей формы тоже.
    assert any(tool["id"] == "greet" and tool["origin"] == "connector" and tool["fields"] == [] for tool in overview["tools"])


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


def test_skill_is_created_archived_and_restored(tmp_path):
    provider = ScriptedProvider(
        call("propose", kind="skill", name="test-cases", title="Тест-кейсы", description="Как писать тест-кейсы",
             reason="Задача повторяется", content="1. Прочитай требования.\n2. Выпиши проверки."),
        call("archive", action="archive", kind="skill", name="test-cases", reason="Больше не нужен"),
        call("archive", action="restore", kind="skill", name="test-cases", reason="Снова нужен"),
        "Готово",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        cards = [events_of(client.post(url + "/turns/stream", json={"content": "Оформи"}))[-1]]
        for _ in range(2):
            cards.append(events_of(client.post(url + "/resume", json={"action": "approve"}))[-1])
        last = events_of(client.post(url + "/resume", json={"action": "approve"}))[-1]
        overview = client.get("/api/capabilities").json()

    assert [card["pending"]["title"].split(" ")[0] for card in cards] == ["Создать", "Убрать", "Вернуть"]
    assert last["type"] == "done"
    assert (tmp_path / "skills" / "test-cases" / "SKILL.md").exists()
    assert [skill["id"] for skill in overview["skills"]] == ["test-cases"] and "agents" not in overview
    assert not list((tmp_path / "skills" / ".deleted").iterdir())
    # Новый скилл сразу виден в реестре следующего запроса; агентов, кроме основного, нет.
    prompt = provider.system_prompt()
    assert "- test-cases — Как писать тест-кейсы" in prompt and "Агенты" not in prompt
    [propose] = [item for item in provider.requests[0]["tools"] if item["function"]["name"] == "propose"]
    assert propose["function"]["parameters"]["properties"]["kind"]["enum"] == ["tool", "skill"]
    assert "learn" not in provider.tool_names(0)


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


def test_library_skill_is_offered_and_enabled_through_archive(tmp_path):
    provider = ScriptedProvider(call("archive", action="restore", kind="skill", name="test-cases", reason="Нужны тест-кейсы"), "Включил")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Подключи тест-кейсы"}))[-1]
        assert "- скилл test-cases — " in provider.system_prompt(0) and "(готовый)" in provider.system_prompt(0)
        client.post(url + "/resume", json={"action": "approve"})
        skills = {skill["id"]: skill for skill in client.get("/api/capabilities").json()["skills"]}

    assert card["pending"]["title"] == "Включить готовый скилл test-cases"
    assert skills["test-cases"]["files"] == ["SKILL.md", "steps.md", "scripts/testcases.py"]
    assert (tmp_path / "skills" / "test-cases" / "scripts" / "testcases.py").is_file()


def test_api_archive_and_restore_skill(tmp_path):
    (tmp_path / "skills" / "notes").mkdir(parents=True)
    (tmp_path / "skills" / "notes" / "SKILL.md").write_text("---\nname: Заметки\ndescription: d\n---\nШаг\n", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path), llm_provider=ScriptedProvider())) as client:
        assert client.post("/api/capabilities/skill/notes/archive").status_code == 204
        [entry] = [item for item in client.get("/api/capabilities").json()["archive"] if item["kind"] == "skill"]
        restored = client.post(f"/api/capabilities/skill/restore/{entry['archive_id']}")
        source = client.get("/api/capabilities/skill/notes").json()

    assert restored.json() == {"id": "notes"}
    assert "Шаг" in source["files"]["SKILL.md"] and source["path"].endswith("skills/notes")


def test_user_rules_are_set_in_settings_and_reach_the_prompt(tmp_path):
    provider = ScriptedProvider("Привет")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        saved = client.put("/api/instructions", json={"text": "Тест-кейсы оформляй таблицей."})
        too_long = client.put("/api/instructions", json={"text": "x" * 5000})
        shown = client.get("/api/instructions").json()
        session = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Привет"})
        cleared = client.put("/api/instructions", json={"text": ""})
        stored = (tmp_path / "main-instructions.md").exists()

    assert saved.json() == {"instructions": "Тест-кейсы оформляй таблицей."} and too_long.status_code == 400
    # Встроенный промпт в настройках только показывается; свои правила стоят после встроенных.
    assert shown["instructions"] == "Тест-кейсы оформляй таблицей." and shown["builtin"].startswith("Ты Meepo")
    assert "Дополнительные правила от пользователя:\nТест-кейсы оформляй таблицей." in provider.system_prompt(0)
    assert cleared.json() == {"instructions": ""} and not stored


def test_only_a_really_empty_session_is_discarded(tmp_path):
    provider = ScriptedProvider("Привет")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        talked = post_session(client, {"model": "test"}).json()
        client.post(f"/api/sessions/{talked['id']}/turns/stream", json={"content": "Привет"})
        empty = post_session(client, {"model": "test"}).json()
        # Интерфейс мог ошибиться — сервер всё равно не удалит сессию с историей.
        refused = client.post(f"/api/sessions/{talked['id']}/discard").status_code
        discarded = client.post(f"/api/sessions/{empty['id']}/discard").status_code
        left = {item["id"] for item in client.get("/api/sessions").json()}

    assert refused == 409 and discarded == 204
    assert talked["id"] in left and empty["id"] not in left


def test_trusting_browser_once_skips_its_cards_for_the_rest_of_the_session(tmp_path):
    from tests.test_web_and_mcp import _browser_tool

    provider = ScriptedProvider(
        call("browser_browser_type", target="e1", text="Pavel"), call("browser_browser_type", target="e2", text="Belov"),
        "Заполнил",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.tool_registry.register(_browser_tool("browser_type", "ok"))
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Заполни форму"}))[-1]
        finished = events_of(client.post(url + "/resume", json={"action": "approve", "trust": True}))
        trusted = client.get(url).json()["trusted"]
        neighbour = post_session(client, {"model": "test", "project_id": session["project_id"]}).json()
        other = client.get(f"/api/sessions/{neighbour['id']}").json()["trusted"]

    assert card["type"] == "interrupt" and card["pending"]["trust_group"] == "browser"
    # Первое действие утверждено «для всей сессии» — второе прошло без карточки.
    assert [event["type"] for event in finished if event["type"] in {"interrupt", "done"}] == ["done"]
    assert finished[-1]["message"]["content"] == "Заполнил"
    assert trusted == ["browser"] and other == []
