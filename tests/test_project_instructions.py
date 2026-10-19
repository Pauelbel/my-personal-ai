"""Проверяет изоляцию инструкций проекта и обновление между ходами без перезапуска."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fakes import post_session

from api.app import create_app
from config.settings import Settings
from core.context import build_system_prompt
from core.project_instructions import load_project_instructions
from llm.models import ChatResult


def test_only_explicit_files_inside_workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "AGENTS.md").write_text("Родительское правило", encoding="utf-8")
    (root / "AGENTS.md").write_text("Правило проекта", encoding="utf-8-sig")
    instructions = root / ".github/instructions"
    instructions.mkdir(parents=True)
    (instructions / "learnings.instructions.md").write_text("Проверенный вывод", encoding="utf-8")
    (instructions / "other.instructions.md").write_text("Не загружать", encoding="utf-8")
    context = load_project_instructions(str(root))
    assert "Правило проекта" in context
    assert "Проверенный вывод" in context
    assert "Родительское правило" not in context
    assert "Не загружать" not in context
    assert load_project_instructions(None) == ""
    assert load_project_instructions(str(root / "missing")) == ""
    assert load_project_instructions(str(root), max_bytes=0) == ""


def test_invalid_and_oversized_files_are_skipped(tmp_path, caplog):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"\xff")
    assert load_project_instructions(str(tmp_path)) == ""
    path.write_bytes(b"a" * (64 * 1024 + 1))
    assert load_project_instructions(str(tmp_path)) == ""
    assert "64 КБ" in caplog.text


def test_budget_and_prompt_boundary(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Правило " * 3000, encoding="utf-8")
    context = load_project_instructions(str(tmp_path), max_bytes=400)
    assert len(context.encode("utf-8")) <= 400
    assert "обрезаны" in context
    prompt = build_system_prompt("Системное правило", project_context=context)
    assert prompt.startswith("Системное правило")
    assert "не отменяет системные" in prompt
    assert context in prompt


def test_external_symlink_is_not_read(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("Секрет", encoding="utf-8")
    try:
        (root / "AGENTS.md").symlink_to(outside)
    except OSError:
        pytest.skip("Создание ссылок недоступно в этой среде")
    assert load_project_instructions(str(root)) == ""


class RecordingProvider:
    def __init__(self):
        self.prompts = []

    async def chat(self, model, messages, tools=None):
        self.prompts.append(messages[0].content)
        return ChatResult(content="Ответ")

    async def close(self):
        pass


def test_next_turn_reloads_and_workspace_switch_does_not_leak(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    third = tmp_path / "third"
    third.mkdir()
    rule = first / "AGENTS.md"
    rule.write_text("ПЕРВОЕ_ПРАВИЛО", encoding="utf-8")
    (second / "AGENTS.md").write_text("ДРУГОЙ_ПРОЕКТ", encoding="utf-8")
    settings = Settings(
        sessions_path=tmp_path / "sessions", projects_path=tmp_path / "projects",
        conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory",
        memory_auto_update_messages=0, _env_file=None,
    )
    provider = RecordingProvider()
    with TestClient(create_app(settings, llm_provider=provider)) as client:
        sessions = {}

        def turn(workspace: Path):
            if workspace not in sessions:
                project_id = client.post("/api/projects", json={
                    "name": workspace.name, "workspace": str(workspace),
                }).json()["id"]
                session = post_session(client, {
                    "model": "test-model", "project_id": project_id,
                }).json()
                sessions[workspace] = session["id"]
            url = f"/api/sessions/{sessions[workspace]}"
            before = len(provider.prompts)
            assert client.post(url + "/turns", json={"content": "Проверка"}).status_code == 200
            # После ответа в фоне может прийти отдельный запрос суммаризации; нужен промпт самого хода.
            return next(prompt for prompt in provider.prompts[before:] if prompt.startswith("Ты Meepo"))

        assert "ПЕРВОЕ_ПРАВИЛО" in turn(first)
        rule.write_text("ОБНОВЛЁННОЕ_ПРАВИЛО", encoding="utf-8")
        updated = turn(first)
        assert "ОБНОВЛЁННОЕ_ПРАВИЛО" in updated
        assert "ПЕРВОЕ_ПРАВИЛО" not in updated
        learnings = first / ".github/instructions/learnings.instructions.md"
        learnings.parent.mkdir(parents=True)
        learnings.write_text("СОХРАНЁННЫЙ_ВЫВОД", encoding="utf-8")
        assert "СОХРАНЁННЫЙ_ВЫВОД" in turn(first)
        switched = turn(second)
        assert "ДРУГОЙ_ПРОЕКТ" in switched
        assert "ОБНОВЛЁННОЕ_ПРАВИЛО" not in switched
        assert "СОХРАНЁННЫЙ_ВЫВОД" not in switched
        # Проект без своих правил не получает правила других проектов.
        assert "ДРУГОЙ_ПРОЕКТ" not in turn(third)
        rule.unlink()
        assert "ОБНОВЛЁННОЕ_ПРАВИЛО" not in turn(first)
