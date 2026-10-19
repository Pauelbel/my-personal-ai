"""Сборка FastAPI: сервисы, роуты и статика интерфейса."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from meepo.settings import Env

UI_DIR = Path(__file__).resolve().parent / "ui"


def create_app(env: Env | None = None) -> FastAPI:
    env = env or Env.load()
    app = FastAPI(title="Meepo", version="2.0.0")
    app.state.env = env

    @app.middleware("http")
    async def no_cache_ui(request: Request, call_next):
        response = await call_next(request)
        # HTML и модули должны обновляться вместе, иначе браузер смешивает версии интерфейса.
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    return app
