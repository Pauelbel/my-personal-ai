"""Сборка FastAPI-приложения: сервисы, реестр возможностей, граф агента и маршруты."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from local_agent.agent.loader import load_agents, load_skills, warn_unknown_references
from local_agent.agent.main_agent import MAIN_AGENT_ID, MainInstructions
from local_agent.agent.models import Agent
from local_agent.agent.plan import PlanTool
from local_agent.agent.registry import AgentRegistry, SkillRegistry
from local_agent.agent.runtime import AgentRuntime, RuntimeLimits
from local_agent.agent.summary import ConversationSummarizer
from local_agent.agent.team import reachable
from local_agent.agent.turn_hub import TurnHub
from local_agent.api.middleware import LocalOriginMiddleware, RequestLoggingMiddleware
from local_agent.api.routes.agents import router as agents_router
from local_agent.api.routes.attachments import router as attachments_router
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
from local_agent.api.routes.providers import apply_default
from local_agent.api.routes.providers import build as build_stored_provider
from local_agent.api.routes.providers import router as providers_router
from local_agent.api.routes.sessions import router as sessions_router
from local_agent.api.routes.team import links_router
from local_agent.api.routes.team import router as team_router
from local_agent.api.routes.turns import router as turns_router
from local_agent.api.routes.wiki import router as wiki_router
from local_agent.capabilities.delegation import AskAgentTool, DelegateTool, DelegationManager
from local_agent.capabilities.extras import extra_factories
from local_agent.capabilities.lessons import LearnTool
from local_agent.capabilities.mcp import MCPManager
from local_agent.capabilities.proposals import ArchiveTool, InstallPackagesTool, ProposeTool
from local_agent.capabilities.pyenv import PythonEnv
from local_agent.capabilities.requests import AskUserTool, RequestToolsTool
from local_agent.capabilities.routing import ChooseRouteTool, HandoffTool
from local_agent.capabilities.secrets import SecretStore
from local_agent.capabilities.service import CapabilityPaths, CapabilityService
from local_agent.capabilities.toolbox import Toolbox
from local_agent.config.logging import configure_logging
from local_agent.config.settings import Settings, get_settings
from local_agent.events import EventBus
from local_agent.llm.base import LLMProvider
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.llm.registry import LLMRegistry
from local_agent.llm.store import ProviderStore
from local_agent.memory.attachments import AttachmentStore
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
from local_agent.wiki.tools import (
    WikiLintTool,
    WikiMatchTool,
    WikiProposeEditsTool,
    WikiReadTool,
    WikiSaveNoteTool,
    WikiSearchTool,
)


def build_providers(settings: Settings, lm_studio: LLMProvider | None) -> LLMRegistry:
    providers: dict[str, LLMProvider] = {
        "lm_studio": lm_studio or OpenAICompatibleProvider(
            settings.llm_base_url, settings.llm_timeout_seconds, name=settings.llm_name,
            verify_ssl=settings.llm_verify_ssl,
        ),
    }
    for config in settings.llm_providers:
        providers[config.id] = OpenAICompatibleProvider(
            config.base_url,
            settings.llm_timeout_seconds,
            name=config.name,
            api_key=config.api_key,
            verify_ssl=settings.llm_verify_ssl,
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
            state.attachments = AttachmentStore(data("attachments_path", "attachments"))

            def forget_attachments(session_id: str) -> None:
                # Картинки сессии уходят вместе с её историей; в заметки вики попали их копии.
                state.attachments.delete(name for message in conversation_store.list(session_id) for name in message.attachments)

            state.session_service = SessionService(
                session_repository,
                projects=project_repository,
                on_deleted=(forget_attachments, conversation_store.delete, memory_store.forget),
            )
            state.project_service = ProjectService(project_repository, state.session_service)
            state.project_service.adopt_sessions()
            state.conversation_service = ConversationService(
                conversation_store,
                on_saved=lambda message: state.session_service.touch(message.session_id, message.created_at),
            )
            state.llm_registry = build_providers(active_settings, llm_provider)
            stack.push_async_callback(state.llm_registry.close)
            # Провайдеры, добавленные в настройках интерфейса, — поверх заданных в .env.
            state.provider_store = ProviderStore(active_settings.data_dir / "providers.json")
            state.default_provider = "lm_studio"
            stored = state.provider_store.load()
            for entry in stored.providers:
                state.llm_registry.set(
                    entry.id,
                    build_stored_provider(
                        entry, active_settings.llm_timeout_seconds, verify_ssl=active_settings.llm_verify_ssl
                    ),
                )
            apply_default(state, stored.default_provider, stored.default_model)
            state.memory_service = MemoryService(
                memory_store,
                state.conversation_service,
                state.llm_registry,
                memory_model=active_settings.memory_model,
                max_context_chars=active_settings.max_memory_context_chars,
                index_path=index_path / "memory.sqlite",
            )
            state.memory_service.initialize()
            state.wiki_service = WikiService(
                data("wiki_path", "wiki"), index_path / "wiki.sqlite", attachments=state.attachments.path,
            )
            state.wiki_service.initialize()

            state.secrets = SecretStore(data("secrets_path", "secrets.json"))
            state.tool_registry = ToolRegistry([
                ListFilesTool(), ReadFileTool(), SearchFilesTool(),
                WriteFileTool(active_settings.memory_path), EditFileTool(active_settings.memory_path),
                GitTool(), SearchDocsTool(index_path / "docs"), FetchUrlTool(),
                WikiReadTool(state.wiki_service),
            ])
            state.agent_registry = AgentRegistry(load_agents(active_settings.agents_path))
            state.skill_registry = SkillRegistry(load_skills(active_settings.skills_path))
            state.capabilities = CapabilityService(
                state.tool_registry, state.skill_registry, state.agent_registry,
                CapabilityPaths(data("tools_path", "tools"), active_settings.skills_path, active_settings.agents_path),
                extras=extra_factories(state.llm_registry),
                env=PythonEnv(data("sandbox_env_path", "sandbox-env")), secrets=state.secrets,
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
                    wiki_context_messages=active_settings.wiki_context_messages,
                    max_tool_rounds=active_settings.max_tool_rounds,
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
                turns=state.turn_hub,
            )
            stack.push_async_callback(state.delegation.close)

            def core_tools(session: Session, agent: Agent, box: Toolbox) -> list[Tool]:
                if session.kind == "wiki":
                    # Чат вики без проекта: инструменты вики и чтение ссылок, все сразу со схемой.
                    return [
                        WikiReadTool(state.wiki_service), WikiSearchTool(state.wiki_service),
                        WikiMatchTool(state.wiki_service), WikiSaveNoteTool(state.wiki_service),
                        WikiLintTool(state.wiki_service), WikiProposeEditsTool(state.wiki_service),
                        FetchUrlTool(),
                    ]
                if agent.id != MAIN_AGENT_ID:
                    # Саб-агентам — вики, но не личная память пользователя.
                    tools: list[Tool] = [WikiSearchTool(state.wiki_service)]
                    if "wiki_read" not in agent.tools:
                        tools.append(WikiReadTool(state.wiki_service))
                    # Команда: саб-агент может обратиться к любому агенту (или к тем, кем ограничен в файле).
                    # В личном чате вне проектов обращаться некуда: разговоры агентов живут в сессиях проекта.
                    if session.project_id and (targets := reachable(agent, state.agent_registry)):
                        tools.append(AskAgentTool(state.delegation, state.agent_registry, session, targets))
                    if session.project_id:
                        # Не получается или не хватает данных — спросить пользователя; не хватает инструмента — попросить.
                        tools += [AskUserTool(agent.name), RequestToolsTool(state.capabilities, state.tool_registry, agent)]
                    # Задачу не по профилю саб-агент не делает сам, а передаёт основному агенту.
                    tools.append(HandoffTool(state.delegation, session))
                    tools.append(PlanTool(state.session_service, session.id))
                    # «Запомни, делай так впредь» — агент дописывает правило в свой промпт после подтверждения.
                    if state.agent_registry.get(agent.id) is not None:
                        tools.append(LearnTool(state.capabilities, agent))
                    return tools
                tools = [
                    RememberTool(state.memory_service, session.id),
                    PlanTool(state.session_service, session.id),
                    ProposeTool(state.capabilities),
                    _CatalogTool(MemorySearchTool(state.memory_service)),
                    _CatalogTool(WikiSearchTool(state.wiki_service)),
                    # Шаги добавления заметки и список правок lint всегда со схемой: без неё модель пишет
                    # правки текстом вместо карточки. Сам lint без параметров — его хватает строки в реестре.
                    WikiMatchTool(state.wiki_service),
                    WikiSaveNoteTool(state.wiki_service),
                    _CatalogTool(WikiLintTool(state.wiki_service)),
                    WikiProposeEditsTool(state.wiki_service),
                    _CatalogTool(ArchiveTool(state.capabilities)),
                    _CatalogTool(InstallPackagesTool(state.capabilities)),
                ]
                # Поручение идёт в разговор агента внутри сессии проекта: в общем чате вне проектов поручать некуда.
                if state.agent_registry.all() and session.project_id:
                    tools += [
                        DelegateTool(state.delegation, state.agent_registry, session),
                        # Кто сделает — сам или саб-агент — решает пользователь, выбор запоминается в проекте.
                        ChooseRouteTool(state.project_service, state.agent_registry, session),
                    ]
                return tools

            state.agent_runtime.core_tools.append(core_tools)
            state.agent_runtime.archive_lines = state.capabilities.archive_lines
            state.agent_runtime.secret_names = state.secrets.names
            state.main_instructions = MainInstructions(active_settings.data_dir / "main-instructions.md")
            state.agent_runtime.main_instructions = state.main_instructions.read
            state.agent_runtime.attachment_path = state.attachments.path
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
        health_router, sessions_router, projects_router, folders_router, files_router, messages_router, attachments_router,
        memory_router, models_router, turns_router, capabilities_router, attention_router, wiki_router, team_router,
        events_router, links_router, agents_router, providers_router,
    ):
        app.include_router(router, prefix="/api")

    ui_dir = Path(__file__).resolve().parents[1] / "ui"
    app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    return app
