"""Скиллы: загрузка из папок, выдача модели через use_skill, запись, архив и проверка файлов."""

import asyncio

import pytest

from local_agent.core.toolbox import UseSkillTool
from local_agent.skills.files import (
    archive_skill,
    archived_skills,
    load_skills,
    restore_skill,
    save_files,
    write_skill,
)
from local_agent.skills.registry import SkillRegistry

SKILL = "---\nname: Проверка\ndescription: проверить что-нибудь\n---\nШаг 1. Шаг 2.\n"


def write(root, name, text, steps=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    if steps is not None:
        (folder / "steps.md").write_text(steps, encoding="utf-8")


def test_system_starts_without_skills(tmp_path):
    root = tmp_path / "skills"

    assert load_skills(root) == []
    assert root.is_dir()


@pytest.mark.parametrize(("name", "content", "message"), [
    ("no-description", "---\nname: X\n---\nТекст\n", "description"),
    ("empty", "---\ndescription: d\n---\n\n", "пустые инструкции"),
    ("Bad Name", SKILL, "имя папки"),
    ("unknown-key", "---\ndescription: d\ntools: git_log\n---\nТекст\n", "неизвестный параметр"),
])
def test_invalid_skill_folder_is_reported(tmp_path, name, content, message):
    write(tmp_path, name, content)

    with pytest.raises(ValueError, match=message):
        load_skills(tmp_path)


def test_skill_with_steps_may_have_no_instructions_but_steps_must_parse(tmp_path):
    steps = "## Свести\nмодель: Сведи документы.\n"
    write(tmp_path, "summary", "---\ndescription: сводка\n---\n", steps)
    [skill] = load_skills(tmp_path)
    write(tmp_path, "broken", "---\ndescription: d\n---\nТекст\n", "## Шаг без вида\nпросто текст\n")

    assert skill.instructions == "" and [step.title for step in skill.steps.steps] == ["Свести"]
    with pytest.raises(ValueError, match="steps.md"):
        load_skills(tmp_path)


def test_use_skill_returns_instructions_and_points_to_steps(tmp_path):
    write(tmp_path, "check", SKILL)
    write(tmp_path, "docs", SKILL.replace("Проверка", "Документы"), "## Нужно от вас\n- папка — где\n\n## Свести\nмодель: Сведи {папка}.\n")
    tool = UseSkillTool(SkillRegistry(load_skills(tmp_path)))

    found = asyncio.run(tool.execute({"name": "check"}, None))
    steps = asyncio.run(tool.execute({"name": "docs"}, None))
    missing = asyncio.run(tool.execute({"name": "../check"}, None))

    assert "Шаг 1. Шаг 2." in found.content and not found.is_error and "run_skill" not in found.content
    assert "запусти run_skill со skill docs и inputs: папка" in steps.content
    assert missing.is_error and "check" in missing.content
    assert tool.parameters["properties"]["name"]["enum"] == ["check", "docs"]


def test_use_skill_warns_about_missing_tools(tmp_path):
    write(tmp_path, "overview", "---\ndescription: обзор\n---\nВызови list_files, затем read_file.\n")
    registry = SkillRegistry(load_skills(tmp_path))
    known = {"list_files", "read_file", "write_file"}

    without = asyncio.run(UseSkillTool(registry, known, set()).execute({"name": "overview"}, None))
    partial = asyncio.run(UseSkillTool(registry, known, {"list_files"}).execute({"name": "overview"}, None))
    full = asyncio.run(UseSkillTool(registry, known, {"list_files", "read_file"}).execute({"name": "overview"}, None))

    assert "недоступны инструменты, которые нужны скиллу: list_files, read_file" in without.content
    assert "нужны скиллу: read_file." in partial.content
    # write_file скиллу не нужен, а нужные есть: предупреждения нет.
    assert "ВНИМАНИЕ" not in full.content


@pytest.mark.parametrize("fields", [
    ("", "d", "текст"), ("Имя", "", "текст"), ("Имя", "d", " "), ("Имя\ndescription: x", "d", "т"), ("Имя", "d\rx", "т"),
])
def test_skill_writing_rejects_empty_fields_and_header_injection(tmp_path, fields):
    with pytest.raises(ValueError):
        write_skill(tmp_path, "notes", *fields)
    assert load_skills(tmp_path) == []


def test_skill_update_keeps_steps_and_scripts_that_were_not_passed(tmp_path):
    steps = "## Сделать\nскрипт: work.py\n"
    write_skill(tmp_path, "notes", "Заметки", "Как вести заметки", "1. Записывай.", steps, {"work.py": "print('a')"})
    updated = write_skill(tmp_path, "notes", "Заметки", "Как вести заметки", "1. Записывай кратко.")

    assert updated.instructions == "1. Записывай кратко." and updated.steps is not None
    assert (tmp_path / "notes" / "scripts" / "work.py").read_text(encoding="utf-8") == "print('a')\n"
    # Сломанный скрипт не попадает на диск, папка остаётся прежней.
    with pytest.raises(ValueError, match="scripts/work.py, строка 1"):
        write_skill(tmp_path, "notes", "Заметки", "Как", "текст", scripts={"work.py": "def ("})
    assert (tmp_path / "notes" / "scripts" / "work.py").read_text(encoding="utf-8") == "print('a')\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["notes"]


def test_skill_files_cannot_leave_the_folder_or_lose_skill_md(tmp_path):
    write_skill(tmp_path, "notes", "Заметки", "Как вести заметки", "1. Записывай.")

    for files in ({"../outside.md": "x"}, {"scripts/sub/x.py": "x"}, {"other.md": "x"}, {"SKILL.md": None}):
        with pytest.raises(ValueError):
            save_files(tmp_path, "notes", files)
    assert not (tmp_path / "outside.md").exists()


def test_skill_is_archived_and_restored(tmp_path):
    write_skill(tmp_path, "notes", "Заметки", "Как вести заметки", "1. Записывай кратко.")
    archive_skill(tmp_path, "notes")
    [(archive_id, archived)] = archived_skills(tmp_path)

    assert load_skills(tmp_path) == [] and archived.instructions == "1. Записывай кратко."
    assert restore_skill(tmp_path, archive_id).id == "notes"
    assert [skill.id for skill in load_skills(tmp_path)] == ["notes"]
