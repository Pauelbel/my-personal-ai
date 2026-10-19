"""Правка возможности из окна просмотра и окончательное удаление из архива."""

from fastapi.testclient import TestClient
from tests.test_graph_flow import settings

from api.app import create_app
from connectors.scripts import write_script_tool

SKILL = "---\nname: Тест-кейсы\ndescription: составить тест-кейсы\n---\nПиши кейсы.\n"


def test_tool_files_are_checked_before_saving(tmp_path):
    write_script_tool(
        tmp_path / "tools", "hello", name="x", description="Здоровается", reason="r",
        code="print('hi')", requires_approval=False, network=False,
    )
    with TestClient(create_app(settings(tmp_path))) as client:
        url = "/api/capabilities/tool/hello"
        syntax = client.put(url, json={"files": {"script.py": "def ("}})
        assert syntax.status_code == 400 and "script.py" in syntax.json()["detail"]
        tool_md = client.get(url).json()["files"]["tool.md"]
        wrong = client.put(url, json={"files": {"tool.md": tool_md + "\n## Параметры\nпросто текст\n"}})
        assert wrong.status_code == 400 and "- имя — что это" in wrong.json()["detail"]
        assert client.put(url, json={"files": {"evil.sh": "rm -rf /"}}).status_code == 400
        assert client.put(url, json={"files": {"script.py": "print('привет')\n"}}).status_code == 204
        assert client.get(url).json()["files"]["script.py"] == "print('привет')\n"
        assert client.put("/api/capabilities/tool/read_file", json={"files": {"script.py": "x"}}).status_code == 404

    assert "## Параметры" not in (tmp_path / "tools" / "hello" / "tool.md").read_text(encoding="utf-8")
