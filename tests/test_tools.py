"""Проверки подтверждают общие разрешения и границы файловых инструментов."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.tools.files import EditFileTool, ListFilesTool, ReadFileTool, WriteFileTool


def test_file_tools_drop_markdown_wrapping_from_path(tmp_path):
    """Модель копирует путь из текста «(`test_results.md`)» вместе с кавычкой и скобкой — файл ложится как надо."""
    root = tmp_path / "workspace"
    (root / "docs (old)").mkdir(parents=True)

    async def exercise():
        tool = WriteFileTool(tmp_path / "memory")
        assert not (await tool.execute({"path": "test_results.md`)", "content": "Итоги"}, root)).is_error
        assert not (await tool.execute({"path": "(`docs (old)/README.md`),", "content": "Описание"}, root)).is_error
        assert (await tool.execute({"path": "a`b.md", "content": "x"}, root)).is_error
        read = await ReadFileTool().execute({"path": "`test_results.md`"}, root)
        assert read.content == "Итоги"

    asyncio.run(exercise())
    assert sorted(path.name for path in root.rglob("*.md")) == ["README.md", "test_results.md"]
    assert (root / "docs (old)" / "README.md").read_text(encoding="utf-8") == "Описание"


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


def test_path_copied_from_breadcrumbs_with_spaces_around_slashes_is_found(tmp_path):
    folder = tmp_path / "softwlc_md" / "02_Модули SoftWLC" / "01_Личный Кабинет"
    folder.mkdir(parents=True)
    (folder / "index.md").write_text("Личный кабинет B2B", encoding="utf-8")

    read = asyncio.run(ReadFileTool().execute(
        {"path": "softwlc_md / 02_Модули SoftWLC / 01_Личный Кабинет /\nindex.md"}, tmp_path,
    ))
    assert not read.is_error and read.content == "Личный кабинет B2B"
