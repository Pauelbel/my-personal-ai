"""Исправление агента: видимая разница, утверждение, версия и резервная копия."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import project_session, settings

from agents import files as agent_files
from agents.files import files_of, initialize_agents
from agents.registry import AgentRegistry
from agents.revisions import agent_revision, source_revision
from api.app import create_app
from catalog.service import CapabilityPaths, CapabilityService
from core.registry import ToolRegistry
from tools.agent_patch import MAX_DIFF_CHARS, ProposeAgentPatchTool
from tools.growth.propose import ReadCapabilityTool


def patch_service(tmp_path):
    root = tmp_path / "agents"
    registry = AgentRegistry(initialize_agents(root))
    capabilities = CapabilityService(ToolRegistry(), CapabilityPaths(tmp_path / "tools", root), agents=registry)
    return capabilities, ProposeAgentPatchTool(capabilities)


def patch_arguments(capabilities):
    agent = capabilities.agents.get("test-cases")
    return {
        "agent": agent.id, "expected_revision": agent_revision(agent),
        "reason": "Ошибка запуска скрипта: исправлена точка входа, задача остаётся у выбранного агента",
        "files": {"scripts/testcases.py": "print('готово')\n"},
    }


def test_revision_covers_source_names_and_content_and_read_capability_exposes_it(tmp_path):
    capabilities, _ = patch_service(tmp_path)
    agent = capabilities.agents.get("test-cases")
    revision = agent_revision(agent)
    assert revision == agent_revision(agent.folder) == source_revision(files_of(agent.folder))
    read = asyncio.run(ReadCapabilityTool(capabilities).execute({"kind": "agent", "name": agent.id}, None))
    assert f"revision: {revision}" in read.content and "propose_agent_patch" in read.content
    (agent.folder / "scripts/testcases.py").write_text("print('другая версия')\n", encoding="utf-8")
    assert agent_revision(agent) != revision
    assert source_revision({"a": "1", "b": "2"}) == source_revision({"b": "2", "a": "1"})
    assert source_revision({"a": "1"}) != source_revision({"b": "1"})


def test_patch_card_shows_changed_file_and_reason_without_changing_sources(tmp_path):
    capabilities, tool = patch_service(tmp_path)
    arguments = patch_arguments(capabilities)
    agent = capabilities.agents.get("test-cases")
    before = files_of(agent.folder)
    assert tool.validate(arguments) is None
    card = tool.approval_payload(arguments)
    assert card["kind"] == "agent_patch" and card["agent"] == "test-cases"
    assert card["reason"] == arguments["reason"] and card["expected_revision"] == arguments["expected_revision"]
    assert card["tools"] == list(agent.tools) and card["effects"] == agent.steps.effects()
    [change] = card["diffs"]
    assert change["path"] == "scripts/testcases.py" and change["action"] == "update"
    old_line = next(line for line in before["scripts/testcases.py"].splitlines() if line)
    assert f"-{old_line}" in change["diff"] and "+print('готово')" in change["diff"]
    assert files_of(agent.folder) == before and not (capabilities.paths.agents / ".history").exists()


@pytest.mark.parametrize("files, problem", [
    ({"../other/AGENT.md": "x"}, "Допустимы"),
    ({"scripts/../../outside.py": "x"}, "Допустимы"),
    ({"scripts/testcases.py": "def ("}, "syntax"),
    ({"steps.md": "## Сломано\nмодель: а\nскрипт: а.py\n"}, "два вида"),
    ({"AGENT.md": "---\nname: x\n---\nРоль\n"}, "описание"),
    ({"AGENT.md": None}, "удалить нельзя"),
    ({"steps.md": None}, "исполняемые шаги"),
    ({"scripts/testcases.py": None}, "нет scripts/testcases.py"),
    ({"scripts/testcases.py": 12}, "нужен текст"),
    ({"scripts/testcases.py": "# " + "x" * MAX_DIFF_CHARS}, "слишком большая"),
])
def test_invalid_patches_are_rejected_before_approval(tmp_path, files, problem):
    capabilities, tool = patch_service(tmp_path)
    arguments = {**patch_arguments(capabilities), "files": files}
    before = agent_revision(capabilities.agents.get("test-cases"))
    error = tool.validate(arguments)
    assert error and problem in error
    result = asyncio.run(tool.execute(arguments, None))
    assert result.is_error and agent_revision(capabilities.agents.get("test-cases")) == before
    assert not (capabilities.paths.agents / ".history").exists()


def test_execute_updates_same_agent_registry_and_keeps_persistent_source_backup(tmp_path):
    capabilities, tool = patch_service(tmp_path)
    arguments = patch_arguments(capabilities)
    agent = capabilities.agents.get("test-cases")
    before = files_of(agent.folder)
    result = asyncio.run(tool.execute(arguments, None))
    updated = capabilities.agents.get("test-cases")
    assert not result.is_error and updated.id == agent.id and updated.folder == agent.folder
    assert updated is not agent
    assert [entry.id for entry in capabilities.agents.all()] == ["browser-qa", "test-cases", "test-review"]
    revision = agent_revision(updated)
    assert result.meta == {"agent_patch": {"agent": "test-cases", "revision": revision}}
    assert revision != arguments["expected_revision"]
    assert (updated.folder / "scripts/testcases.py").read_text(encoding="utf-8") == "print('готово')\n"
    [backup] = list((capabilities.paths.agents / ".history/test-cases").iterdir())
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["revision"] == arguments["expected_revision"] and manifest["new_revision"] == revision
    assert manifest["status"] == "applied" and manifest["reason"] == arguments["reason"]
    assert files_of(backup / "files") == before
    # Повторное применение старого утверждения не перезаписывает новую версию.
    assert asyncio.run(tool.execute(arguments, None)).is_error
    assert len(list(backup.parent.iterdir())) == 1


def test_new_script_and_steps_are_validated_together_and_unused_script_can_be_deleted(tmp_path):
    capabilities, tool = patch_service(tmp_path)
    arguments = patch_arguments(capabilities)
    steps = files_of(capabilities.agents.get("test-cases").folder)["steps.md"]
    arguments["files"] = {
        "steps.md": steps.replace("testcases.py", "fixed.py"),
        "scripts/fixed.py": "print('готово')\n", "scripts/testcases.py": None,
    }
    assert tool.validate(arguments) is None
    card = tool.approval_payload(arguments)
    assert {change["action"] for change in card["diffs"]} == {"update", "create", "delete"}
    result = asyncio.run(tool.execute(arguments, None))
    assert not result.is_error
    agent = capabilities.agents.get("test-cases")
    assert agent.steps.steps[0].text == "fixed.py"
    assert not (agent.folder / "scripts/testcases.py").exists()


def test_execute_checks_revision_after_card_was_prepared(tmp_path):
    capabilities, tool = patch_service(tmp_path)
    arguments = patch_arguments(capabilities)
    tool.approval_payload(arguments)
    agent = capabilities.agents.get("test-cases")
    script = agent.folder / "scripts/testcases.py"
    script.write_text("print('правка пользователя')\n", encoding="utf-8")
    result = asyncio.run(tool.execute(arguments, None))
    assert result.is_error and "изменился" in result.content
    assert script.read_text(encoding="utf-8") == "print('правка пользователя')\n"
    assert not (capabilities.paths.agents / ".history").exists()


def test_catalog_revision_check_prevents_overwriting_an_edit_during_patch_execution(tmp_path, monkeypatch):
    capabilities, tool = patch_service(tmp_path)
    arguments = patch_arguments(capabilities)
    original = capabilities.save_source
    changed = "print('одновременная правка из редактора')\n"

    def concurrent_edit(kind, item_id, files, **kwargs):
        # Правка из другого запроса попала между подготовкой копии и CAS-записью.
        original(kind, item_id, {"scripts/testcases.py": changed})
        original(kind, item_id, files, **kwargs)

    monkeypatch.setattr(capabilities, "save_source", concurrent_edit)
    result = asyncio.run(tool.execute(arguments, None))
    assert result.is_error and "изменился" in result.content
    agent = capabilities.agents.get("test-cases")
    assert (agent.folder / "scripts/testcases.py").read_text(encoding="utf-8") == changed
    [backup] = list((capabilities.paths.agents / ".history/test-cases").iterdir())
    assert json.loads((backup / "manifest.json").read_text(encoding="utf-8"))["status"] == "prepared"


def test_archiving_during_source_staging_waits_for_save_and_does_not_resurrect_agent(tmp_path, monkeypatch):
    capabilities, _ = patch_service(tmp_path)
    arguments = patch_arguments(capabilities)
    agent = capabilities.agents.get("test-cases")
    staged, release, archive_waiting, archived = Event(), Event(), Event(), Event()
    original_lock = capabilities._source_lock

    class ObservedLock:
        def __enter__(self):
            if current_thread().name.startswith("agent-archive"):
                archive_waiting.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    monkeypatch.setattr(capabilities, "_source_lock", ObservedLock())
    copytree = agent_files.shutil.copytree

    def pause_staging(source, target, *args, **kwargs):
        result = copytree(source, target, *args, **kwargs)
        if source == agent.folder:
            staged.set()
            assert release.wait(timeout=2), "сохранение не продолжено"
        return result

    def archive():
        capabilities.archive("agent", agent.id)
        archived.set()

    monkeypatch.setattr(agent_files.shutil, "copytree", pause_staging)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-save") as saver,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-archive") as archiver,
    ):
        saving = saver.submit(capabilities.save_source, "agent", agent.id, arguments["files"],
                              expected_revision=arguments["expected_revision"])
        try:
            assert staged.wait(timeout=2), "сохранение не достигло staging"
            archiving = archiver.submit(archive)
            assert archive_waiting.wait(timeout=2), "архивирование обошло общий source lock"
            assert not archived.is_set() and agent.folder.is_dir()
        finally:
            release.set()
        saving.result(timeout=2)
        archiving.result(timeout=2)
    assert archived.is_set() and capabilities.agents.get(agent.id) is None and not agent.folder.exists()
    archive_id = capabilities.archived("agent", agent.id)
    assert archive_id
    archived_files = files_of(capabilities.paths.agents / ".deleted" / archive_id)
    assert archived_files["scripts/testcases.py"] == arguments["files"]["scripts/testcases.py"]


def test_patch_waits_for_approval_and_reject_does_not_change_agent(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = ScriptedProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        capabilities = client.app.state.capabilities
        arguments = patch_arguments(capabilities)
        before = agent_revision(capabilities.agents.get("test-cases"))
        provider.steps = [call("propose_agent_patch", **arguments), "Исправление отклонено"]
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Предложи исправление агента"}))
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "agent_patch"
        assert agent_revision(capabilities.agents.get("test-cases")) == before
        assert not (capabilities.paths.agents / ".history").exists()
        resumed = events_of(client.post(url + "/resume", json={"action": "reject"}))
        assert resumed[-1]["type"] == "done"
        [result] = [item["message"] for item in resumed if item["type"] == "tool_result"]
        assert result["is_error"] and "отклонил" in result["content"]
        assert agent_revision(capabilities.agents.get("test-cases")) == before
        assert not (capabilities.paths.agents / ".history").exists()


def test_patch_approval_survives_restart_and_applies_only_once(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    configured = settings(tmp_path)
    provider = ScriptedProvider()
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        capabilities = client.app.state.capabilities
        arguments = patch_arguments(capabilities)
        provider.steps = [call("propose_agent_patch", **arguments), "Исправление сохранено"]
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Предложи исправление агента"}))
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "agent_patch"
        assert not (capabilities.paths.agents / ".history").exists()
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        capabilities = client.app.state.capabilities
        assert client.get(url).json()["pending"]["kind"] == "agent_patch"
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))
        assert events[-1]["type"] == "done"
        [result] = [item["message"] for item in events if item["type"] == "tool_result"]
        assert not result["is_error"] and result["meta"]["agent_patch"]["agent"] == "test-cases"
        assert client.post(url + "/resume", json={"action": "approve"}).status_code == 409
        assert len(list((capabilities.paths.agents / ".history/test-cases").iterdir())) == 1
        assert agent_revision(capabilities.agents.get("test-cases")) != arguments["expected_revision"]


def test_read_only_project_does_not_offer_or_execute_agent_patch(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = ScriptedProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        capabilities = client.app.state.capabilities
        arguments = patch_arguments(capabilities)
        provider.steps = [call("propose_agent_patch", **arguments), "Нужен полный режим проекта"]
        session = project_session(client, workspace)
        client.put(f"/api/projects/{session['project_id']}", json={"name": "Проект", "workspace": str(workspace), "mode": "read"})
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Исправь агента"}))
        assert events[-1]["type"] == "done"
        [result] = [item["message"] for item in events if item["type"] == "tool_result"]
        assert result["is_error"] and "Инструмента «propose_agent_patch» нет" in result["content"]
        assert "propose_agent_patch" not in provider.tool_names(0)
        assert "- propose_agent_patch" not in provider.system_prompt(0)
        assert not (capabilities.paths.agents / ".history").exists()
