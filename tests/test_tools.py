"""Проверки подтверждают общие разрешения и границы файловых инструментов."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.models import ChatResult, ToolCall
from local_agent.tools.filesystem import EditFileTool, ListFilesTool, ReadFileTool, WriteFileTool


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
        # Обе сессии без проекта попадают в «Черновики»: у них нет инструментов, и переключатель общий.
        session = client.post("/api/sessions", json={}).json()
        other = client.post("/api/sessions", json={}).json()
        tools = client.get(f"/api/sessions/{session['id']}/tools").json()
        assert [tool["id"] for tool in tools] == [
            "list_files", "read_file", "search_files", "write_file", "edit_file", "git", "search_docs",
            "generate_test_cases",
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
        assert [tool["enabled"] for tool in tools] == [False, True, False, False, False, False, False, False]
        other_tools = client.get(f"/api/sessions/{other['id']}/tools").json()
        assert [tool["id"] for tool in other_tools if tool["enabled"]] == ["read_file"]


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
        project = client.post("/api/projects", json={"name": "Проект", "workspace": str(workspace)}).json()
        session = client.post(
            "/api/sessions",
            json={"model": "test-model", "project_id": project["id"]},
        ).json()
        # Новый проект начинает с инструментов чтения; выключаем их, чтобы проверить путь с нуля.
        defaults = [tool["id"] for tool in client.get(f"/api/sessions/{session['id']}/tools").json() if tool["enabled"]]
        assert defaults == ["list_files", "read_file", "search_files", "git", "search_docs"]
        for tool_id in defaults:
            client.put(f"/api/sessions/{session['id']}/tools/{tool_id}", json={"enabled": False})
        first = client.post(f"/api/sessions/{session['id']}/turns", json={"content": "Привет"})
        assert first.status_code == 200
        assert provider.calls[0][1] is None
        client.put(f"/api/sessions/{session['id']}/tools/read_file", json={"enabled": True})
        second = client.post(f"/api/sessions/{session['id']}/turns", json={"content": "Прочитай файл"})
        assert second.status_code == 200
        assert second.json()["content"] == "Файл прочитан"
        assert provider.calls[1][1][0]["function"]["name"] == "read_file"
        assert provider.calls[2][0][-1].content == "Содержимое"
        draft = client.post(
            "/api/sessions", json={"model": "test-model"}
        ).json()
        assert client.post(
            f"/api/sessions/{draft['id']}/turns", json={"content": "Прочитай файл"}
        ).status_code == 200
        assert provider.calls[3][1] is None
        client.put(f"/api/sessions/{session['id']}/tools/read_file", json={"enabled": False})
        assert client.post(
            f"/api/sessions/{session['id']}/turns", json={"content": "Прочитай ещё раз"}
        ).status_code == 200
        assert provider.calls[4][1] is None


def edit(workspace, **arguments):
    return asyncio.run(EditFileTool().execute(arguments, workspace))


def test_edit_file_replaces_unique_fragment(tmp_path):
    (tmp_path / "app.py").write_text("a = 1\nb = 2\n", encoding="utf-8")

    result = edit(tmp_path, path="app.py", old_text="b = 2", new_text="b = 3")

    assert not result.is_error
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "a = 1\nb = 3\n"
    assert EditFileTool.requires_approval is True


def test_edit_file_keeps_crlf(tmp_path):
    (tmp_path / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = edit(tmp_path, path="win.txt", old_text="one\ntwo", new_text="one\n2\nextra")

    assert not result.is_error
    assert (tmp_path / "win.txt").read_bytes() == b"one\r\n2\r\nextra\r\nthree\r\n"


@pytest.mark.parametrize(("old_text", "message"), [("missing", "не найден"), ("x", "встречается 2")])
def test_edit_file_requires_exactly_one_match(tmp_path, old_text, message):
    (tmp_path / "file.txt").write_text("x\nx\n", encoding="utf-8")

    result = edit(tmp_path, path="file.txt", old_text=old_text, new_text="y")

    assert result.is_error and message in result.content
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "x\nx\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/abs.txt", "missing.txt", "."])
def test_edit_file_stays_inside_workspace(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    result = edit(workspace, path=path, old_text="secret", new_text="changed")

    assert result.is_error
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "secret"


@pytest.mark.parametrize(("content", "old_text", "new_text", "expected"), [
    # Строка таблицы остаётся, новые строки встают после неё.
    ("| a |\n| b |\n", "| a |", "| new |", "| a |\n| new |\n| b |\n"),
    # old_text — часть строки: вставка всё равно после всей строки.
    ("| a | x |\n| b |\n", "| a", "| new |\n", "| a | x |\n| new |\n| b |\n"),
    ("one\ntwo", "two", "three", "one\ntwo\nthree"),
    ("one\ntwo\n", "one\n", "1\n2", "one\n1\n2\ntwo\n"),
])
def test_edit_file_inserts_after_line_without_replacing(tmp_path, content, old_text, new_text, expected):
    (tmp_path / "note.md").write_text(content, encoding="utf-8")

    result = edit(tmp_path, path="note.md", old_text=old_text, new_text=new_text, insert_after=True)

    assert not result.is_error
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == expected


def test_edit_file_insert_keeps_crlf(tmp_path):
    (tmp_path / "win.md").write_bytes(b"one\r\ntwo\r\n")

    assert not edit(tmp_path, path="win.md", old_text="one", new_text="new", insert_after=True).is_error
    assert (tmp_path / "win.md").read_bytes() == b"one\r\nnew\r\ntwo\r\n"


@pytest.mark.parametrize("arguments", [
    {"old_text": "", "new_text": "y"},
    {"old_text": "x", "new_text": None},
    {"old_text": "x", "new_text": "\n", "insert_after": True},
    {"old_text": "x", "new_text": "y", "insert_after": "yes"},
])
def test_edit_file_validates_arguments(tmp_path, arguments):
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")

    assert edit(tmp_path, path="file.txt", **arguments).is_error


def test_file_write_tools_cannot_change_memory(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "data" / "memory"
    memory.mkdir(parents=True)
    path = memory / "preferences.md"
    path.write_text("# Предпочтения\n", encoding="utf-8")

    async def exercise():
        write = await WriteFileTool(memory).execute(
            {"path": "data/memory/preferences.md", "content": "Удалено"}, workspace,
        )
        edit_result = await EditFileTool(memory).execute(
            {"path": "data/memory/preferences.md", "old_text": "Предпочтения", "new_text": "Удалено"},
            workspace,
        )
        new_file = await WriteFileTool(memory).execute(
            {"path": "data/memory/new.md", "content": "Новая память"}, workspace,
        )
        ordinary = await WriteFileTool(memory).execute(
            {"path": "note.txt", "content": "Обычный файл"}, workspace,
        )
        assert write.is_error and edit_result.is_error and new_file.is_error
        assert not ordinary.is_error

    asyncio.run(exercise())
    assert path.read_text(encoding="utf-8") == "# Предпочтения\n"
    assert not (memory / "new.md").exists()


def test_app_configures_memory_write_protection(tmp_path):
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        tool = client.app.state.tool_registry.get("write_file")
        result = asyncio.run(tool.execute(
            {"path": "memory/preferences.md", "content": "Удалено"}, tmp_path,
        ))
        assert result.is_error
        assert "только через раздел" in result.content
        assert (settings.memory_path / "preferences.md").read_text(encoding="utf-8") == "# Предпочтения\n"
