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


def test_mcp_server_sees_display_but_not_secrets(monkeypatch):
    from local_agent.capabilities.mcp import server_environment

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("SECRET_API_KEY", "x")
    env = server_environment({"DEBUG": 1})
    assert env["DISPLAY"] == ":0" and env["WAYLAND_DISPLAY"] == "wayland-0" and env["DEBUG"] == "1"
    assert "SECRET_API_KEY" not in env and "PATH" in env


def test_mcp_element_ref_copied_from_snapshot_is_unwrapped():
    from types import SimpleNamespace

    from local_agent.capabilities.mcp import MCPTool

    sent = []

    class Session:
        async def call_tool(self, name, arguments):
            sent.append(arguments)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], is_error=False)

    remote = SimpleNamespace(name="browser_type", title=None, description="Type", input_schema={}, annotations=None)
    tool = MCPTool("browser", Session(), remote, set())
    for target in ("[ref=e41]", "ref=e41", "e41", "input[name='q']"):
        asyncio.run(tool.execute({"target": target, "text": "пирог"}, None))
    assert [item["target"] for item in sent] == ["e41", "e41", "e41", "input[name='q']"]
    assert all(item["text"] == "пирог" for item in sent)


def test_mcp_tool_description_is_translated_from_config():
    from types import SimpleNamespace

    from local_agent.capabilities.mcp import BROWSER_DESCRIPTIONS, MCPTool

    def tool(name, description):
        return SimpleNamespace(name=name, title=None, description=description, input_schema={}, annotations=None)

    translated = MCPTool("browser", None, tool("browser_click", "Perform click on a web page"), set(), BROWSER_DESCRIPTIONS)
    original = MCPTool("browser", None, tool("browser_new_thing", "Brand new  tool"), set(), BROWSER_DESCRIPTIONS)
    assert translated.description == BROWSER_DESCRIPTIONS["browser_click"]
    # Инструмента нет в переводах — остаётся описание сервера.
    assert original.description == "Brand new tool"


def test_open_browser_page_is_reminded_on_next_turn():
    from datetime import UTC, datetime

    from local_agent.agent.context import browser_state, build_system_prompt
    from local_agent.memory.models import Message

    def tool(name, content):
        return Message(id=name, session_id="s", role="tool", content=content, created_at=datetime.now(UTC), tool_name=name)

    snapshot = "### Page\n- Page URL: https://www.aviasales.ru/search\n- Page Title: Краснодар — Пекин\n### Snapshot\n…"
    history = [tool("browser_browser_navigate", "### Page\n- Page URL: https://www.aviasales.ru/"),
               tool("browser_browser_snapshot", snapshot), tool("search_search", "- Page URL: https://other.example/")]
    state = browser_state(history)
    assert "https://www.aviasales.ru/search — Краснодар — Пекин" in state and "browser_browser_snapshot" in state
    assert state in build_system_prompt("Правила", browser=state)
    # Браузер закрыт или ещё не открывался — напоминать не о чем.
    assert browser_state([*history, tool("browser_browser_close", "closed")]) == ""
    assert browser_state([tool("search_search", "ничего")]) == ""


def test_pointer_actions_pause_only_in_visible_browser(monkeypatch):
    from types import SimpleNamespace

    from local_agent.capabilities import mcp
    from local_agent.capabilities.mcp import POINTER_PAUSE_SECONDS, MCPTool

    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(mcp.asyncio, "sleep", sleep)

    class Session:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], is_error=name == "browser_click")

    def tool(name, visible):
        remote = SimpleNamespace(name=name, title=None, description=name, input_schema={}, annotations=None)
        return MCPTool("browser", Session(), remote, set(), visible=visible)

    # Наведение в видимом окне ждёт, пока человек увидит курсор; снимок, окно без экрана и неудачный клик — нет.
    for name, visible in (("browser_hover", True), ("browser_snapshot", True), ("browser_hover", False),
                          ("browser_click", True)):
        asyncio.run(tool(name, visible).execute({"target": "e1"}, None))
    assert pauses == [POINTER_PAUSE_SECONDS]


