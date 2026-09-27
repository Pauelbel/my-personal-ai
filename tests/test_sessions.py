"""Проверка сессий подтверждает API и сохранение данных между запусками."""

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_session_persists_after_app_restart(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/sessions",
            json={"title": "Первый проект", "workspace": str(tmp_path)},
        )
        assert created.status_code == 201
        session = created.json()
        assert session["title"] == "Первый проект"
        assert session["agent_id"] == "default"
        assert session["workspace"] == str(tmp_path.resolve())

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
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)

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
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)

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

    session_files = sorted(settings.sessions_path.glob("*.json"))
    assert [path.stem for path in session_files] == [second["id"]]
    assert not (settings.conversations_path / f"{first['id']}.jsonl").exists()
    assert (settings.conversations_path / f"{second['id']}.jsonl").exists()
