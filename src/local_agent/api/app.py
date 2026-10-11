"""Сборка FastAPI-приложения: сервисы, реестр возможностей, граф агента и маршруты."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from local_agent.agent.loader import load_agents, load_skills, warn_unknown_references
from local_agent.agent.main_agent import MAIN_AGENT_ID
from local_agent.agent.models import Agent
from local_agent.agent.registry import AgentRegistry, SkillRegistry
from local_agent.agent.runtime import AgentRuntime, RuntimeLimits
from local_agent.agent.summary import ConversationSummarizer
from local_agent.agent.turn_hub import TurnHub
from local_agent.api.middleware import LocalOriginMiddleware, RequestLoggingMiddleware
from local_agent.api.routes.attention import router as attention_router
from local_agent.api.routes.capabilities import router as capabilities_router
from local_agent.api.routes.events import router as events_router
from local_agent.api.routes.files import router as files_router
from local_agent.api.routes.folders import router as folders_router
from local_agent.api.routes.health import router as health_router
from local_agent.api.routes.memory import router as memory_router
from local_agent.api.routes.messages import router as messages_router
from local_agent.api.routes.models import router as models_router
from local_agent.api.routes.projects import router as projects_router
from local_agent.api.routes.sessions import router as sessions_router
from local_agent.api.routes.team import links_router
from local_agent.api.routes.team import router as team_router
from local_agent.api.routes.turns import router as turns_router
from local_agent.api.routes.wiki import router as wiki_router
from local_agent.capabilities.delegation import AskAgentTool, DelegateTool, DelegationManager
from local_agent.capabilities.extras import extra_factories
from local_agent.capabilities.mcp import MCPManager
from local_agent.capabilities.proposals import ArchiveTool, ProposeTool
from local_agent.capabilities.service import CapabilityPaths, CapabilityService
from local_agent.capabilities.toolbox import Toolbox
from local_agent.config.logging import configure_logging
from local_agent.config.settings import Settings, get_settings
from local_agent.events import EventBus
from local_agent.llm.base import LLMProvider
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.llm.registry import LLMRegistry
from local_agent.memory.conversation import ConversationService
from local_agent.memory.markdown import MarkdownMemoryStore
from local_agent.memory.service import MemoryService
from local_agent.memory.tools import MemorySearchTool, RememberTool
from local_agent.projects.service import ProjectService
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService
from local_agent.storage.json.projects import JsonProjectRepository
from local_agent.storage.json.sessions import JsonSessionRepository
from local_agent.storage.jsonl.conversation import JsonlConversationStore
from local_agent.tools.base import Tool
from local_agent.tools.docs import SearchDocsTool
from local_agent.tools.filesystem import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from local_agent.tools.git import GitTool
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.web import FetchUrlTool
from local_agent.wiki.service import WikiService
from local_agent.wiki.tools import WikiReadTool, WikiSearchTool, WikiWriteTool


def build_providers(settings: Settings, lm_studio: LLMProvider | None) -> LLMRegistry:
    providers: dict[str, LLMProvider] = {
        "lm_studio": lm_studio or OpenAICompatibleProvider(
            settings.llm_base_url, settings.llm_timeout_seconds, name=settings.llm_name
        ),
    }
    for config in settings.llm_providers:
        providers[config.id] = OpenAICompatibleProvider(
            config.base_url,
            settings.llm_timeout_seconds,
            name=config.name,
            api_key=config.api_key,
        )
    return LLMRegistry(providers)


class _CatalogTool:
    """Обёртка помечает инструмент как редкий: модель видит его в реестре и подключает через activate."""

    catalog = True

    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    def __getattr__(self, name: str):
        return getattr(self._tool, name)


def create_app(
    settings: Settings | None = None, llm_provider: LLMProvider | None = None
) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        data = active_settings.data_path
        index_path = data("index_path", "index")
        async with AsyncExitStack() as stack:
            state.events = EventBus()
            state.events.bind(asyncio.get_running_loop())
            conversation_store = JsonlConversationStore(active_settings.conversations_path)
            conversation_store.initialize()
            session_repository = JsonSessionRepository(active_settings.sessions_path)
            session_repository.initialize()
            project_repository = JsonProjectRepository(active_settings.projects_path)
            project_repository.initialize()
            session_repository.on_change = lambda session_id: state.events.publish({"type": "changed", "session_id": session_id})
            project_repository.on_change = lambda project_id: state.events.publish({"type": "projects", "project_id": project_id})
            memory_store = MarkdownMemoryStore(active_settings.memory_path)

            state.session_service = SessionService(
                session_repository,
                projects=project_repository,
                on_deleted=(conversation_store.delete, memory_store.forget),
            )
            state.project_service = ProjectService(project_repository, state.session_service)
            # «Черновики» нужны до миграции: в них переходят сессии без проекта.
            state.project_service.ensure_drafts(active_settings.drafts_path)
            state.project_service.adopt_sessions()
            state.conversation_service = ConversationService(
                conversation_store,
                on_saved=lambda message: state.session_service.touch(message.session_id, message.created_at),
            )
            state.llm_registry = build_providers(active_settings, llm_provider)
            stack.push_async_callback(state.llm_registry.close)
            state.memory_service = MemoryService(
                memory_store,
                state.conversation_service,
                state.llm_registry,
                memory_model=active_settings.memory_model,
                max_context_chars=active_settings.max_memory_context_chars,
                index_path=index_path / "memory.sqlite",
            )
            state.memory_service.initialize()
            state.wiki_service = WikiService(data("wiki_path", "wiki"), index_path / "wiki.sqlite")
            state.wiki_service.initialize()

            state.tool_registry = ToolRegistry([
                ListFilesTool(), ReadFileTool(), SearchFilesTool(),
                WriteFileTool(active_settings.memory_path), EditFileTool(active_settings.memory_path),
                GitTool(), SearchDocsTool(index_path / "docs"), FetchUrlTool(),
                WikiReadTool(state.wiki_service), WikiWriteTool(state.wiki_service),
            ])
            state.agent_registry = AgentRegistry(load_agents(active_settings.agents_path))
            state.skill_registry = SkillRegistry(load_skills(active_settings.skills_path))
            state.capabilities = CapabilityService(
                state.tool_registry, state.skill_registry, state.agent_registry,
                CapabilityPaths(data("tools_path", "tools"), active_settings.skills_path, active_settings.agents_path),
                extras=extra_factories(state.llm_registry),
            )
            state.capabilities.load()

            checkpoints = data("checkpoints_path", "checkpoints.sqlite")
            checkpoints.parent.mkdir(parents=True, exist_ok=True)
            checkpointer = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(checkpoints)))
            state.agent_runtime = AgentRuntime(
                state.session_service,
                state.conversation_service,
                state.agent_registry,
                state.llm_registry,
                state.memory_service,
                state.tool_registry,
                state.skill_registry,
                ConversationSummarizer(state.session_service, state.conversation_service, state.llm_registry),
                RuntimeLimits(
                    max_context_messages=active_settings.max_context_messages,
                    default_context_tokens=active_settings.default_context_tokens,
                    response_reserve_tokens=active_settings.response_reserve_tokens,
                    memory_auto_update_messages=active_settings.memory_auto_update_messages,
                ),
                checkpointer=checkpointer,
            )
            stack.push_async_callback(state.agent_runtime.close)
            state.turn_hub = TurnHub(state.agent_runtime, state.session_service, state.events.publish)
            stack.push_async_callback(state.turn_hub.close)
            state.delegation = DelegationManager(
                state.agent_runtime, state.session_service, state.conversation_service, state.agent_registry,
                max_parallel=active_settings.max_parallel_tasks,
                provider=active_settings.subagent_provider, model=active_settings.subagent_model,
            )
            stack.push_async_callback(state.delegation.close)

            def core_tools(session: Session, agent: Agent, box: Toolbox) -> list[Tool]:
                if agent.id != MAIN_AGENT_ID:
                    # Саб-агентам — вики, но не личная память пользователя.
                    tools: list[Tool] = [WikiSearchTool(state.wiki_service)]
                    if "wiki_read" not in agent.tools:
                        tools.append(WikiReadTool(state.wiki_service))
                    # Связи на холсте: кого этот саб-агент может спросить посреди задачи.
                    if targets := tuple(item for item in agent.agents if state.agent_registry.get(item)):
                        tools.append(AskAgentTool(state.delegation, state.agent_registry, session, targets))
                    return tools
                tools = [
                    RememberTool(state.memory_service, session.id),
                    ProposeTool(state.capabilities),
                    _CatalogTool(MemorySearchTool(state.memory_service)),
                    _CatalogTool(WikiSearchTool(state.wiki_service)),
                    _CatalogTool(ArchiveTool(state.capabilities)),
                ]
                if state.agent_registry.all():
                    tools.append(DelegateTool(state.delegation, state.agent_registry, session))
                return tools

            state.agent_runtime.core_tools.append(core_tools)
            state.agent_runtime.archive_lines = state.capabilities.archive_lines
            warn_unknown_references(
                state.agent_registry.all(),
                {tool.id for tool in state.tool_registry.all()},
                {skill.id for skill in state.skill_registry.all()},
            )
            state.mcp = MCPManager(
                data("mcp_config_path", "mcp.json"), state.tool_registry, data("logs_path", "logs"),
            )
            if active_settings.mcp_enabled:
                state.mcp.start()
            stack.push_async_callback(state.mcp.close)
            yield

    app = FastAPI(title="Meepo Agent", version="0.2.0", lifespan=lifespan)
    app.state.settings = active_settings

    @app.middleware("http")
    async def refresh_ui_assets(request: Request, call_next):
        response = await call_next(request)
        # HTML и модули должны обновляться вместе, иначе браузер смешивает версии интерфейса.
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Starlette оборачивает последним добавленный middleware снаружи, поэтому отказы тоже попадут в лог.
    app.add_middleware(LocalOriginMiddleware, allowed_hosts=active_settings.allowed_host_set)
    app.add_middleware(RequestLoggingMiddleware)
    for router in (
        health_router, sessions_router, projects_router, folders_router, files_router, messages_router,
        memory_router, models_router, turns_router, capabilities_router, attention_router, wiki_router, team_router,
        events_router, links_router,
    ):
        app.include_router(router, prefix="/api")

    ui_dir = Path(__file__).resolve().parents[1] / "ui"
    app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    return app