def _browser_tool(name, text, **options):
    from types import SimpleNamespace

    from local_agent.capabilities.mcp import MCPTool

    class Session:
        async def call_tool(self, remote, arguments):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)

    remote = SimpleNamespace(name=name, title=None, description=name, input_schema={}, annotations=None)
    return MCPTool("browser", Session(), remote, set(), **options)


def test_snapshot_file_is_inlined_and_long_result_says_it_is_cut(tmp_path):
    from local_agent.capabilities.mcp import MAX_RESULT_CHARS

    snapshots = tmp_path / ".playwright-mcp"
    snapshots.mkdir()
    (snapshots / "page-1.yml").write_text('- link "Travel" [ref=e23]\n', encoding="utf-8")
    (tmp_path.parent / "secret.yml").write_text("секрет", encoding="utf-8")
    reply = "### Page\n- Page URL: https://books.toscrape.com/\n### Snapshot\n- [Snapshot](.playwright-mcp/page-1.yml)"

    # Модель видит сам снимок, а не ссылку на файл, который ей не прочитать; файл после этого не нужен.
    text = asyncio.run(_browser_tool("browser_click", reply, workdir=tmp_path).execute({}, None)).content
    assert '### Snapshot\n```yaml\n- link "Travel" [ref=e23]\n```' in text and not (snapshots / "page-1.yml").exists()
    # Ссылка за пределы рабочей папки сервера остаётся ссылкой.
    outside = reply.replace(".playwright-mcp/page-1.yml", "../secret.yml")
    assert asyncio.run(_browser_tool("browser_click", outside, workdir=tmp_path).execute({}, None)).content == outside

    cut = asyncio.run(_browser_tool("browser_snapshot", "x" * (MAX_RESULT_CHARS + 500)).execute({}, None)).content
    assert f"показано {MAX_RESULT_CHARS} из {MAX_RESULT_CHARS + 500} символов" in cut and "browser_browser_evaluate" in cut


def test_browser_kit_is_bound_together_without_taking_slots():
    from local_agent.capabilities.toolbox import MAX_ACTIVE_TOOLS, Toolbox

    browser = [_browser_tool(name, "ok") for name in ("browser_navigate", "browser_hover", "browser_wait_for", "browser_tabs")]
    others = [SimpleTool(f"tool_{number}") for number in range(MAX_ACTIVE_TOOLS)]
    box = Toolbox(core=[], catalog=[*browser, *others], active=[])
    box.activate(["browser_browser_navigate"])
    bound = {tool.id for tool in box.bound()}
    # Подключили переход — под рукой и наведение с ожиданием; вкладки в набор не входят.
    assert {"browser_browser_navigate", "browser_browser_hover", "browser_browser_wait_for"} <= bound
    assert "browser_browser_tabs" not in bound and box.is_active("browser_browser_hover")
    box.activate([tool.id for tool in others[:MAX_ACTIVE_TOOLS - 1]])
    assert box.is_active("browser_browser_hover") and box.active[0] == "browser_browser_navigate"


class SimpleTool:
    def __init__(self, tool_id):
        self.id = tool_id
        self.description = tool_id
        self.parameters = {"type": "object", "properties": {}}
        self.requires_approval = False


def test_only_latest_page_snapshot_stays_in_turn_context():
    from local_agent.agent.context import drop_stale_snapshots
    from local_agent.llm.models import ChatMessage

    def snapshot(url, body):
        return f"### Ran Playwright code\nawait page.goto('{url}');\n### Page\n- Page URL: {url}\n### Snapshot\n```yaml\n{body}\n```"

    messages = [
        ChatMessage("user", "Собери цены"),
        ChatMessage("tool", snapshot("https://a.example/", "- list: 11 книг"), tool_call_id="1"),
        ChatMessage("tool", '[{"title": "A Year in Provence", "price": 56.88}]', tool_call_id="2"),
        ChatMessage("tool", snapshot("https://a.example/page-2", "- list: ещё книги"), tool_call_id="3"),
    ]
    result = drop_stale_snapshots(messages)
    # От старого снимка остались действие и адрес, собранные данные и последний снимок — целиком.
    assert "11 книг" not in result[1].content and "page.goto('https://a.example/')" in result[1].content
    assert "Page URL: https://a.example/\n### Snapshot\n(снимок устарел" in result[1].content
    assert result[2:] == messages[2:] and messages[1].content.endswith("```")
