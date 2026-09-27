"""Проверка сессий подтверждает API и сохранение данных между запусками."""

import sqlite3

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_session_persists_after_app_restart(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.sqlite3", _env_file=None)

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/sessions",
            json={"title": "Первый проект", "workspace": "C:\\projects\\example"},
        )
        assert created.status_code == 201
        session = created.json()
        assert session["title"] == "Первый проект"
        assert session["agent_id"] == "default"
        assert session["workspace"] == "C:\\projects\\example"

    with TestClient(create_app(settings)) as client:
        fetched = client.get(f"/api/sessions/{session['id']}")
        listed = client.get("/api/sessions")
        missing = client.get("/api/sessions/does-not-exist")

    assert fetched.status_code == 200
    assert fetched.json() == session
    assert listed.status_code == 200
    assert listed.json() == [session]
    assert missing.status_code == 404


def test_session_can_be_renamed(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.sqlite3", _env_file=None)

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        renamed = client.put(
            f"/api/sessions/{session['id']}/title", json={"title": "  Мой проект  "}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Мой проект"
        assert client.put(
            f"/api/sessions/{session['id']}/title", json={"title": "   "}
        ).status_code == 422
        assert client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "Первое сообщение"},
        ).status_code == 201
        assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "Мой проект"

    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "Мой проект"


def test_deleting_session_removes_only_its_messages(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.sqlite3", _env_file=None)

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/sessions", json={}).json()
        second = client.post("/api/sessions", json={}).json()
        for session in (first, second):
            assert client.post(
                f"/api/sessions/{session['id']}/messages",
                json={"content": "Тестовое сообщение"},
            ).status_code == 201

        deleted = client.delete(f"/api/sessions/{first['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/sessions/{first['id']}").status_code == 404
        assert client.get(f"/api/sessions/{first['id']}/messages").status_code == 404
        assert client.delete(f"/api/sessions/{first['id']}").status_code == 404
        assert client.get(f"/api/sessions/{second['id']}/messages").status_code == 200

    with sqlite3.connect(settings.database_path) as connection:
        session_ids = [row[0] for row in connection.execute("SELECT id FROM sessions")]
        message_session_ids = [
            row[0] for row in connection.execute("SELECT session_id FROM messages")
        ]
    assert session_ids == [second["id"]]
    assert message_session_ids == [second["id"]]
