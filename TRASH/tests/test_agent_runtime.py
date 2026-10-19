"""Встроенный launcher работает в реальной песочнице без пакетов приложения и вызовов LLM."""

import asyncio
import venv

import pytest

from agents.files import LIBRARY, initialize_agents
from agents.runtime import TESTCASES_LAUNCHER, prepared_agent_script
from catalog.service import CapabilityPaths, CapabilityService
from connectors.pyenv import PythonEnv
from connectors.sandbox import SandboxPolicy, bwrap_path, run_script
from core.registry import ToolRegistry


@pytest.mark.parametrize("install", ["bootstrap", "catalog"])
@pytest.mark.parametrize("interpreter", ["application", "sandbox-env"])
@pytest.mark.skipif(bwrap_path() is None, reason="нужен bubblewrap")
def test_builtin_testcases_runs_in_sandbox_without_application_imports(tmp_path, install, interpreter):
    root = tmp_path / "agents"
    if install == "bootstrap":
        initialize_agents(root)
    else:
        service = CapabilityService(ToolRegistry(), CapabilityPaths(tmp_path / "tools", root))
        service.enable("agent", "test-cases")
    script = root / "test-cases" / "scripts" / "testcases.py"
    python = None
    if interpreter == "sandbox-env":
        env = PythonEnv(tmp_path / "sandbox-env")
        venv.EnvBuilder(with_pip=False).create(env.path)
        python = env.python
    workspace = tmp_path / "project"
    (workspace / "docs").mkdir(parents=True)

    with prepared_agent_script(script) as executable:
        assert executable != script
        result = asyncio.run(run_script(
            executable, {"mode": "generate", "path": "docs"}, workspace,
            SandboxPolicy(timeout=10, network=False), python,
            env={"MEEPO_LLM_URL": "http://127.0.0.1:1", "MEEPO_LLM_MODEL": "unused"},
        ))

    assert not result.is_error, result.content
    assert "Страниц: 0" in result.content
    assert "Новых страниц для генерации нет" in result.content
    assert not executable.exists()
    assert script.read_text(encoding="utf-8") == TESTCASES_LAUNCHER
    assert list(workspace.iterdir()) == [workspace / "docs"]


def test_prepared_builtin_script_is_removed_after_failure(tmp_path):
    script = tmp_path / "testcases.py"
    script.write_text(TESTCASES_LAUNCHER, encoding="utf-8")

    with pytest.raises(RuntimeError, match="failed"):
        with prepared_agent_script(script) as executable:
            assert executable.exists()
            raise RuntimeError("failed")

    assert not executable.exists()
    assert script.exists()


@pytest.mark.parametrize("source", [TESTCASES_LAUNCHER + "\n# Пользовательская доработка\n", "print('custom')\n"])
def test_user_scripts_run_without_substitution(tmp_path, source):
    script = tmp_path / "testcases.py"
    script.write_text(source, encoding="utf-8")

    with prepared_agent_script(script) as executable:
        assert executable == script

    assert script.read_text(encoding="utf-8") == source


def test_packaged_launcher_matches_the_recognized_builtin():
    script = LIBRARY / "test-cases" / "scripts" / "testcases.py"
    assert script.read_text(encoding="utf-8") == TESTCASES_LAUNCHER
