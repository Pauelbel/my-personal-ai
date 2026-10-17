"""HTTP-приложение: маршруты, middleware и статика интерфейса; всё остальное собирает assembly.py."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from local_agent.api.events import EventBus
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
from local_agent.api.routes.providers import router as providers_router
from local_agent.api.routes.sessions import router as sessions_router
from local_agent.api.routes.team import links_router
from local_agent.api.routes.team import router as team_router
from local_agent.api.routes.turns import router as turns_router
from local_agent.api.routes.wiki import router as wiki_router
from local_agent.assembly import assemble
from local_agent.config.logging import configure_logging
from local_agent.config.settings import Settings, get_settings
from local_agent.llm.base import LLMProvider


def create_app(
    settings: Settings | None = None, llm_provider: LLMProvider | None = None
) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        async with AsyncExitStack() as stack:
            state.events = EventBus()
            state.events.bind(asyncio.get_running_loop())
            await assemble(state, active_settings, stack, llm_provider)
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
