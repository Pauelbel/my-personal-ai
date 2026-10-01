"""Агенты из интерфейса: создание, правка, удаление в архив и защита файла агента от лишних прав."""


import pytest
from fastapi.testclient import TestClient

from local_agent.agent.loader import load_agents
from local_agent.api.app import create_app
from local_agent.config.settings import Settings

AGENT = {
    "name": "Ревьюер", "description": "Смотрит изменения", "model": "test-model",
    "tools": ["read_file", "git"], "skills": [], "system_prompt": "Ты ревьюер кода.",
}


def settings(tmp_path):
    return Settings(
        agents_path=tmp_path / "agents", skills_path=tmp_path / "skills", sessions_path=tmp_path / "sessions",
        projects_path=tmp_path / "projects", conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )


def test_agent_is_created_edited_and_survives_restart(tmp_path):
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        created = client.post("/api/agents", json={**AGENT, "id": "reviewer"})
        generated = client.post("/api/agents", json={**AGENT, "name": "Без id"})
        duplicate = client.post("/api/agents", json={**AGENT, "id": "reviewer"})
        updated = client.put("/api/agents/reviewer", json={
            **AGENT, "tools": ["read_file"], "skills": None, "max_tool_rounds": 3, "description": "Строгий",
        })
        options = client.get("/api/agents/options").json()
        listed = {agent["id"]: agent for agent in client.get("/api/agents").json()}
    with TestClient(create_app(configured)) as client:
        reloaded = {agent["id"]: agent for agent in client.get("/api/agents").json()}

    assert created.status_code == 201 and created.json()["tools"] == ["read_file", "git"]
    assert generated.status_code == 201 and generated.json()["id"].startswith("agent-")
    assert duplicate.status_code == 409
    assert updated.status_code == 200
    # Правка подхватывается сразу, без перезапуска, и сохраняется в файле.
    for agents in (listed, reloaded):
        assert agents["reviewer"]["tools"] == ["read_file"]
        assert agents["reviewer"]["skills"] is None and agents["reviewer"]["max_tool_rounds"] == 3
        assert agents["reviewer"]["description"] == "Строгий"
    assert "write_file" in {tool["id"] for tool in options["tools"]}
    assert next(tool for tool in options["tools"] if tool["id"] == "write_file")["requires_approval"]


@pytest.mark.parametrize(("fields", "message"), [
    ({"tools": ["read_file", "rm_rf"]}, "Неизвестные инструменты: rm_rf"),
    ({"skills": ["ghost"]}, "Неизвестные навыки: ghost"),
    ({"name": "Имя\ntools: write_file"}, "в одну строку"),
    ({"description": "Описание\rtools: write_file, git"}, "в одну строку"),
    ({"id": "../default"}, "ID агента"),
    ({"id": "Reviewer"}, "ID агента"),
    ({"max_tool_rounds": 0}, "Раундов инструментов"),
])
def test_agent_file_cannot_gain_unexpected_rights(tmp_path, fields, message):
    """Инструменты — права агента: в файл попадают только известные, а перевод строки не дописывает заголовок."""
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/api/agents", json={**AGENT, **fields})
        # Правка существующего агента проходит те же проверки; id при правке не меняется.
        edit = None if "id" in fields else client.put("/api/agents/default", json={**AGENT, **fields})

    assert response.status_code == 400 and message in response.json()["detail"]
    assert edit is None or edit.status_code == 400
    agents = {agent.id: agent for agent in load_agents(tmp_path / "agents")}
    assert set(agents) == {"default"}
    assert "rm_rf" not in agents["default"].tools and agents["default"].name == "Основной агент"


def test_agent_form_does_not_set_model(tmp_path):
    """Модель выбирают в чате: API агентов её не принимает, а закреплённую в файле вручную не трогает."""
    configured = settings(tmp_path)
    injected = {**AGENT, "provider": "nope", "model": "m\ntools: write_file"}
    with TestClient(create_app(configured)) as client:
        created = client.post("/api/agents", json={**injected, "id": "reviewer"})
    path = tmp_path / "agents" / "reviewer.md"
    path.write_text(path.read_text(encoding="utf-8").replace("name:", "provider: ollama\nmodel: pinned\nname:", 1), encoding="utf-8")
    with TestClient(create_app(configured)) as client:
        updated = client.put("/api/agents/reviewer", json={**injected, "description": "Строгий"})

    assert created.status_code == 201 and updated.status_code == 200
    assert "model" not in created.json() and "provider" not in updated.json()
    agent = next(agent for agent in load_agents(tmp_path / "agents") if agent.id == "reviewer")
    assert agent.tools == ("read_file", "git") and agent.description == "Строгий"
    assert (agent.llm_provider, agent.model) == ("ollama", "pinned")


def test_agent_deletion_is_guarded_and_moves_sessions_to_default(tmp_path):
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        client.post("/api/agents", json={**AGENT, "id": "reviewer"})
        client.post("/api/agents", json={**AGENT, "id": "helper"})
        owner = client.post("/api/sessions", json={"model": "m"}).json()
        client.put(f"/api/sessions/{owner['id']}/title", json={"title": "Ревью кода"})
        client.put(f"/api/sessions/{owner['id']}/canvas", json={
            "nodes": [{"id": "main", "name": "Вход"}, {"id": "reviewer", "name": "Ревьюер", "agent_id": "reviewer"}],
        })
        session = client.post("/api/sessions", json={"model": "m", "agent_id": "helper"}).json()

        default = client.delete("/api/agents/default")
        in_team = client.delete("/api/agents/reviewer")
        deleted = client.delete("/api/agents/helper")
        again = client.delete("/api/agents/helper")
        moved = client.get(f"/api/sessions/{session['id']}").json()
        listed = {agent["id"] for agent in client.get("/api/agents").json()}

    assert default.status_code == 400
    assert in_team.status_code == 409 and "Ревью кода" in in_team.json()["detail"]
    assert deleted.status_code == 204 and again.status_code == 404
    assert moved["agent_id"] == "default"
    assert listed == {"default", "reviewer"}
    # Удалённый агент лежит в архиве и не загружается как активный.
    assert len(list((tmp_path / "agents" / ".deleted").glob("helper.*.md"))) == 1
    assert {agent.id for agent in load_agents(tmp_path / "agents")} == {"default", "reviewer"}
