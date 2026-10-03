"""Сборка FastAPI-приложения подключает маршруты и общие HTTP-настройки."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from local_agent.agent.loader import load_agents, load_skills, warn_unknown_references
from local_agent.agent.registry import AgentRegistry, SkillRegistry
from local_agent.agent.runtime import AgentRuntime, RuntimeLimits
from local_agent.agent.summary import ConversationSummarizer
from local_agent.api.middleware import LocalOriginMiddleware, RequestLoggingMiddleware
from local_agent.api.routes.agents import router as agents_router
from local_agent.api.routes.files import router as files_router
from local_agent.api.routes.folders import router as folders_router
from local_agent.api.routes.health import router as health_router
from local_agent.api.routes.memory import router as memory_router
from local_agent.api.routes.messages import router as messages_router
from local_agent.api.routes.models import router as models_router
from local_agent.api.routes.projects import router as projects_router
from local_agent.api.routes.sessions import router as sessions_router
from local_agent.api.routes.skills import router as skills_router
from local_agent.api.routes.tools import router as tools_router
from local_agent.api.routes.turns import router as turns_router
from local_agent.config.logging import configure_logging
from local_agent.config.settings import Settings, get_settings
from local_agent.llm.base import LLMProvider
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.llm.registry import LLMRegistry
from local_agent.memory.conversation import ConversationService
from local_agent.memory.markdown import MarkdownMemoryStore
from local_agent.memory.service import MemoryService
from local_agent.projects.service import ProjectService
from local_agent.sessions.service import SessionService
from local_agent.storage.json.projects import JsonProjectRepository
from local_agent.storage.json.sessions import JsonSessionRepository
from local_agent.storage.jsonl.conversation import JsonlConversationStore
from local_agent.team.coordinator import TeamCoordinator
from local_agent.tools.filesystem import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from local_agent.tools.git import GitTool
from local_agent.tools.registry import ToolRegistry


def build_providers(settings: Settings, lm_studio: LLMProvider | None) -> LLMRegistry:
    providers: dict[str, LLMProvider] = {
        "lm_studio": lm_studio or OpenAICompatibleProvider(
            settings.lm_studio_base_url, settings.llm_timeout_seconds
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


def create_app(
    settings: Settings | None = None, llm_provider: LLMProvider | None = None
) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conversation_store = JsonlConversationStore(active_settings.conversations_path)
        conversation_store.initialize()
        session_repository = JsonSessionRepository(active_settings.sessions_path)
        session_repository.initialize()
        project_repository = JsonProjectRepository(active_settings.projects_path)
        project_repository.initialize()
        memory_store = MarkdownMemoryStore(active_settings.memory_path)

        app.state.session_service = SessionService(
            session_repository,
            projects=project_repository,
            on_deleted=(conversation_store.delete, memory_store.forget),
        )
        app.state.project_service = ProjectService(project_repository, app.state.session_service)
        app.state.project_service.adopt_sessions()
        app.state.conversation_service = ConversationService(
            conversation_store,
            on_saved=lambda message: app.state.session_service.touch(
                message.session_id, message.created_at
            ),
        )
        app.state.llm_registry = build_providers(active_settings, llm_provider)
        app.state.memory_service = MemoryService(
            memory_store,
            app.state.conversation_service,
            app.state.llm_registry,
            memory_model=active_settings.memory_model,
            max_context_chars=active_settings.max_memory_context_chars,
        )
        app.state.memory_service.initialize()
        app.state.tool_registry = ToolRegistry([
            ListFilesTool(), ReadFileTool(), SearchFilesTool(),
            WriteFileTool(active_settings.memory_path), EditFileTool(active_settings.memory_path),
            GitTool(),
        ])
        app.state.agent_registry = AgentRegistry(load_agents(active_settings.agents_path))
        app.state.skill_registry = SkillRegistry(load_skills(active_settings.skills_path))
        warn_unknown_references(
            app.state.agent_registry.all(),
            {tool.id for tool in app.state.tool_registry.all()},
            {skill.id for skill in app.state.skill_registry.all()},
        )
        app.state.agent_runtime = AgentRuntime(
            app.state.session_service,
            app.state.conversation_service,
            app.state.agent_registry,
            app.state.llm_registry,
            app.state.memory_service,
            app.state.tool_registry,
            app.state.skill_registry,
            ConversationSummarizer(
                app.state.session_service,
                app.state.conversation_service,
                app.state.llm_registry,
            ),
            RuntimeLimits(
                max_context_messages=active_settings.max_context_messages,
                default_context_tokens=active_settings.default_context_tokens,
                response_reserve_tokens=active_settings.response_reserve_tokens,
                memory_auto_update_messages=active_settings.memory_auto_update_messages,
            ),
        )
        app.state.team_coordinator = TeamCoordinator(
            app.state.agent_runtime,
            app.state.session_service,
            app.state.conversation_service,
            app.state.agent_registry,
            max_steps=active_settings.max_run_steps,
        )
        try:
            yield
        finally:
            await app.state.agent_runtime.close()
            await app.state.llm_registry.close()

    app = FastAPI(title="Meepo Agent", version="0.1.0", lifespan=lifespan)
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
    app.include_router(health_router, prefix="/api")
    app.include_router(sessions_router, prefix="/api")
    app.include_router(projects_router, prefix="/api")
    app.include_router(folders_router, prefix="/api")
    app.include_router(files_router, prefix="/api")
    app.include_router(messages_router, prefix="/api")
    app.include_router(memory_router, prefix="/api")
    app.include_router(models_router, prefix="/api")
    app.include_router(agents_router, prefix="/api")
    app.include_router(turns_router, prefix="/api")
    app.include_router(tools_router, prefix="/api")
    app.include_router(skills_router, prefix="/api")

    ui_dir = Path(__file__).resolve().parents[1] / "ui"
    app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    return app
