"""Точка входа предоставляет ASGI-приложение для запуска сервером."""

from local_agent.api.app import create_app

app = create_app()
