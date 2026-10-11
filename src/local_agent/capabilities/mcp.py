"""Внешние инструменты через MCP: браузер (Playwright MCP) и веб-поиск (DuckDuckGo MCP).

Серверы описаны в data/mcp.json — его можно править руками. Каждый сервер живёт в своей фоновой
задаче: stdio-клиент MCP нужно открывать и закрывать в одной задаче. Инструменты сервера попадают
в общий реестр под id «<сервер>_<инструмент>» — так же, как встроенные и созданные агентом.

Подтверждение: без него выполняются только инструменты из auto_approve или с подсказкой read_only_hint.
Если сервер не запустился (например, нет Node.js для Playwright), его инструментов просто нет
в реестре, а причина видна в списке возможностей — агент скажет, что такой возможности нет.
"""

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from local_agent.tools.base import ToolResult
from local_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

START_TIMEOUT_SECONDS = 90
CALL_TIMEOUT_SECONDS = 120
MAX_RESULT_CHARS = 15_000

DEFAULT_CONFIG = {
    "servers": {
        "search": {
            "description": "Веб-поиск DuckDuckGo и чтение страниц",
            "command": "uvx",
            "args": ["duckduckgo-mcp-server"],
            "auto_approve": ["search", "fetch_content", "expand_link"],
            "enabled": True,
        },
        "browser": {
            "description": "Браузер Playwright: открыть страницу, кликнуть, заполнить, извлечь данные",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest", "--headless", "--isolated"],
            "auto_approve": [
                "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot",
                "browser_wait_for", "browser_tabs", "browser_console_messages", "browser_network_requests",
            ],
            "enabled": True,
        },
    }
}


@dataclass
class ServerState:
    name: str
    description: str = ""
    status: str = "выключен"
    tools: list[str] = field(default_factory=list)


class MCPTool:
    def __init__(self, server: str, session, tool, auto_approve: set[str]) -> None:
        self.server = server
        self.remote_name = tool.name
        self.id = sanitize(f"{server}_{tool.name}")
        self.name = tool.title or tool.name
        self.description = " ".join((tool.description or tool.name).split())
        schema = tool.input_schema or {}
        self.parameters = schema if schema.get("type") == "object" else {"type": "object", "properties": {}}
        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
        self.requires_approval = not (read_only or tool.name in auto_approve)
        self._session = session

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        try:
            result = await asyncio.wait_for(self._session.call_tool(self.remote_name, arguments), CALL_TIMEOUT_SECONDS)
        except TimeoutError:
            return ToolResult(f"Сервер {self.server} не ответил за {CALL_TIMEOUT_SECONDS} с", is_error=True)
        except Exception as exc:  # noqa: BLE001 — сбой внешнего сервера становится ошибкой вызова, а не хода
            return ToolResult(f"Сервер {self.server} вернул ошибку: {exc}", is_error=True)
        parts = []
        for item in getattr(result, "content", None) or []:
            if getattr(item, "type", "") == "text":
                parts.append(item.text)
            else:
                parts.append(f"[{getattr(item, 'type', 'данные')}: не текст, пропущено]")
        text = "\n".join(parts).strip() or "Пустой ответ"
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n… (обрезано)"
        return ToolResult(text, is_error=bool(getattr(result, "is_error", False)))


class MCPManager:
    def __init__(self, config_path: Path, registry: ToolRegistry, logs_path: Path) -> None:
        self._config_path = config_path
        self._registry = registry
        self._logs_path = logs_path
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.servers: dict[str, ServerState] = {}

    def config(self) -> dict:
        if not self._config_path.exists():
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Не удалось прочитать %s: %s", self._config_path, exc)
            return {"servers": {}}
        return data if isinstance(data.get("servers"), dict) else {"servers": {}}

    def start(self) -> None:
        for name, settings in self.config()["servers"].items():
            state = ServerState(sanitize(name), str(settings.get("description", "")))
            self.servers[state.name] = state
            if not settings.get("enabled", True):
                continue
            command = settings.get("command")
            if not isinstance(command, str) or shutil.which(command) is None:
                state.status = f"не запущен: команды {command!r} нет в системе" + (
                    " — установите Node.js" if command in {"npx", "node"} else "")
                continue
            state.status = "запускается"
            self._tasks.append(asyncio.create_task(self._serve(state, settings), name=f"mcp:{state.name}"))

    async def close(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _serve(self, state: ServerState, settings: dict) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._logs_path.mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(
            command=settings["command"], args=[str(item) for item in settings.get("args", [])],
            env=settings.get("env") or None,
        )
        auto_approve = set(settings.get("auto_approve", []))
        try:
            with (self._logs_path / f"mcp-{state.name}.log").open("a", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), START_TIMEOUT_SECONDS)
                        listed = await asyncio.wait_for(session.list_tools(), START_TIMEOUT_SECONDS)
                        tools = [MCPTool(state.name, session, tool, auto_approve) for tool in listed.tools]
                        for tool in tools:
                            if self._registry.get(tool.id) is None:
                                self._registry.register(tool)
                        state.tools = [tool.id for tool in tools]
                        state.status = "работает"
                        logger.info("MCP-сервер %s подключён: %s", state.name, ", ".join(state.tools))
                        await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — сервер мог упасть как угодно; приложение работает дальше
            state.status = f"ошибка: {_describe(exc)}"
            logger.warning("MCP-сервер %s не работает: %s", state.name, state.status)
        finally:
            for tool_id in state.tools:
                self._registry.unregister(tool_id)
            state.tools = []
            if state.status in {"работает", "запускается"}:
                state.status = "остановлен"


def sanitize(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")[:64]


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _describe(exc.exceptions[0])
    return f"{type(exc).__name__}: {exc}"[:300]
