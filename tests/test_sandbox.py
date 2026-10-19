"""Песочница скриптов агента: таймаут, только чтение рабочей папки без подтверждения, нет сети, нет домашней папки."""

import asyncio
import os

import pytest

from connectors.sandbox import SandboxPolicy, bwrap_path, run_script

pytestmark = pytest.mark.skipif(bwrap_path() is None, reason="нужен bubblewrap")


def run(tmp_path, code, policy=SandboxPolicy(timeout=5), arguments=None):
    script = tmp_path / "tool" / "script.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(code, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return asyncio.run(run_script(script, arguments or {}, workspace, policy)), workspace


def test_arguments_in_stdin_result_in_stdout(tmp_path):
    result, _ = run(tmp_path, "import json,sys; print(json.load(sys.stdin)['x'] * 2)", arguments={"x": 21})
    assert result.content == "42" and not result.is_error


def test_read_only_tool_cannot_change_workspace(tmp_path):
    code = "open('created.txt', 'w').write('x')"
    denied, workspace = run(tmp_path, code)
    assert denied.is_error and not (workspace / "created.txt").exists()
    allowed, workspace = run(tmp_path, code, SandboxPolicy(timeout=5, writable_workspace=True))
    assert not allowed.is_error and (workspace / "created.txt").exists()


def test_home_and_network_are_hidden_and_timeout_stops_script(tmp_path):
    home = os.path.expanduser("~")
    listing, _ = run(tmp_path, f"import os; print(os.listdir({home!r}))")
    assert listing.is_error or ".ssh" not in listing.content
    network, _ = run(tmp_path, "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)")
    assert network.is_error
    slow, _ = run(tmp_path, "import time; time.sleep(30)", SandboxPolicy(timeout=1))
    assert slow.is_error and "не уложился" in slow.content
