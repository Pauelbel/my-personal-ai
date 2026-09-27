"""Проверки подтверждают общие разрешения и границы файловых инструментов."""

import asyncio

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.models import ChatResult, ToolCall
from local_agent.tools.filesystem import ListFilesTool, ReadFileTool


class ToolCallingProvider:
    def __init__(self) -> None:
        self.calls = []

    async def chat(self, model, messages, tools=None):
        self.calls.append((messages, tools))
        if tools and messages[-1].role != "tool":
            return ChatResult(
                content=None,
                tool_calls=(ToolCall("call-1", "read_file", '{"path":"note.txt"}'),),
            )
        return ChatResult(content="Файл прочитан")

    async def list_models(self):
        return ["test-model"]

    async def close(self):
        pass


def test_session_tools_persist(tmp_path):
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)
    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        other = client.post("/api/sessions", json={}).json()
        tools = client.get(f"/api/sessions/{session['id']}/tools").json()
        assert [tool["id"] for tool in tools] == [
            "list_files", "read_file", "search_files", "write_file"
        ]
        assert not any(tool["enabled"] for tool in tools)
        updated = client.put(
            f"/api/sessions/{session['id']}/tools/read_file",
            json={"enabled": True},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is True
        assert client.put(
            f"/api/sessions/{session['id']}/tools/unknown", json={"enabled": True}
        ).status_code == 404
    with TestClient(create_app(settings)) as client:
        tools = client.get(f"/api/sessions/{session['id']}/tools").json()
        assert [tool["enabled"] for tool in tools] == [False, True, False, False]
        other_tools = client.get(f"/api/sessions/{other['id']}/tools").json()
        assert not any(tool["enabled"] for tool in other_tools)


def test_file_tools_stay_inside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("Привет", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    async def exercise():
        listing = await ListFilesTool().execute({"path": "."}, root)
        assert "note.txt" in listing.content
        read = await ReadFileTool().execute({"path": "note.txt"}, root)
        assert read.content == "Привет"
        escape = await ReadFileTool().execute({"path": "../secret.txt"}, root)
        assert escape.is_error
        assert "secret" not in escape.content

    asyncio.run(exercise())


def test_model_only_receives_enabled_tools_and_result(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("Содержимое", encoding="utf-8")
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)
    provider = ToolCallingProvider()
    with TestClient(create_app(settings, llm_provider=provider)) as client:
        session = client.post(
            "/api/sessions",
            json={"model": "test-model", "workspace": str(workspace)},
        ).json()
        first = client.post(f"/api/sessions/{session['id']}/turns", json={"content": "Привет"})
        assert first.status_code == 200
        assert provider.calls[0][1] is None
        client.put(f"/api/sessions/{session['id']}/tools/read_file", json={"enabled": True})
        second = client.post(f"/api/sessions/{session['id']}/turns", json={"content": "Прочитай файл"})
        assert second.status_code == 200
        assert second.json()["content"] == "Файл прочитан"
        assert provider.calls[1][1][0]["function"]["name"] == "read_file"
        assert provider.calls[2][0][-1].content == "Содержимое"
        no_workspace = client.post(
            "/api/sessions", json={"model": "test-model"}
        ).json()
        assert client.post(
            f"/api/sessions/{no_workspace['id']}/turns", json={"content": "Прочитай файл"}
        ).status_code == 200
        assert provider.calls[3][1] is None
        client.put(f"/api/sessions/{session['id']}/tools/read_file", json={"enabled": False})
        assert client.post(
            f"/api/sessions/{session['id']}/turns", json={"content": "Прочитай ещё раз"}
        ).status_code == 200
        assert provider.calls[4][1] is None
