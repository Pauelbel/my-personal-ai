"""Проверка истории подтверждает сохранение сообщений после перезапуска приложения."""

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_messages_persist_after_app_restart(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.sqlite3", _env_file=None)

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        created = client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "  Привет, агент!  "},
        )
        assert created.status_code == 201
        message = created.json()
        assert message["role"] == "user"
        assert message["content"] == "Привет, агент!"
        assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "Привет, агент!"
        assert client.post(
            f"/api/sessions/{session['id']}/messages", json={"content": "   "}
        ).status_code == 422

    with TestClient(create_app(settings)) as client:
        history = client.get(f"/api/sessions/{session['id']}/messages")
        missing = client.get("/api/sessions/does-not-exist/messages")

    assert history.status_code == 200
    assert history.json() == [message]
    assert missing.status_code == 404
