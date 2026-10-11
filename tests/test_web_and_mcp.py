"""Чтение страниц по ссылке и подключение MCP-серверов."""

import asyncio

import pytest

from local_agent.capabilities.mcp import MCPManager, sanitize
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.web import check_public_url, html_to_text


def test_html_is_reduced_to_text():
    title, text = html_to_text(
        "<html><head><title>Статья</title><script>x()</script></head>"
        "<body><nav>меню</nav><h1>Заголовок</h1><p>Первый абзац.</p><ul><li>пункт</li></ul></body></html>"
    )
    assert title == "Статья"
    assert text == "# Заголовок\nПервый абзац.\n- пункт"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://localhost/", "file:///etc/passwd", "ftp://x.org"])
def test_local_and_non_http_urls_are_refused(url):
    with pytest.raises(ValueError):
        check_public_url(url)


def test_missing_mcp_command_is_reported_not_fatal(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text('{"servers": {"Browser!": {"command": "no-such-binary-xyz", "args": []}}}', encoding="utf-8")
    registry = ToolRegistry()

    async def start():
        manager = MCPManager(config, registry, tmp_path / "logs")
        manager.start()
        await manager.close()
        return manager

    manager = asyncio.run(start())
    assert "не запущен" in manager.servers["browser"].status and registry.all() == []
    assert sanitize("Search Server/fetch-content") == "search_server_fetch_content"


def test_default_config_is_written_once(tmp_path):
    manager = MCPManager(tmp_path / "mcp.json", ToolRegistry(), tmp_path / "logs")
    assert set(manager.config()["servers"]) == {"search", "browser"}
    (tmp_path / "mcp.json").write_text('{"servers": {}}', encoding="utf-8")
    assert manager.config()["servers"] == {}
