"""Проверка health защищает базовый контракт запуска HTTP-приложения."""

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_health(tmp_path) -> None:
    client = TestClient(create_app(Settings(
        database_path=tmp_path / "agent.sqlite3",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        tool_settings_path=tmp_path / "settings" / "tools.json",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]
