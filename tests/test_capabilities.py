"""Рост возможностей: propose → карточка → утверждение → файлы на диске; архив; правила основного агента."""


import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session
from tests.test_graph_flow import project_session, settings

from api.app import create_app
from connectors.sandbox import bwrap_path

GREET = 'import json, sys\nargs = json.load(sys.stdin)\nprint(f"Привет, {args[\'name\']}")\n'
PROPOSE_TOOL = call(
    "propose", kind="tool", name="greet", title="Приветствие", description="Здоровается по имени",
    reason="Пользователь хочет приветствия", content=GREET,
    parameters="- name — имя", hint="Зови, когда просят поздороваться", requires_approval=False,
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
        assert card["pending"]["proposal"]["parameters"] == "- name — имя"
        assert not (tmp_path / "tools" / "greet").exists()
        client.post(url + "/resume", json={"action": "approve"})
        events = events_of(client.post(url + "/turns/stream", json={"content": "Поздоровайся с Павлом"}))
        overview = client.get("/api/capabilities").json()

    folder = tmp_path / "tools" / "greet"
    assert {path.name for path in folder.iterdir()} == {"tool.md", "script.py"}
    assert "## Параметры\n- name — имя\n\n## Подсказка модели\nЗови, когда просят поздороваться" in (folder / "tool.md").read_text(encoding="utf-8")
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
    # Схему объектом, по привычке, переводим в список; кривой required — значит, обязательных нет.
    assert proposal["parameters"] == "- mask (необязательно)"


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
