"""Правка возможности из окна просмотра и окончательное удаление из архива."""

from fastapi.testclient import TestClient
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.capabilities.scripts import write_script_tool

SKILL = "---\nname: Тест-кейсы\ndescription: составить тест-кейсы\n---\nПиши кейсы.\n"


def test_skill_is_edited_only_when_valid(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "test-cases.md").write_text(SKILL, encoding="utf-8")
    with TestClient(create_app(settings(tmp_path))) as client:
        edited = SKILL.replace("Пиши кейсы.", "Пиши кейсы таблицей.")
        assert client.put("/api/capabilities/skill/test-cases", json={"files": {"test-cases.md": edited}}).status_code == 204
        assert client.app.state.skill_registry.get("test-cases").instructions == "Пиши кейсы таблицей."

        broken = client.put("/api/capabilities/skill/test-cases", json={"files": {"test-cases.md": "---\nname: x\n---\nтекст\n"}})
        assert broken.status_code == 400 and "description" in broken.json()["detail"]
        # Ошибка называет настоящий файл, а не черновик.
        assert str(tmp_path / "skills" / "test-cases.md") in broken.json()["detail"] and ".edit-" not in broken.json()["detail"]
        wrong = client.put("/api/capabilities/skill/test-cases", json={"files": {"other.md": edited}})
        assert wrong.status_code == 400
        assert client.put("/api/capabilities/skill/missing", json={"files": {"missing.md": edited}}).status_code == 404

    assert (tmp_path / "skills" / "test-cases.md").read_text(encoding="utf-8") == edited
    assert [path.name for path in (tmp_path / "skills").iterdir()] == ["test-cases.md"]


def test_tool_files_are_checked_before_saving(tmp_path):
    write_script_tool(
        tmp_path / "tools", "hello", name="x", description="Здоровается", reason="r", parameters={"type": "object"},
        code="print('hi')", requires_approval=False, network=False,
    )
    with TestClient(create_app(settings(tmp_path))) as client:
        url = "/api/capabilities/tool/hello"
        syntax = client.put(url, json={"files": {"script.py": "def ("}})
        assert syntax.status_code == 400 and "script.py" in syntax.json()["detail"]
        schema = client.put(url, json={"files": {"parameters.json": "[]"}})
        assert schema.status_code == 400
        assert client.put(url, json={"files": {"evil.sh": "rm -rf /"}}).status_code == 400
        assert client.put(url, json={"files": {"script.py": "print('привет')\n"}}).status_code == 204
        assert client.get(url).json()["files"]["script.py"] == "print('привет')\n"
        assert client.put("/api/capabilities/tool/read_file", json={"files": {"script.py": "x"}}).status_code == 404

    assert (tmp_path / "tools" / "hello" / "parameters.json").read_text(encoding="utf-8").strip().startswith("{")


def test_archived_skill_can_be_deleted_for_good(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "test-cases.md").write_text(SKILL, encoding="utf-8")
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.post("/api/capabilities/skill/test-cases/archive").status_code == 204
        entry = next(item for item in client.get("/api/capabilities").json()["archive"] if item["kind"] == "skill")
        assert client.delete(f"/api/capabilities/skill/archive/{entry['archive_id']}").status_code == 204
        assert not [item for item in client.get("/api/capabilities").json()["archive"] if item["kind"] == "skill"]
        assert client.delete(f"/api/capabilities/skill/archive/{entry['archive_id']}").status_code == 404
        # Путь за пределы архива не принимается: такой записи в архиве нет.
        assert client.delete("/api/capabilities/skill/archive/..%2F..%2Fsecrets.json").status_code in (404, 405)
        extra = client.delete("/api/capabilities/tool/archive/extra:generate_test_cases")
        assert extra.status_code == 400

    assert not list((tmp_path / "skills" / ".deleted").iterdir())


def test_main_agent_prompt_is_shown_read_only(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        client.put("/api/agents/main/instructions", json={"text": "Отвечай кратко"})
        source = client.get("/api/capabilities/agent/main").json()
        assert source["readonly"] is True
        prompt, rules = source["files"].values()
        assert prompt.startswith("Ты Meepo") and rules == "Отвечай кратко"
        # Встроенный промпт не правится через окно просмотра.
        assert client.put("/api/capabilities/agent/main", json={"files": {"main.md": "x"}}).status_code == 404
