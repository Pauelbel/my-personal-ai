"""Точка входа предоставляет ASGI-приложение для запуска сервером."""

import uvicorn

from api.app import create_app
from config.settings import get_settings

app = create_app()


def run() -> None:
    # Уровень логов uvicorn берётся из LOG_LEVEL, чтобы и его стартовые сообщения подчинялись .env.
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level=get_settings().log_level.lower())


if __name__ == "__main__":
    run()
