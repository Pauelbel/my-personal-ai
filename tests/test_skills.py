"""Навыки: загрузка из файлов, выдача модели через use_skill и правка из UI."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from local_agent.agent.loader import DEFAULT_SKILLS, load_skills
from local_agent.agent.registry import SkillRegistry
from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.models import ChatResult
from local_agent.tools.skills import UseSkillTool

SKILL = "---\nname: Проверка\ndescription: проверить что-нибудь\n---\nШаг 1. Шаг 2.\n"


def test_default_skills_are_copied_only_once(tmp_path):
    root = tmp_path / "skills"

    first = load_skills(root)
    (root / f"{first[0].id}.md").unlink()
    second = load_skills(root)

    assert {skill.id for skill in first} == {path.stem for path in DEFAULT_SKILLS.glob("*.md")}
    # Удалённый пользователем навык не возвращается при следующем запуске.
    assert len(second) == len(first) - 1


@pytest.mark.parametrize(("name", "content", "message"), [
    ("no-description.md", "---\nname: X\n---\nТекст\n", "description"),
    ("empty.md", "---\ndescription: d\n---\n\n", "пустые инструкции"),
    ("Bad Name.md", SKILL, "имя файла"),
    ("unknown-key.md", "---\ndescription: d\ntools: git_log\n---\nТекст\n", "неизвестный параметр"),
])
def test_invalid_skill_file_is_reported(tmp_path, name, content, message):
    (tmp_path / name).write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_skills(tmp_path)


def test_use_skill_returns_instructions(tmp_path):
    (tmp_path / "check.md").write_text(SKILL, encoding="utf-8")
    tool = UseSkillTool(SkillRegistry(load_skills(tmp_path)))

    found = asyncio.run(tool.execute({"name": "check"}, None))
    missing = asyncio.run(tool.execute({"name": "../check"}, None))

    assert "Шаг 1. Шаг 2." in found.content and not found.is_error
    assert missing.is_error and "check" in missing.content
    assert tool.parameters["properties"]["name"]["enum"] == ["check"]


def test_skill_creation_is_available_immediately_and_survives_restart(tmp_path):
    configured = settings(tmp_path)
    app = create_app(configured)
    with TestClient(app) as client:
        created = client.post("/api/skills", json={
            "name": " Мой скилл ", "description": " Проверять результат ",
            "instructions": "# Проверка\n\n1. Прочитай файл.\n",
        })
        assert created.status_code == 201
        skill = created.json()
        tool = UseSkillTool(app.state.skill_registry)
        result = asyncio.run(tool.execute({"name": skill["id"]}, None))
        assert not result.is_error and "# Проверка" in result.content
        assert client.get(f"/api/skills/{skill['id']}").json()["name"] == "Мой скилл"
    with TestClient(create_app(configured)) as client:
        loaded = client.get(f"/api/skills/{skill['id']}").json()
        assert loaded["description"] == "Проверять результат"
        assert loaded["instructions"] == "# Проверка\n\n1. Прочитай файл."


def test_skill_deletion_archives_file_and_removes_it_from_agent(tmp_path):
    configured = settings(tmp_path)
    app = create_app(configured)
    with TestClient(app) as client:
        created = client.post("/api/skills", json={
            "name": "Удаляемый", "description": "Проверка удаления", "instructions": "# Текст",
        }).json()
        skill_id = created["id"]
        assert client.delete(f"/api/skills/{skill_id}").status_code == 204
        assert client.get(f"/api/skills/{skill_id}").status_code == 404
        tool = UseSkillTool(app.state.skill_registry)
        assert skill_id not in tool.parameters["properties"]["name"]["enum"]
        assert asyncio.run(tool.execute({"name": skill_id}, None)).is_error
        assert client.delete(f"/api/skills/{skill_id}").status_code == 404
    archive = list((tmp_path / "skills" / ".deleted").glob(f"{skill_id}.*.md"))
    assert len(archive) == 1 and "# Текст" in archive[0].read_text(encoding="utf-8")
    assert not (tmp_path / "skills" / f"{skill_id}.md").exists()
    with TestClient(create_app(configured)) as client:
        assert client.get(f"/api/skills/{skill_id}").status_code == 404


def test_archive_lists_restores_and_clears_only_deleted_skills(tmp_path):
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        skill = client.post("/api/skills", json={
            "name": "Архивный", "description": "Архив", "instructions": "# Текст",
        }).json()
        assert client.delete(f"/api/skills/{skill['id']}").status_code == 204
        archived = client.get("/api/skills/archive").json()
        assert len(archived) == 1 and archived[0]["name"] == "Архивный"
        restored = client.post(f"/api/skills/archive/{archived[0]['archive_id']}/restore")
        assert restored.status_code == 200 and restored.json()["id"] == skill["id"]
        assert client.get("/api/skills/archive").json() == []
        assert client.get(f"/api/skills/{skill['id']}").status_code == 200
        assert client.delete(f"/api/skills/{skill['id']}").status_code == 204
        unrelated = tmp_path / "skills" / ".deleted" / "notes.txt"
        unrelated.write_text("Не удалять", encoding="utf-8")
        assert client.delete("/api/skills/archive").json() == {"deleted": 1}
        assert client.get("/api/skills/archive").json() == []
        assert unrelated.read_text(encoding="utf-8") == "Не удалять"
        assert client.get(f"/api/skills/{skill['id']}").status_code == 404


def test_archive_restore_refuses_existing_skill(tmp_path):
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        skill = client.post("/api/skills", json={
            "name": "Архивный", "description": "Архив", "instructions": "Исходный текст",
        }).json()
        client.delete(f"/api/skills/{skill['id']}")
        archive_id = client.get("/api/skills/archive").json()[0]["archive_id"]
        path = tmp_path / "skills" / f"{skill['id']}.md"
        path.write_text("---\nname: Другой\ndescription: Другой\n---\nНовый текст\n", encoding="utf-8")
        response = client.post(f"/api/skills/archive/{archive_id}/restore")
        assert response.status_code == 409
        assert "Новый текст" in path.read_text(encoding="utf-8")
        assert len(client.get("/api/skills/archive").json()) == 1


@pytest.mark.parametrize("fields", [
    {"name": "   "}, {"description": "   "}, {"instructions": "   "},
    {"name": "Имя\ntools: write_file"}, {"description": "Описание\rname: X"},
])
def test_skill_creation_rejects_empty_fields_and_header_injection(tmp_path, fields):
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        before = client.get("/api/skills").json()
        result = client.post("/api/skills", json={
            "name": "Имя", "description": "Описание", "instructions": "Текст", **fields,
        })
        assert result.status_code == 400
        assert client.get("/api/skills").json() == before


def test_use_skill_warns_about_missing_tools(tmp_path):
    (tmp_path / "overview.md").write_text(
        "---\ndescription: обзор\n---\nВызови list_files, затем read_file.\n", encoding="utf-8"
    )
    registry = SkillRegistry(load_skills(tmp_path))
    known = {"list_files", "read_file", "write_file"}

    without = asyncio.run(UseSkillTool(registry, known, set()).execute({"name": "overview"}, None))
    partial = asyncio.run(UseSkillTool(registry, known, {"list_files"}).execute({"name": "overview"}, None))
    full = asyncio.run(UseSkillTool(registry, known, {"list_files", "read_file"}).execute({"name": "overview"}, None))

    assert "недоступны инструменты, которые нужны навыку: list_files, read_file" in without.content
    assert "нужны навыку: read_file." in partial.content
    # write_file навыку не нужен, а нужные есть: предупреждения нет.
    assert "ВНИМАНИЕ" not in full.content


class RecordingProvider:
    def __init__(self) -> None:
        self.calls = []

    async def chat(self, model, messages, tools=None):
        self.calls.append((messages, tools))
        return ChatResult(content="Готово")

    async def list_models(self):
        return ["test-model"]

    async def close(self):
        pass


def settings(tmp_path) -> Settings:
    return Settings(
        agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory",
        skills_path=tmp_path / "skills", _env_file=None,
    )


def test_skills_are_offered_without_workspace(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "check.md").write_text(SKILL, encoding="utf-8")
    provider = RecordingProvider()

    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test-model"}).json()
        assert client.post(f"/api/sessions/{session['id']}/turns", json={"content": "Привет"}).status_code == 200

    messages, tools = provider.calls[0]
    assert [tool["function"]["name"] for tool in tools] == ["use_skill"]
    assert "- check: проверить что-нибудь" in messages[0].content


def test_skill_instructions_can_be_edited(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "check.md").write_text(SKILL, encoding="utf-8")

    with TestClient(create_app(settings(tmp_path))) as client:
        listed = client.get("/api/skills").json()
        saved = client.put("/api/skills/check", json={"instructions": "  Новые шаги  "})
        empty = client.put("/api/skills/check", json={"instructions": "  "})
        unknown = client.put("/api/skills/..%2Fagents%2Fdefault", json={"instructions": "x"})
        read = client.get("/api/skills/check").json()

    assert listed == [{"id": "check", "name": "Проверка", "description": "проверить что-нибудь"}]
    assert saved.status_code == 200 and read["instructions"] == "Новые шаги"
    assert empty.status_code == 400
    assert unknown.status_code == 404
    # Заголовок с названием и описанием сохраняется как был.
    assert (tmp_path / "skills" / "check.md").read_text(encoding="utf-8") == (
        "---\nname: Проверка\ndescription: проверить что-нибудь\n---\nНовые шаги\n"
    )
