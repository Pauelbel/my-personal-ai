"""Окружение песочницы: только имена пакетов PyPI, requires инструмента, скрипт видит пакеты окружения."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.capabilities.proposals import InstallPackagesTool
from local_agent.capabilities.pyenv import PythonEnv, check_requirements, requirement_name
from local_agent.capabilities.sandbox import bwrap_path
from local_agent.capabilities.scripts import read_script_tool, write_script_tool

USES_PACKAGE = "import hello_pkg\nprint(hello_pkg.GREETING)\n"


def test_only_pypi_names_are_accepted():
    assert requirement_name("pypdf") == "pypdf"
    assert requirement_name("Py_PDF.x>=4,<6") == "py-pdf-x"
    assert requirement_name("httpx[http2]==0.27") == "httpx"
    for bad in ("-e .", "--index-url=http://evil", "git+https://x/y", "./pkg", "/tmp/a.whl", "a; rm -rf /", ""):
        assert requirement_name(bad) is None, bad
    assert check_requirements(["pypdf", "--pre"]) is not None
    assert check_requirements([]) is not None
    assert check_requirements(["pypdf", "lxml>=5"]) is None


def test_requires_is_saved_and_missing_package_stops_the_call(tmp_path):
    env = PythonEnv(tmp_path / "env")
    tool = write_script_tool(
        tmp_path / "tools", "read_pdf", name="PDF", description="Читает PDF", reason="r", parameters={"type": "object"},
        code=USES_PACKAGE, requires_approval=False, network=False, requires=["hello-pkg"],
    )
    assert "requires: hello-pkg" in (tool.folder / "tool.md").read_text(encoding="utf-8")
    tool = read_script_tool(tool.folder)
    tool.env = env
    assert tool.requires == ("hello-pkg",)
    result = asyncio.run(tool.execute({}, None))
    assert result.is_error and "install_packages" in result.content and "hello-pkg" in result.content


def test_bad_requires_in_tool_md_is_rejected(tmp_path):
    tool = write_script_tool(
        tmp_path / "tools", "bad_req", name="x", description="x", reason="r", parameters={"type": "object"},
        code="print(1)", requires_approval=False, network=False,
    )
    text = (tool.folder / "tool.md").read_text(encoding="utf-8").replace("timeout:", "requires: git+https://x\ntimeout:")
    (tool.folder / "tool.md").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="requires"):
        read_script_tool(tool.folder)


@pytest.mark.skipif(bwrap_path() is None, reason="нужен bubblewrap")
def test_script_imports_package_from_sandbox_env(tmp_path):
    env = PythonEnv(tmp_path / "env")
    assert asyncio.run(env._create()) is None
    # Пакет «ставим» без сети: модуль и dist-info прямо в site-packages окружения.
    site = env._site_packages()[0]
    (site / "hello_pkg.py").write_text("GREETING = 'привет из песочницы'\n", encoding="utf-8")
    info = site / "hello_pkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: hello-pkg\nVersion: 1.0\n", encoding="utf-8")
    assert env.packages() == {"hello-pkg": "1.0"}

    tool = write_script_tool(
        tmp_path / "tools", "greet_pkg", name="x", description="x", reason="r", parameters={"type": "object"},
        code=USES_PACKAGE, requires_approval=False, network=False, requires=["hello-pkg"],
    )
    tool.env = env
    result = asyncio.run(tool.execute({}, None))
    assert not result.is_error and result.content == "привет из песочницы"


def test_install_tool_validates_before_the_card_and_overview_lists_packages(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        capabilities = client.app.state.capabilities
        tool = InstallPackagesTool(capabilities)
        assert tool.validate({"packages": ["https://evil/x.whl"], "reason": "r"}) is not None
        assert tool.approval_payload({"packages": ["pypdf"], "reason": "r"})["title"].endswith("pypdf")
        overview = client.get("/api/capabilities").json()
        assert overview["python"]["packages"] == []
        assert overview["python"]["path"].endswith("sandbox-env")
        assert client.delete("/api/capabilities/packages/pypdf").status_code == 400
