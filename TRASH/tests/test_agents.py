"""Специализированные агенты: файлы, реестр и карточка делегирования."""

from agents.files import (
    AGENT_FILE,
    TESTCASES_MIGRATION,
    archive_agent,
    archived_agents,
    initialize_agents,
    load_agents,
    restore_agent,
    save_files,
)
from agents.registry import AgentRegistry
from agents.runtime import LEGACY_TESTCASES_LAUNCHER, TESTCASES_LAUNCHER
from steps.engine import Toolkit
from tools.agents import DelegateAgentTool


def test_three_default_agents_are_installed_once_and_editable(tmp_path):
    agents = initialize_agents(tmp_path)
    assert [agent.id for agent in agents] == ["browser-qa", "test-cases", "test-review"]
    reviewer = next(agent for agent in agents if agent.id == "test-review")
    assert reviewer.steps is not None and "edit_file" in reviewer.tools

    text = (reviewer.folder / AGENT_FILE).read_text(encoding="utf-8").replace("строгий ревьюер", "ведущий ревьюер")
    changed = save_files(tmp_path, reviewer.id, {AGENT_FILE: text})
    assert "ведущий ревьюер" in changed.system_prompt

    archive_agent(tmp_path, "test-review")
    initialize_agents(tmp_path)
    assert "test-review" not in {agent.id for agent in load_agents(tmp_path)}
    [(archive_id, _)] = archived_agents(tmp_path)
    assert restore_agent(tmp_path, archive_id).id == "test-review"


def test_delegate_card_contains_agent_inputs_and_effects(tmp_path):
    registry = AgentRegistry(initialize_agents(tmp_path))
    tool = DelegateAgentTool(registry, None, Toolkit(lambda name: None, lambda: []), 8192)
    arguments = {"agent": "test-review", "inputs": {"тест-кейсы": "cases.md", "требования": "spec.md"}}

    assert tool.validate(arguments) is None
    card = tool.approval_payload(arguments)
    assert card["title"] == "Поручить агенту: «QA-инженер · ревью тест-кейсов»"
    assert card["run"]["values"] == {"тест-кейсы": "cases.md", "требования": "spec.md"}
    assert card["run"]["steps"][0]["title"] == "Проверить и заполнить вопросы"


def test_agent_tools_are_an_upper_bound_for_step_tools(tmp_path):
    class FakeTool:
        def __init__(self, tool_id, server=None):
            self.id, self.server = tool_id, server

    registry = AgentRegistry(initialize_agents(tmp_path))
    available = [FakeTool("read_file"), FakeTool("write_file"), FakeTool("browser_click", "browser")]
    tool = DelegateAgentTool(
        registry, None,
        Toolkit(lambda name: next((item for item in available if item.id == name), None), lambda: available),
        8192,
    )

    test_cases = tool._kit_for(registry.get("test-cases"))
    assert test_cases.tool("read_file") is not None
    assert test_cases.tool("write_file") is None
    assert test_cases.tool("browser_click") is None

    browser = tool._kit_for(registry.get("browser-qa"))
    assert browser.tool("browser_click") is not None
    assert browser.tool("write_file") is not None


def test_initialized_agents_migrate_only_the_legacy_launcher_and_keep_a_backup(tmp_path):
    initialize_agents(tmp_path)
    folder = tmp_path / "test-cases"
    script = folder / "scripts" / "testcases.py"
    original = LEGACY_TESTCASES_LAUNCHER.replace("\n", "\r\n").encode("utf-8")
    script.write_bytes(original)
    instructions = (folder / AGENT_FILE).read_text(encoding="utf-8") + "\nПравило пользователя.\n"
    (folder / AGENT_FILE).write_text(instructions, encoding="utf-8")
    steps = (folder / "steps.md").read_bytes()
    extra = folder / "scripts" / "custom.py"
    extra.write_text("print('custom')\n", encoding="utf-8")

    initialize_agents(tmp_path)

    backup = tmp_path / TESTCASES_MIGRATION
    assert backup.read_bytes() == original
    assert script.read_text(encoding="utf-8") == TESTCASES_LAUNCHER
    assert (folder / AGENT_FILE).read_text(encoding="utf-8") == instructions
    assert (folder / "steps.md").read_bytes() == steps
    assert extra.read_text(encoding="utf-8") == "print('custom')\n"
    snapshot = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}

    initialize_agents(tmp_path)

    assert snapshot == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}


def test_launcher_migration_preserves_user_code_and_a_removed_script(tmp_path):
    initialize_agents(tmp_path)
    script = tmp_path / "test-cases" / "scripts" / "testcases.py"
    customized = LEGACY_TESTCASES_LAUNCHER + "\n# Пользовательская доработка\n"
    script.write_text(customized, encoding="utf-8")

    initialize_agents(tmp_path)

    assert script.read_text(encoding="utf-8") == customized
    assert not (tmp_path / ".migrations").exists()
    script.unlink()

    initialize_agents(tmp_path)

    assert not script.exists()


def test_archived_legacy_agent_stays_archived_and_is_migrated_when_restored(tmp_path):
    initialize_agents(tmp_path)
    script = tmp_path / "test-cases" / "scripts" / "testcases.py"
    script.write_text(LEGACY_TESTCASES_LAUNCHER, encoding="utf-8")
    archive_agent(tmp_path, "test-cases")
    [(archive_id, _)] = archived_agents(tmp_path)
    archived_script = tmp_path / ".deleted" / archive_id / "scripts" / "testcases.py"

    initialize_agents(tmp_path)

    assert not script.exists()
    assert archived_script.read_text(encoding="utf-8") == LEGACY_TESTCASES_LAUNCHER
    assert not (tmp_path / ".migrations").exists()

    restored = restore_agent(tmp_path, archive_id)

    assert restored.id == "test-cases"
    assert script.read_text(encoding="utf-8") == TESTCASES_LAUNCHER
    assert (tmp_path / TESTCASES_MIGRATION).read_text(encoding="utf-8") == LEGACY_TESTCASES_LAUNCHER
