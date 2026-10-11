"""Навыки: загрузка из файлов, выдача модели через use_skill и правка из UI."""

import asyncio

import pytest

from local_agent.agent.loader import archive_skill, archived_skills, load_skills, restore_skill, write_skill
from local_agent.agent.registry import SkillRegistry
from local_agent.tools.skills import UseSkillTool

SKILL = "---\nname: Проверка\ndescription: проверить что-нибудь\n---\nШаг 1. Шаг 2.\n"


def test_system_starts_without_skills(tmp_path):
    root = tmp_path / "skills"

    assert load_skills(root) == []
    assert root.is_dir()


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


@pytest.mark.parametrize("fields", [
    ("", "d", "текст"), ("Имя", "", "текст"), ("Имя", "d", " "), ("Имя\ndescription: x", "d", "т"), ("Имя", "d\rx", "т"),
])
def test_skill_writing_rejects_empty_fields_and_header_injection(tmp_path, fields):
    with pytest.raises(ValueError):
        write_skill(tmp_path, "notes", *fields)
    assert load_skills(tmp_path) == []


def test_skill_is_written_archived_and_restored(tmp_path):
    write_skill(tmp_path, "notes", "Заметки", "Как вести заметки", "1. Записывай.")
    write_skill(tmp_path, "notes", "Заметки", "Как вести заметки", "1. Записывай кратко.")
    archive_skill(tmp_path, "notes")
    [(archive_id, archived)] = archived_skills(tmp_path)

    assert load_skills(tmp_path) == [] and archived.instructions == "1. Записывай кратко."
    assert restore_skill(tmp_path, archive_id).id == "notes"
    assert [skill.id for skill in load_skills(tmp_path)] == ["notes"]
